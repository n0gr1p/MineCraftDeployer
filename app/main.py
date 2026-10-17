import json, os, re, secrets, shutil, uuid, zipfile
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
    return {"id":meta["id"],"name":meta["name"],"port":meta["port"],"ram_gb":meta["ram_gb"],"status":c.status if c else "not-created","address":f'{HOST}:{meta["port"]}'}

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
    if bool(cf_url)!=bool(pack): raise HTTPException(400,"Supply exactly one CurseForge URL or server ZIP")
    if cf_url:
        url=urlparse(cf_url)
        if url.scheme!="https" or url.hostname not in ("curseforge.com","www.curseforge.com") or not url.path.startswith("/minecraft/modpacks/"):
            raise HTTPException(400,"Invalid CurseForge URL")
        if not CF_KEY: raise HTTPException(400,"Configure CF_API_KEY first")
    used={json.loads((p/"meta.json").read_text())["port"] for p in ROOT.iterdir() if (p/"meta.json").exists()}
    port=next((n for n in range(25565,25601) if n not in used),None)
    if port is None: raise HTTPException(409,"No available configured port")
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
        meta={"id":sid,"name":name,"port":port,"ram_gb":ram_gb,"cf_url":cf_url,"mode":"curseforge" if cf_url else "zip"}
        (root/"meta.json").write_text(json.dumps(meta))
        return state(meta)
    except (ValueError,OSError,zipfile.BadZipFile) as exc:
        shutil.rmtree(root,ignore_errors=True)
        raise HTTPException(400,str(exc)) from exc

class Command(BaseModel): action:str

@app.post("/api/servers/{sid}/action")
def action(sid:str,command:Command,authorization:str|None=Header(None)):
    auth(authorization)
    meta=meta_for(sid)
    c=container_for(sid)
    if command.action=="stop":
        if c: c.stop(timeout=45)
    elif command.action=="restart":
        if not c: raise HTTPException(409,"Server not created")
        c.restart(timeout=45)
    elif command.action=="start":
        if c:
            if c.status!="running": c.start()
        else:
            env={"EULA":"TRUE","MEMORY":f'{meta["ram_gb"]}G',"ONLINE_MODE":"TRUE","ENABLE_WHITELIST":"TRUE","ENABLE_RCON":"FALSE"}
            if meta["mode"]=="curseforge": env.update({"TYPE":"AUTO_CURSEFORGE","CF_PAGE_URL":meta["cf_url"],"CF_API_KEY":CF_KEY})
            else:
                if not (path_for(sid)/"minecraft"/"server.jar").exists(): raise HTTPException(409,"Server ZIP must contain a top-level server.jar")
                env.update({"TYPE":"CUSTOM","CUSTOM_SERVER":"/data/server.jar"})
            client.containers.run("itzg/minecraft-server:java21",detach=True,name="mcd-"+sid,environment=env,volumes={str(path_for(sid)/"minecraft"):{"bind":"/data","mode":"rw"}},ports={"25565/tcp":meta["port"]},mem_limit=f'{meta["ram_gb"]+1}g',nano_cpus=400000000,restart_policy={"Name":"unless-stopped"},security_opt=["no-new-privileges:true"],cap_drop=["ALL"])
    else: raise HTTPException(400,"Unknown action")
    return state(meta)

@app.get("/api/servers/{sid}/logs")
def logs(sid:str,authorization:str|None=Header(None)):
    auth(authorization)
    c=container_for(sid)
    return {"logs":c.logs(tail=250).decode(errors="replace")[-30000:] if c else "Not started"}

@app.get("/api/health")
def health(): return {"ok":True}
