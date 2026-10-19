import fcntl, json, os, re, secrets, shutil, uuid, zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
import docker
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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

# Curated GUI settings. Never expose arbitrary file paths, environment names,
# level generation, networking, whitelist, RCON credentials, or Docker flags.
PROPERTY_SCHEMA={
    "pvp":("PVP","bool",True),
    "difficulty":("DIFFICULTY","choice","easy",("peaceful","easy","normal","hard")),
    "gamemode":("MODE","choice","survival",("survival","creative","adventure","spectator")),
    "allow-flight":("ALLOW_FLIGHT","bool",False),
    "enable-command-block":("ENABLE_COMMAND_BLOCK","bool",False),
    "spawn-monsters":("SPAWN_MONSTERS","bool",True),
    "spawn-animals":("SPAWN_ANIMALS","bool",True),
    "spawn-npcs":("SPAWN_NPCS","bool",True),
    "allow-nether":("ALLOW_NETHER","bool",True),
    "max-players":("MAX_PLAYERS","int",20,1,100),
    "view-distance":("VIEW_DISTANCE","int",10,3,16),
    "spawn-protection":("SPAWN_PROTECTION","int",16,0,128),
}
DREGORA_LOCKED={
    "difficulty":"hard",
    "allow-flight":True,
    "enable-command-block":True,
    "view-distance":10,
}


def locked_properties(meta):
    return DREGORA_LOCKED if meta.get("pack_format")=="rlcraft-dregora" else {}


def normalized_setting(key,value):
    spec=PROPERTY_SCHEMA[key]
    kind=spec[1]
    if kind=="bool":
        if type(value) is not bool: raise ValueError(f"{key} must be on or off")
        return value
    if kind=="int":
        if type(value) is not int or not spec[3]<=value<=spec[4]:
            raise ValueError(f"{key} must be between {spec[3]} and {spec[4]}")
        return value
    if not isinstance(value,str) or value not in spec[3]:
        raise ValueError(f"{key} must be one of: {', '.join(spec[3])}")
    return value


def setting_env_value(value):
    if isinstance(value,bool): return "TRUE" if value else "FALSE"
    return str(value)


def selected_setting_env(meta):
    return {PROPERTY_SCHEMA[key][0]:setting_env_value(value)
            for key,value in meta.get("server_properties",{}).items()
            if key in PROPERTY_SCHEMA and key not in locked_properties(meta)}


def settings_pending(c,meta):
    if not c: return False
    env=minecraft_env(c)
    return any(env.get(key)!=value
               for key,value in selected_setting_env(meta).items())


def read_properties(meta):
    # Existing server.properties, when available, is the baseline rather than
    # overwriting a modpack's non-default settings with vanilla defaults.
    path=path_for(meta["id"])/"minecraft"/"server.properties"
    properties={}
    try:
        for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith(("#","!")):
                key,value=line.split("=",1)
                properties[key.strip()]=value.strip()
    except OSError:
        pass

    values={}
    for key,spec in PROPERTY_SCHEMA.items():
        value=properties.get(key)
        try:
            if value is None: raise ValueError("not in file")
            if spec[1]=="bool":
                if value.lower() not in ("true","false"): raise ValueError("bad bool")
                converted=value.lower()=="true"
            elif spec[1]=="int":
                converted=int(value)
            else:
                converted=value.lower()
                # Old Minecraft/Forge server.properties may encode game
                # mode/difficulty as numeric IDs rather than words.
                legacy={
                    "difficulty":{"0":"peaceful","1":"easy","2":"normal","3":"hard"},
                    "gamemode":{"0":"survival","1":"creative","2":"adventure","3":"spectator"}
                }
                converted=legacy.get(key,{}).get(converted,converted)
            values[key]=normalized_setting(key,converted)
        except ValueError:
            values[key]=spec[2]

    values.update({key:value for key,value in meta.get("server_properties",{}).items()
                   if key in PROPERTY_SCHEMA})
    values.update(locked_properties(meta))
    return values


