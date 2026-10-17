import fcntl, json, os, re, secrets, shutil, uuid, zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
import docker
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

def check_launch(meta):
    if meta["mode"]=="zip" and not (path_for(meta["id"])/"minecraft"/"server.jar").is_file():
        raise HTTPException(409,"This ZIP does not contain a top-level server.jar; it cannot be activated yet")
    if meta["mode"]=="curseforge" and not CF_KEY:
        raise HTTPException(409,"CF_API_KEY is required for CurseForge packs")

def get_or_create_container(meta):
    sid=meta["id"]
    c=container_for(sid)
    if c:
        c.reload()
        existing=(c.attrs.get("HostConfig",{}).get("PortBindings",{}).get("25565/tcp") or [])
        ports={str(binding.get("HostPort")) for binding in existing}
        if ports!={str(GAME_PORT)}:
            # A container created by the old multiport preview must be replaced.
            # Its /data volume is a bind mount and is never removed.
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
         "ENABLE_WHITELIST":"TRUE","ENABLE_RCON":"FALSE"}
    if meta["mode"]=="curseforge":
        env.update({"TYPE":"AUTO_CURSEFORGE","CF_PAGE_URL":meta["cf_url"],"CF_API_KEY":CF_KEY})
    else:
        env.update({"TYPE":"CUSTOM","CUSTOM_SERVER":"/data/server.jar"})
    return client.containers.create(
        "itzg/minecraft-server:java21",name="mcd-"+sid,environment=env,
        volumes={str(path_for(sid)/"minecraft"):{"bind":"/data","mode":"rw"}},
        ports={"25565/tcp":GAME_PORT},mem_limit=f'{meta["ram_gb"]+1}g',
        nano_cpus=400000000,restart_policy={"Name":"no"},
        security_opt=["no-new-privileges:true"],cap_drop=["ALL"])

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
    return {"id":meta["id"],"name":meta["name"],"port":GAME_PORT,
            "ram_gb":meta["ram_gb"],"status":c.status if c else "not-created",
            "is_active":active_id()==meta["id"],"address":f"{HOST}:{GAME_PORT}"}

@app.get("/")
def home(): return FileResponse(Path(__file__).parent/"static"/"index.html")

@app.get("/api/servers")
def list_servers(authorization:str|None=Header(None)):
    auth(authorization)
    return [state(json.loads((p/"meta.json").read_text())) for p in ROOT.iterdir() if (p/"meta.json").is_file()]

@app.post("/api/servers")
async def create_server(name:str=Form(...),ram_gb:int=Form(6),cf_url:str=Form(""),pack:UploadFile|None=File(None),world:UploadFile|None=File(None),authorization:str|None=Header(None)):
    auth(authorization)
    if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9 _-]{0,39}",name): raise HTTPException(400,"Invalid server name")
    if not 2<=ram_gb<=MAX_RAM: raise HTTPException(400,"Invalid RAM allocation")
    if bool(cf_url)==bool(pack): raise HTTPException(400,"Supply exactly one CurseForge URL or server ZIP")
    if cf_url:
        url=urlparse(cf_url)
        if url.scheme!="https" or url.hostname not in ("curseforge.com","www.curseforge.com") or not url.path.startswith("/minecraft/modpacks/"):
            raise HTTPException(400,"Invalid CurseForge URL")
        if not CF_KEY: raise HTTPException(400,"Configure CF_API_KEY first")
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
            if (data/"manifest.json").exists(): raise ValueError("Client-export ZIP is not a ready-to-run server pack")
        if world:
            archive=root/"world.zip"
            await save_upload(world,archive)
            safe_extract(archive,root/"import")
            archive.unlink()
            candidates=list((root/"import").rglob("level.dat"))
            if len(candidates)!=1: raise ValueError("World ZIP must contain exactly one level.dat")
            shutil.copytree(candidates[0].parent,data/"world",dirs_exist_ok=True)
            shutil.rmtree(root/"import")
        meta={"id":sid,"name":name,"ram_gb":ram_gb,"cf_url":cf_url,"mode":"curseforge" if cf_url else "zip"}
        (root/"meta.json").write_text(json.dumps(meta))
        return state(meta)
    except (ValueError,OSError,zipfile.BadZipFile) as exc:
        shutil.rmtree(root,ignore_errors=True)
        raise HTTPException(400,str(exc)) from exc

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
