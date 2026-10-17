import fcntl, json, os, re, secrets, shutil, uuid, zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
import docker
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal
from app.pack_import import prepare_pack

ROOT=Path(os.getenv("MCD_DATA","/data")).resolve()
ROOT.mkdir(parents=True,exist_ok=True)
TOKEN=os.getenv("MCD_TOKEN","")
if len(TOKEN)<24: raise RuntimeError("MCD_TOKEN must be at least 24 characters")
CF_KEY=os.getenv("CF_API_KEY","")
MAX_RAM=int(os.getenv("MCD_MAX_RAM_GB","12"))
HOST=os.getenv("MCD_PUBLIC_HOST","localhost")
GAME_PORT=int(os.getenv("MCD_GAME_PORT","25565"))
if not 1<=GAME_PORT<=65535: raise RuntimeError("Invalid MCD_GAME_PORT")
client=docker.from_env()
app=FastAPI(title="MineCraftDeployer")
app.mount("/static",StaticFiles(directory=Path(__file__).parent/"static"),name="static")

def auth(authorization):
    value=authorization.removeprefix("Bearer ") if authorization else ""
    if not secrets.compare_digest(value,TOKEN): raise HTTPException(401,"Unauthorized")

def path_for(sid):
    if not re.fullmatch("[0-9a-f]{32}",sid): raise HTTPException(404,"Unknown server")
    path=ROOT/sid
    if not (path/"meta.json").exists(): raise HTTPException(404,"Unknown server")
    return path

def meta_for(sid): return json.loads((path_for(sid)/"meta.json").read_text())

def container_for(sid):
    try: return client.containers.get("mcd-"+sid)
    except docker.errors.NotFound: return None