_FTB_MODPACK_PATH=re.compile(r"/modpacks/([1-9][0-9]{0,9})(?:-[a-z0-9-]+)?/?")
_FTB_VERSIONS={8,17,21,25}


def parse_ftb_url(value):
    # Only an official FTB web URL is accepted; never fetch arbitrary user URLs.
    url=urlparse(value)
    if (url.scheme!="https" or url.netloc not in ("feed-the-beast.com","www.feed-the-beast.com")
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("Use an HTTPS FTB modpack URL from feed-the-beast.com")
    match=_FTB_MODPACK_PATH.fullmatch(url.path)
    if not match:
        raise ValueError("FTB URL must look like https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2")
    return int(match.group(1))


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
    if meta["mode"]=="ftb":
        if (not isinstance(meta.get("ftb_modpack_id"),int) or meta["ftb_modpack_id"]<1
                or not isinstance(meta.get("ftb_version_id"),int) or meta["ftb_version_id"]<1
                or meta.get("java_version") not in _FTB_VERSIONS):
            raise HTTPException(409,"Invalid FTB profile metadata; activation cancelled before stopping the selected server")
    if meta["mode"]=="forge":
        data=path_for(meta["id"])/"minecraft"
        if meta.get("pack_format")!="rlcraft-dregora" or meta.get("minecraft_version")!="1.12.2" or meta.get("loader_version")!="14.23.5.2860" or not (data/"SERVER README OR DIE.txt").is_file() or not (data/"mods").is_dir() or not (data/"config").is_dir():
            raise HTTPException(409,"The installed Dregora Forge server pack is missing or incompatible; activation cancelled")
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
        requested_ram=int(meta["ram_gb"])
        existing_heap=minecraft_env(c).get("MEMORY")
        existing_limit=c.attrs.get("HostConfig",{}).get("Memory")
        ram_mismatch=(existing_heap!=f"{requested_ram}G"
                      or existing_limit!=(requested_ram+1)*1024**3)
        if ports!={str(GAME_PORT)} or incompatible_security or not player_controls_ready(c) or ram_mismatch or settings_pending(c,meta):
            # Recreate on changed port, incompatible options, or changed RAM/heap.
            # /data is a bind mount. Removing the game container does not delete game files.
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
    if meta["mode"]=="ftb":
        # FTBA fetches official FTB installers and installs the mod loader;
        # the pinned version prevents surprise modpack upgrades on restart.
        env.update({"TYPE":"FTBA",
                    "FTB_MODPACK_ID":str(meta["ftb_modpack_id"]),
                    "FTB_MODPACK_VERSION_ID":str(meta["ftb_version_id"])})
    elif meta["mode"]=="curseforge":
        env.update({"TYPE":"AUTO_CURSEFORGE","CF_PAGE_URL":meta["cf_url"]})
        if CF_KEY: env["CF_API_KEY"]=CF_KEY
    elif meta["mode"]=="fabric":
        env.update({"TYPE":"FABRIC",
                    "VERSION":meta["minecraft_version"],
                    "FABRIC_LOADER_VERSION":meta["loader_version"]})
    elif meta["mode"]=="forge":
        # Dregora v1.1.2b requirements from its bundled server README.
        # TYPE=FORGE installs Forge without executing untrusted pack scripts.
        env.update({
            "TYPE":"FORGE", "VERSION":meta["minecraft_version"],
            "FORGE_VERSION":meta["loader_version"],
            "LEVEL":"DregoraRL", "LEVEL_TYPE":"OTG",
            "ALLOW_FLIGHT":"TRUE", "DIFFICULTY":"hard",
            "MAX_TICK_TIME":"-1", "ENABLE_COMMAND_BLOCK":"TRUE",
            "VIEW_DISTANCE":"10",
        })
    elif meta["mode"]=="zip":
        env.update({"TYPE":"CUSTOM","CUSTOM_SERVER":"/data/server.jar"})
    else:
        raise HTTPException(409,"Unsupported server mode")
    # User-edited settings are applied on container creation. Existing containers
    # are recreated on activation/restart when their saved settings differ.
    env.update(selected_setting_env(meta))
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
            # A normal Docker restart cannot change environment variables.
            # Recreate only when saved settings or RAM differ from the container.
            newer=get_or_create_container(meta)
            if newer is c:
                c.restart(timeout=45)
            else:
                newer.reload()
                if newer.status!="running": newer.start()
                newer.update(restart_policy={"Name":"unless-stopped"})
            return state(meta)
        except (docker.errors.DockerException,RuntimeError) as exc:
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
    requested_ram=int(meta["ram_gb"])
    pending_ram=bool(c and (
        minecraft_env(c).get("MEMORY")!=f"{requested_ram}G"
        or c.attrs.get("HostConfig",{}).get("Memory")!=(requested_ram+1)*1024**3
    ))
    return {"id":meta["id"],"name":meta["name"],"port":GAME_PORT,
            "ram_gb":meta["ram_gb"],"max_ram_gb":MAX_RAM,
            "ram_change_pending":pending_ram,
            "server_properties":read_properties(meta),
            "locked_properties":list(locked_properties(meta)),
            "settings_pending":settings_pending(c,meta),
            "status":c.status if c else "not-created",
            "is_active":active_id()==meta["id"],
            "whitelist":allowed,
            "ops":operators,
            "player_controls_ready":bool(c and player_controls_ready(c)),
            "mode":meta.get("mode","zip"),
            "minecraft_version":meta.get("minecraft_version"),
            "loader_version":meta.get("loader_version"),
            "java_version":meta.get("java_version",21),
            "ftb_modpack_id":meta.get("ftb_modpack_id"),
            "ftb_version_id":meta.get("ftb_version_id"),
            "address":f"{HOST}:{GAME_PORT}"}

@app.get("/")
def home(): return FileResponse(Path(__file__).parent/"static"/"index.html")

@app.get("/api/servers")
def list_servers(authorization:str|None=Header(None)):
    auth(authorization)
    return [state(json.loads((p/"meta.json").read_text())) for p in ROOT.iterdir() if (p/"meta.json").is_file()]

@app.post("/api/servers")
async def create_server(name:str=Form(...),ram_gb:int=Form(6),players:str=Form(...),cf_url:str=Form(""),ftb_url:str=Form(""),ftb_version_id:str=Form(""),ftb_java_version:int=Form(25),pack:UploadFile|None=File(None),world:UploadFile|None=File(None),authorization:str|None=Header(None)):
    auth(authorization)
    if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9 _-]{0,39}",name): raise HTTPException(400,"Invalid server name")
    if not 2<=ram_gb<=MAX_RAM: raise HTTPException(400,"Invalid RAM allocation")
    if sum((bool(cf_url),bool(ftb_url),bool(pack)))!=1:
        raise HTTPException(400,"Provide exactly one installation source: CurseForge URL, FTB URL, or server ZIP")
    if ftb_url:
        try:
            ftb_pack_id=parse_ftb_url(ftb_url)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        if not re.fullmatch(r"[1-9][0-9]{0,9}",ftb_version_id):
            raise HTTPException(400,"FTB version ID is required; copy its numeric ID from the pack's Versions tab")
        if ftb_java_version not in _FTB_VERSIONS:
            raise HTTPException(400,"FTB Java version must be 8, 17, 21, or 25 (Evolution 2 requires 25)")
    elif ftb_version_id:
        raise HTTPException(400,"An FTB version ID requires an FTB modpack URL")
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
            shutil.copytree(candidates[0].parent,data/(detected.get("world_name","world") if pack else "world"),dirs_exist_ok=True)
            shutil.rmtree(root/"import")
        meta={"id":sid,"name":name,"ram_gb":ram_gb,"cf_url":cf_url,
              "mode":"ftb" if ftb_url else ("curseforge" if cf_url else "zip"),
              "whitelist":whitelist,"ops":[]}
        if ftb_url:
            meta.update({"ftb_url":ftb_url,"ftb_modpack_id":ftb_pack_id,
                         "ftb_version_id":int(ftb_version_id),
                         "java_version":ftb_java_version})
        if pack: meta.update(detected)
        (root/"meta.json").write_text(json.dumps(meta))
        return state(meta)
    except (ValueError,OSError,zipfile.BadZipFile) as exc:
        shutil.rmtree(root,ignore_errors=True)
        raise HTTPException(400,str(exc)) from exc

class DeleteServerConfirmation(BaseModel):
    confirm_name:str
    confirm_word:Literal["DELETE"]


@app.post("/api/servers/{sid}/delete")
def delete_server(sid:str,confirmation:DeleteServerConfirmation,
                  authorization:str|None=Header(None)):
    auth(authorization)
    with deployment_lock():
        # Deleted data includes world saves, packs and settings; never act on
        # a running or selected profile, even if that selected game is stopped.
        directory=path_for(sid)
        if directory.is_symlink() or directory.resolve().parent!=ROOT.resolve():
            raise HTTPException(409,"Unsafe profile path; refusing deletion")
        meta=meta_for(sid)
        if meta.get("id")!=sid:
            raise HTTPException(409,"Profile ID does not match its directory")
        if confirmation.confirm_name!=meta["name"]:
            raise HTTPException(400,"Type the server name exactly to confirm permanent deletion")
        if active_id()==sid:
            raise HTTPException(409,"Cannot delete the selected server. Make another server active first")

        try:
            c=container_for(sid)
            if c:
                c.reload()
                if c.status not in ("created","exited"):
                    raise HTTPException(409,
                        "The server container must be fully stopped before deletion")
                # Do not remove any volumes here. Profile data is handled only
                # after Docker confirms the container removal succeeded.
                c.remove()
        except docker.errors.DockerException as exc:
            raise HTTPException(409,
                f"Could not remove the stopped game container; profile files preserved: {exc}") from exc

        try:
            # This is deliberately permanent. A failed filesystem removal may
            # leave a partial profile, which can be inspected/retried; never
            # report success unless the complete folder is gone.
            shutil.rmtree(directory)
        except OSError as exc:
            raise HTTPException(409,
                f"Game container removed, but deleting profile files failed: {exc}. Check the server data folder before retrying.") from exc

        return {"deleted":True,"id":sid,"name":meta["name"]}