def active_id():
    try:
        return json.loads((ROOT/"active.json").read_text()).get("server_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def set_active(sid):
    temp=ROOT/(".active-"+uuid.uuid4().hex+".tmp")
    temp.write_text(json.dumps({"server_id":sid}))
    temp.replace(ROOT/"active.json")

@contextmanager
def deployment_lock():
    # Across processes/workers on this Linux host, not just Python threads.
    with (ROOT/".deployment.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def all_managed():
    for directory in ROOT.iterdir():
        if re.fullmatch("[0-9a-f]{32}",directory.name) and (directory/"meta.json").is_file():
            c=container_for(directory.name)
            if c:
                c.reload()
                yield directory.name,c

def running(c):
    return c.status in ("running","restarting","paused")

def minecraft_env(c):
    return dict(item.split("=",1) for item in c.attrs.get("Config",{}).get("Env",[])
                if "=" in item)

def player_controls_ready(c):
    env=minecraft_env(c)
    return (env.get("ENABLE_RCON","").upper()=="TRUE"
            and env.get("EXISTING_WHITELIST_FILE","").upper()=="SKIP"
            and env.get("EXISTING_OPS_FILE","").upper()=="SKIP")

def listed_players(meta,filename,fallback):
    # Minecraft's persisted JSON is authoritative, including changes via console.
    file=path_for(meta["id"])/"minecraft"/filename
    try:
        entries=json.loads(file.read_text())
        if not isinstance(entries,list): return fallback
        names=[p.get("name") for p in entries if isinstance(p,dict)]
        return list(dict.fromkeys(name for name in names
                         if isinstance(name,str) and re.fullmatch(r"[A-Za-z0-9_]{3,16}",name)))
    except (OSError,ValueError):
        return fallback

def player_lists(meta):
    return (listed_players(meta,"whitelist.json",meta.get("whitelist",[])),
            listed_players(meta,"ops.json",meta.get("ops",[])))

def write_meta(meta):
    metadata=path_for(meta["id"])/"meta.json"
    staged=metadata.with_name("."+uuid.uuid4().hex+".tmp")
    staged.write_text(json.dumps(meta))
    staged.replace(metadata)

def check_launch(meta):
    # An older portal version accepted an outer ZIP without preparing its contents.
    # Repair its profile on first activation, before stopping any running game.
    if meta["mode"]=="zip":
        data=path_for(meta["id"])/"minecraft"
        try:
            detected=prepare_pack(data, safe_extract)
        except (ValueError,OSError,zipfile.BadZipFile) as exc:
            raise HTTPException(409,f"Cannot activate this pack: {exc}") from exc
        if any(meta.get(key)!=val for key,val in detected.items()):
            meta.update(detected)
            metadata=path_for(meta["id"])/"meta.json"
            staged=metadata.with_suffix(".tmp")
            staged.write_text(json.dumps(meta))
            staged.replace(metadata)
    if meta["mode"]=="fabric" and not (path_for(meta["id"])/"minecraft"/"variables.txt").is_file():
        raise HTTPException(409,"Fabric pack metadata is missing")
    if meta["mode"]=="zip" and not (path_for(meta["id"])/"minecraft"/"server.jar").is_file():
        raise HTTPException(409,"Server ZIP must include a top-level server.jar")

def get_or_create_container(meta):
    sid=meta["id"]
    c=container_for(sid)
    if c:
        c.reload()
        existing=(c.attrs.get("HostConfig",{}).get("PortBindings",{}).get("25565/tcp") or [])
        ports={str(binding.get("HostPort")) for binding in existing}
        security_options=c.attrs.get("HostConfig",{}).get("SecurityOpt") or []
        incompatible_security=any(
            option.startswith("no-new-privileges") for option in security_options
        )
        if ports!={str(GAME_PORT)} or incompatible_security or not player_controls_ready(c):
            # Migrate legacy ports and containers with an incompatible security option.
            # The data directory is bind-mounted; removing the container never deletes it.
            c.update(restart_policy={"Name":"no"})
            if running(c):
                c.stop(timeout=45)
                c.reload()
                if running(c): raise RuntimeError("Old container did not stop")
            c.remove()
            c=None
    if c:
        return c
    env={"EULA":"TRUE","MEMORY":f'{meta["ram_gb"]}G',"ONLINE_MODE":"TRUE",
         "ENABLE_RCON":"TRUE","RCON_PASSWORD":secrets.token_urlsafe(32),
         "EXISTING_WHITELIST_FILE":"SKIP","EXISTING_OPS_FILE":"SKIP"}
    allowed=meta.get("whitelist",[])
    if allowed:
        # A public game port must only admit the names entered at installation.
        env.update({"ENABLE_WHITELIST":"TRUE","ENFORCE_WHITELIST":"TRUE",
                    "WHITELIST":",".join(allowed)})
    else:
        # Backward compatibility for profiles created before player entry existed.
        # Unrestricted game joining is clearly indicated in the dashboard.
        env["ENABLE_WHITELIST"]="FALSE"
    ops=meta.get("ops",[])
    if ops: env["OPS"]=",".join(ops)
    if meta["mode"]=="curseforge":
        env.update({"TYPE":"AUTO_CURSEFORGE","CF_PAGE_URL":meta["cf_url"]})
        if CF_KEY: env["CF_API_KEY"]=CF_KEY
    elif meta["mode"]=="fabric":
        env.update({"TYPE":"FABRIC",
                    "VERSION":meta["minecraft_version"],
                    "FABRIC_LOADER_VERSION":meta["loader_version"]})
    elif meta["mode"]=="zip":
        env.update({"TYPE":"CUSTOM","CUSTOM_SERVER":"/data/server.jar"})
    else:
        raise HTTPException(409,"Unsupported server mode")
    java_version=meta.get("java_version",21)
    return client.containers.create(
        f"itzg/minecraft-server:java{java_version}",name="mcd-"+sid,environment=env,
        volumes={str(path_for(sid)/"minecraft"):{"bind":"/data","mode":"rw"}},
        ports={"25565/tcp":GAME_PORT},mem_limit=f'{meta["ram_gb"]+1}g',
        nano_cpus=400000000,restart_policy={"Name":"no"})

def activate(sid):
    # The lock covers the entire stop -> start transition, including rollback.
    with deployment_lock():
        meta=meta_for(sid)
        check_launch(meta)  # Reject invalid packs BEFORE stopping an active game.
        old_running=[(other_id,c) for other_id,c in all_managed()
                     if other_id!=sid and c.status=="running"]
        try:
            for other_id,c in all_managed():
                if other_id==sid: continue
                # Prevent Docker restarting an inactive profile on a future reboot.
                c.update(restart_policy={"Name":"no"})
                if running(c):
                    c.stop(timeout=45)
                    c.reload()
                    if running(c): raise RuntimeError(f"Could not stop {other_id}")
            c=get_or_create_container(meta)
            c.reload()
            if c.status!="running": c.start()
            c.update(restart_policy={"Name":"unless-stopped"})
            set_active(sid)
            return state(meta)
        except (docker.errors.DockerException,RuntimeError,OSError) as exc:
            # Best effort: restore the previous live server if switching failed.
            restore_error=None
            if len(old_running)==1:
                prior_id,prior=old_running[0]
                try:
                    current=container_for(sid)
                    if current:
                        current.reload()
                        if running(current):
                            current.update(restart_policy={"Name":"no"})
                            current.stop(timeout=45)
                    prior.reload()
                    if prior.status!="running": prior.start()
                    prior.update(restart_policy={"Name":"unless-stopped"})
                    set_active(prior_id)
                except (docker.errors.DockerException,RuntimeError,OSError) as rollback:
                    restore_error=str(rollback)
            detail=f"Could not activate server: {exc}"
            if restore_error: detail+=f"; previous server restore also failed: {restore_error}"
            raise HTTPException(409,detail) from exc

def stop_active(sid):
    with deployment_lock():
        if active_id()!=sid:
            raise HTTPException(409,"Only the selected server can be stopped")
        c=container_for(sid)
        if c:
            try:
                c.update(restart_policy={"Name":"no"})
                c.reload()
                if running(c): c.stop(timeout=45)
            except docker.errors.DockerException as exc:
                raise HTTPException(409,f"Could not stop server: {exc}") from exc
        return state(meta_for(sid))

def restart_active(sid):
    with deployment_lock():
        if active_id()!=sid:
            raise HTTPException(409,"Only the selected server can be restarted")
        meta=meta_for(sid)
        c=container_for(sid)
        if not c: raise HTTPException(409,"Server has not been started yet")
        try:
            c.restart(timeout=45)
            return state(meta)
        except docker.errors.DockerException as exc:
            raise HTTPException(409,f"Could not restart server: {exc}") from exc

def safe_extract(source,target):
    with zipfile.ZipFile(source) as archive:
        entries=archive.infolist()
        if len(entries)>50000: raise ValueError("Too many archive members")
        total=0
        for item in entries:
            part=Path(item.filename)
            if part.is_absolute() or ".." in part.parts or chr(92) in item.filename or ":" in item.filename:
                raise ValueError("Unsafe archive member")
            if (item.external_attr>>16)&0o170000==0o120000: raise ValueError("Symlink rejected")
            total+=item.file_size
            if total>12*1024**3: raise ValueError("Archive expands beyond 12 GiB")
        target.mkdir(parents=True,exist_ok=True)
        for item in entries:
            dest=(target/item.filename).resolve()
            if not dest.is_relative_to(target.resolve()): raise ValueError("Unsafe archive path")
            if item.is_dir(): dest.mkdir(parents=True,exist_ok=True)
            else:
                dest.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(item) as source_stream, dest.open("wb") as output:
                    shutil.copyfileobj(source_stream,output)

async def save_upload(upload,target):
    total=0
    with target.open("wb") as out:
        while chunk:=await upload.read(1024**2):
            total+=len(chunk)
            if total>4*1024**3: raise ValueError("Upload exceeds 4 GiB")
            out.write(chunk)
    if not zipfile.is_zipfile(target): raise ValueError("Not a ZIP archive")

def state(meta):
    c=container_for(meta["id"])
    if c: c.reload()
    allowed,operators=player_lists(meta)
    return {"id":meta["id"],"name":meta["name"],"port":GAME_PORT,
            "ram_gb":meta["ram_gb"],"status":c.status if c else "not-created",
            "is_active":active_id()==meta["id"],
            "whitelist":allowed,
            "ops":operators,
            "player_controls_ready":bool(c and player_controls_ready(c)),
            "mode":meta.get("mode","zip"),
            "minecraft_version":meta.get("minecraft_version"),
            "loader_version":meta.get("loader_version"),
            "java_version":meta.get("java_version",21),
            "address":f"{HOST}:{GAME_PORT}"}

@app.get("/")
def home(): return FileResponse(Path(__file__).parent/"static"/"index.html")

@app.get("/api/servers")
def list_servers(authorization:str|None=Header(None)):
    auth(authorization)
    return [state(json.loads((p/"meta.json").read_text())) for p in ROOT.iterdir() if (p/"meta.json").is_file()]

@app.post("/api/servers")
async def create_server(name:str=Form(...),ram_gb:int=Form(6),players:str=Form(...),cf_url:str=Form(""),pack:UploadFile|None=File(None),world:UploadFile|None=File(None),authorization:str|None=Header(None)):
    auth(authorization)
    if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9 _-]{0,39}",name): raise HTTPException(400,"Invalid server name")
    if not 2<=ram_gb<=MAX_RAM: raise HTTPException(400,"Invalid RAM allocation")
    if bool(cf_url)==bool(pack): raise HTTPException(400,"Supply exactly one CurseForge URL or server ZIP")
    whitelist=[p.strip() for p in re.split(r"[,\s]+",players) if p.strip()]
    if not whitelist or len(whitelist)>50 or any(not re.fullmatch(r"[A-Za-z0-9_]{3,16}",p) for p in whitelist):
        raise HTTPException(400,"Enter 1-50 valid Minecraft Java usernames (3-16 letters, numbers, underscores)")
    whitelist=list(dict.fromkeys(whitelist))
    if cf_url:
        url=urlparse(cf_url)
        if url.scheme!="https" or url.hostname not in ("curseforge.com","www.curseforge.com") or not url.path.startswith("/minecraft/modpacks/"):
            raise HTTPException(400,"Invalid CurseForge URL")
    sid=uuid.uuid4().hex
    root=ROOT/sid
    root.mkdir(mode=0o700)
    data=root/"minecraft"
    data.mkdir()
    try:
        if pack:
            archive=root/"pack.zip"
            await save_upload(pack,archive)
            safe_extract(archive,data)
            archive.unlink()
            detected=prepare_pack(data,safe_extract)
        if world:
            archive=root/"world.zip"
            await save_upload(world,archive)
            safe_extract(archive,root/"import")
            archive.unlink()
            candidates=list((root/"import").rglob("level.dat"))
            if len(candidates)!=1: raise ValueError("World ZIP must contain exactly one level.dat")
            shutil.copytree(candidates[0].parent,data/"world",dirs_exist_ok=True)
            shutil.rmtree(root/"import")
        meta={"id":sid,"name":name,"ram_gb":ram_gb,"cf_url":cf_url,"mode":"curseforge" if cf_url else "zip","whitelist":whitelist,"ops":[]}
        if pack: meta.update(detected)
        (root/"meta.json").write_text(json.dumps(meta))
        return state(meta)
    except (ValueError,OSError,zipfile.BadZipFile) as exc:
        shutil.rmtree(root,ignore_errors=True)
        raise HTTPException(400,str(exc)) from exc

class PlayerCommand(BaseModel):
    action:Literal["whitelist_add","whitelist_remove","op","deop"]
    username:str

_SUCCESS={
    "whitelist_add":("added ","to the whitelist"),
    "whitelist_remove":("removed ","from the whitelist"),
    "op":("made ","a server operator"),
    "deop":("made ","no longer a server operator"),
}

@app.post("/api/servers/{sid}/players")
def manage_player(sid:str,command:PlayerCommand,authorization:str|None=Header(None)):
    auth(authorization)
    name=command.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}",name):
        raise HTTPException(400,"Enter a valid Minecraft Java username (3-16 letters, numbers, underscores)")
    with deployment_lock():
        meta=meta_for(sid)
        if active_id()!=sid:
            raise HTTPException(409,"Select this server as active before changing players")
        c=container_for(sid)
        if not c:
            raise HTTPException(409,"Start the selected server before changing players")
        c.reload()
        if c.status!="running":
            raise HTTPException(409,"Minecraft is not running; start the server before changing players")
        if not player_controls_ready(c):
            raise HTTPException(409,"Enable Players first; this requires a one-time server restart")

        allowed,ops=player_lists(meta)
        action=command.action
        if action=="op" and name.casefold() not in {n.casefold() for n in allowed}:
            raise HTTPException(409,"Whitelist the player before granting operator privileges")
        if action=="whitelist_remove" and name.casefold() in {n.casefold() for n in ops}:
            raise HTTPException(409,"Revoke operator privileges before removing a player from the whitelist")
        target_ops=action in ("op","deop")
        current=ops if target_ops else allowed
        present=name.casefold() in {n.casefold() for n in current}
        add=action in ("op","whitelist_add")
        if present==add:
            return state(meta)

        args={
            "whitelist_add":["rcon-cli","whitelist","add",name],
            "whitelist_remove":["rcon-cli","whitelist","remove",name],
            "op":["rcon-cli","op",name],
            "deop":["rcon-cli","deop",name],
        }[action]
        try:
            result=c.exec_run(args,stdout=True,stderr=True)
        except docker.errors.DockerException as exc:
            raise HTTPException(409,f"Could not contact Minecraft console: {exc}") from exc
        output=result.output.decode(errors="replace") if isinstance(result.output,bytes) else str(result.output)
        expected=_SUCCESS[action]
        if result.exit_code!=0 or not all(part in output.lower() for part in expected):
            raise HTTPException(409,"Minecraft did not confirm the change. Check that the server has finished starting. Console reply: "+output[:500])

        # Maintain portal metadata as a fallback when Minecraft JSON is absent.
        destination="ops" if target_ops else "whitelist"
        updated=current+[name] if add else [n for n in current if n.casefold()!=name.casefold()]
        meta[destination]=updated
        try:
            write_meta(meta)
        except OSError as exc:
            raise HTTPException(409,"Minecraft applied the command, but saving portal metadata failed; refresh Players and inspect the logs") from exc
        return state(meta)


class Command(BaseModel):
    action:str

@app.post("/api/servers/{sid}/action")
def action(sid:str,command:Command,authorization:str|None=Header(None)):
    auth(authorization)
    meta_for(sid)
    if command.action=="activate":
        return activate(sid)
    if command.action=="start":
        if active_id()!=sid:
            raise HTTPException(409,"Choose Make Active to switch servers")
        return activate(sid)
    if command.action=="stop":
        return stop_active(sid)
    if command.action=="restart":
        return restart_active(sid)
    raise HTTPException(400,"Unknown action")

@app.get("/api/servers/{sid}/logs")
def logs(sid:str,authorization:str|None=Header(None)):
    auth(authorization)
    c=container_for(sid)
    return {"logs":c.logs(tail=250).decode(errors="replace")[-30000:] if c else "Not started"}

@app.get("/api/health")
def health(): return {"ok":True}