class RamChange(BaseModel):
    ram_gb:int=Field(ge=2)
    restart_running:bool=False


@app.patch("/api/servers/{sid}/ram")
def update_ram(sid:str,change:RamChange,authorization:str|None=Header(None)):
    auth(authorization)
    if change.ram_gb>MAX_RAM:
        raise HTTPException(400,f"RAM must be between 2 and {MAX_RAM} GB")

    with deployment_lock():
        meta=meta_for(sid)
        old_ram=int(meta["ram_gb"])
        if change.ram_gb==old_ram:
            return state(meta)

        c=container_for(sid)
        if c: c.reload()
        game_running=bool(c and running(c))
        if game_running:
            if active_id()!=sid:
                raise HTTPException(409,"Cannot change RAM on an unexpected running inactive server")
            if not change.restart_running:
                raise HTTPException(409,"Changing RAM requires a Minecraft restart. Confirm the restart before saving.")

        # Stopped servers store the new allocation; get_or_create_container()
        # applies it on the next activation by comparing heap and Docker memory cap.
        previous=meta.copy()
        meta["ram_gb"]=change.ram_gb
        try:
            write_meta(meta)
        except OSError as exc:
            raise HTTPException(409,f"Could not save RAM setting: {exc}") from exc
        if not game_running:
            return state(meta)

        try:
            # Only the selected game container is replaced, never its /data directory.
            new=get_or_create_container(meta)
            new.reload()
            if new.status!="running": new.start()
            new.update(restart_policy={"Name":"unless-stopped"})
            return state(meta)
        except (docker.errors.DockerException,RuntimeError,OSError) as exc:
            # Best effort restore the original allocation and Minecraft service.
            rollback_error=None
            try:
                write_meta(previous)
                restore=get_or_create_container(previous)
                restore.reload()
                if restore.status!="running": restore.start()
                restore.update(restart_policy={"Name":"unless-stopped"})
            except (docker.errors.DockerException,RuntimeError,OSError) as rollback:
                rollback_error=str(rollback)
            detail=f"Could not change RAM: {exc}"
            if rollback_error:
                detail+=f"; restoring the old {old_ram} GB server also failed: {rollback_error}"
            else:
                detail+=f"; previous {old_ram} GB allocation restored"
            raise HTTPException(409,detail) from exc


class SettingsUpdate(BaseModel):
    properties:dict[str,bool|int|str]


@app.patch("/api/servers/{sid}/settings")
def update_settings(sid:str,change:SettingsUpdate,authorization:str|None=Header(None)):
    auth(authorization)
    with deployment_lock():
        meta=meta_for(sid)
        c=container_for(sid)
        if c:
            c.reload()
            if running(c) and active_id()!=sid:
                raise HTTPException(409,"Cannot modify an unexpectedly running inactive server")
        current=dict(meta.get("server_properties",{}))
        locks=locked_properties(meta)
        for key,value in change.properties.items():
            if key not in PROPERTY_SCHEMA:
                raise HTTPException(400,f"Unsupported server property: {key}")
            if key in locks:
                raise HTTPException(409,
                    f"{key} is locked for Dregora's required world-generation compatibility")
            try:
                current[key]=normalized_setting(key,value)
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
        meta["server_properties"]=current
        try:
            write_meta(meta)
        except OSError as exc:
            raise HTTPException(409,f"Could not save server settings: {exc}") from exc
        return state(meta)


class ConsoleCommand(BaseModel):
    command:str=Field(min_length=1,max_length=256)


@app.post("/api/servers/{sid}/console")
def run_console(sid:str,request:ConsoleCommand,authorization:str|None=Header(None)):
    auth(authorization)
    command=request.command.strip()
    if command.startswith("/"):
        command=command[1:]
    if not command or any(ord(ch)<32 or ord(ch)==127 for ch in command):
        raise HTTPException(400,"Enter one Minecraft command without newlines or control characters")
    # Never invoke a shell: RCON commands are one argv entry after 'rcon-cli'.
    # Unlike the restricted player manager this is fully privileged Minecraft
    # console access; protect the portal token and do not expose TCP 8080.
    with deployment_lock():
        meta_for(sid)
        if active_id()!=sid:
            raise HTTPException(409,"Console is only available on the selected server")
        c=container_for(sid)
        if not c:
            raise HTTPException(409,"Start Minecraft before using its console")
        c.reload()
        if c.status!="running":
            raise HTTPException(409,"Minecraft is not running")
        if not player_controls_ready(c):
            raise HTTPException(409,"Console requires the RCON-enabled game container. Use Enable Players first.")
        try:
            result=c.exec_run(["rcon-cli",command],stdout=True,stderr=True)
        except docker.errors.DockerException as exc:
            raise HTTPException(409,f"Could not reach Minecraft console: {exc}") from exc
        reply=(result.output.decode(errors="replace") if isinstance(result.output,bytes)
               else str(result.output))
        if result.exit_code!=0:
            raise HTTPException(409,
                "Minecraft console request failed. Is the server ready? Reply: "+reply[:1500])
        return {"output":reply[:8000],"command":command}


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
