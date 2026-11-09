"""No Docker daemon required; verify the one-active-container invariant."""
import json
import hashlib
import os
import tempfile
import io
import zipfile

import docker
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from types import SimpleNamespace

os.environ.setdefault("MCD_TOKEN", "test-secret-" + "a" * 40)
os.environ.setdefault("MCD_DATA", tempfile.mkdtemp(prefix="minecraft-deployer-test-"))

from app import main as portal


class FakeContainer:
    def __init__(self, owner, sid, port, fail_start=False, status="created"):
        self.owner=owner
        self.sid=sid
        self.status=status
        self.fail_start=fail_start
        self.policy="no"
        self.port=port
        self.commands=[]
        self.console_ready=True
        self.attrs={"HostConfig":{"PortBindings":{"25565/tcp":[{"HostPort":str(port)}]}},
                    "Config":{"Env":[]}}

    def reload(self): pass

    def update(self, restart_policy):
        self.policy=restart_policy["Name"]

    def start(self):
        if self.fail_start or self.sid in self.owner.fail_once_for:
            self.owner.fail_once_for.discard(self.sid)
            raise docker.errors.APIError("deliberate start failure")
        # No two Minecraft processes should ever overlap in fake runtime.
        assert not any(c.status=="running" for sid,c in self.owner.items.items() if sid!=self.sid)
        self.status="running"

    def stop(self, timeout):
        assert timeout>=45
        self.status="exited"

    def restart(self, timeout):
        assert timeout>=45
        assert self.status=="running"
        self.status="running"

    def remove(self):
        assert self.status!="running"
        del self.owner.items[self.sid]

    def exec_run(self,cmd,stdout,stderr):
        self.commands.append(cmd)
        if not self.console_ready:
            return SimpleNamespace(exit_code=1,output=b"RCON connection refused")
        if cmd[0]!="rcon-cli":
            raise AssertionError("Only rcon-cli console commands are permitted")
        if len(cmd)==2:
            # Simulate one complete Minecraft command, never a shell invocation.
            return SimpleNamespace(exit_code=0,output=("Minecraft response: "+cmd[1]).encode())
        phrase={
            ("whitelist","add"):"Added {name} to the whitelist",
            ("whitelist","remove"):"Removed {name} from the whitelist",
            ("op",):"Made {name} a server operator",
            ("deop",):"Made {name} no longer a server operator"
        }.get(tuple(cmd[1:-1]))
        if phrase is None: raise AssertionError("Unexpected Minecraft command: "+str(cmd))
        return SimpleNamespace(exit_code=0,output=phrase.format(name=cmd[-1]).encode())


class FakeContainers:
    def __init__(self):
        self.items={}
        self.fail_for=set()
        self.fail_once_for=set()
        self.creations=[]
        self.images=[]

    def get(self, name):
        sid=name.removeprefix("mcd-")
        if sid not in self.items:
            raise docker.errors.NotFound("not found")
        return self.items[sid]

    def create(self, image, **kwargs):
        self.images.append(image)
        sid=kwargs["name"].removeprefix("mcd-")
        port=kwargs["ports"]["25565/tcp"]
        c=FakeContainer(self,sid,port,fail_start=sid in self.fail_for)
        c.attrs["Config"]["Env"]=[key+"="+str(val) for key,val in kwargs["environment"].items()]
        c.attrs["HostConfig"]["Memory"]=int(kwargs["mem_limit"][:-1])*1024**3
        self.items[sid]=c
        self.creations.append(kwargs)
        return c


class FakeClient:
    def __init__(self):
        self.containers=FakeContainers()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    fake=FakeClient()
    monkeypatch.setattr(portal,"ROOT",tmp_path)
    monkeypatch.setattr(portal,"client",fake)
    monkeypatch.setattr(portal,"GAME_PORT",25565)
    monkeypatch.setattr(portal,"HOST","friends.example.test")
    return fake


def install_profile(root, index, valid=True):
    sid=f"{index:032x}"
    directory=root/sid
    (directory/"minecraft").mkdir(parents=True)
    if valid:
        (directory/"minecraft"/"server.jar").write_bytes(b"fake-jar")
    (directory/"meta.json").write_text(json.dumps({
        "id":sid,"name":f"Pack {index}","mode":"zip","cf_url":"",
        "ram_gb":6,"whitelist":["TestPlayer"],"port":25565+index  # Legacy value must be ignored.
    }))
    return sid



def test_new_and_switched_game_containers_have_no_cpu_restriction(environment):
    # Minecraft should be able to schedule work on every host logical CPU.
    # No CPU quota, shares override, or affinity restriction is set at creation.
    first=install_profile(portal.ROOT,101)
    second=install_profile(portal.ROOT,102)
    portal.activate(first)
    portal.activate(second)
    assert len(environment.containers.creations)==2
    for settings in environment.containers.creations:
        for key in ("nano_cpus","cpu_quota","cpu_period","cpu_shares","cpuset_cpus"):
            assert key not in settings
        # Removing CPU limits must not also remove memory protection.
        assert settings["mem_limit"]=="7g"



def test_switch_reuses_one_public_port_and_preserves_old_install(environment):
    a=install_profile(portal.ROOT,1)
    b=install_profile(portal.ROOT,2)
    first=portal.activate(a)
    assert first["is_active"] and first["port"]==25565
    assert first["address"]=="friends.example.test:25565"
    assert environment.containers.items[a].status=="running"
    assert environment.containers.items[a].policy=="unless-stopped"

    second=portal.activate(b)
    assert second["is_active"] and second["port"]==25565
    assert environment.containers.items[a].status=="exited"
    assert environment.containers.items[a].policy=="no"
    assert environment.containers.items[b].status=="running"
    assert portal.active_id()==b
    assert (portal.ROOT/a/"minecraft"/"server.jar").exists()
    assert len(environment.containers.creations)==2
    assert {entry["ports"]["25565/tcp"] for entry in environment.containers.creations}=={25565}

    portal.activate(a)
    assert environment.containers.items[a].status=="running"
    assert environment.containers.items[b].status=="exited"
    assert environment.containers.items[b].policy=="no"
    assert len(environment.containers.creations)==2


def test_unlaunchable_pack_does_not_interrupt_current_game(environment):
    a=install_profile(portal.ROOT,1)
    b=install_profile(portal.ROOT,2,valid=False)
    portal.activate(a)
    with pytest.raises(HTTPException) as exc:
        portal.activate(b)
    assert exc.value.status_code==409
    assert portal.active_id()==a
    assert environment.containers.items[a].status=="running"


def test_failed_switch_restores_previous_running_game(environment):
    a=install_profile(portal.ROOT,1)
    b=install_profile(portal.ROOT,2)
    portal.activate(a)
    environment.containers.fail_for.add(b)
    with pytest.raises(HTTPException) as exc:
        portal.activate(b)
    assert "Could not activate server" in str(exc.value.detail)
    assert environment.containers.items[a].status=="running"
    assert environment.containers.items[a].policy=="unless-stopped"
    assert portal.active_id()==a
    assert environment.containers.items[b].status!="running"


def test_only_selected_server_can_be_started_or_stopped(environment):
    a=install_profile(portal.ROOT,1)
    b=install_profile(portal.ROOT,2)
    portal.activate(a)
    with pytest.raises(HTTPException) as exc:
        portal.action(b,portal.Command(action="start"),authorization="Bearer "+portal.TOKEN)
    assert exc.value.status_code==409
    with pytest.raises(HTTPException) as exc:
        portal.stop_active(b)
    assert exc.value.status_code==409
    portal.stop_active(a)
    assert portal.active_id()==a
    assert environment.containers.items[a].status=="exited"
    assert environment.containers.items[a].policy=="no"
    portal.activate(a)
    assert environment.containers.items[a].status=="running"


def test_old_multiport_container_recreated_only_when_activated(environment):
    a=install_profile(portal.ROOT,1)
    old=FakeContainer(environment.containers,a,25566,status="exited")
    environment.containers.items[a]=old
    portal.activate(a)
    assert old not in environment.containers.items.values()
    assert environment.containers.items[a].port==25565
    assert (portal.ROOT/a/"minecraft"/"server.jar").exists()


def test_old_no_new_privileges_container_recreated_and_data_preserved(environment):
    sid=install_profile(portal.ROOT,5)
    old=FakeContainer(environment.containers,sid,25565,status="exited")
    old.attrs["HostConfig"]["SecurityOpt"]=["no-new-privileges:true"]
    environment.containers.items[sid]=old
    (portal.ROOT/sid/"minecraft"/"world").mkdir()
    (portal.ROOT/sid/"minecraft"/"world"/"level.dat").write_bytes(b"world-data")

    portal.set_active(sid)  # Already selected in the UI, but the container exited.
    result=portal.action(sid,portal.Command(action="start"),
                         authorization="Bearer "+portal.TOKEN)

    assert old not in environment.containers.items.values()
    assert result["is_active"] and result["status"]=="running"
    assert old.policy=="no"
    assert (portal.ROOT/sid/"minecraft"/"server.jar").exists()
    assert (portal.ROOT/sid/"minecraft"/"world"/"level.dat").read_bytes()==b"world-data"
    assert "security_opt" not in environment.containers.creations[-1]


def test_no_new_privileges_variation_is_replaced(environment):
    sid=install_profile(portal.ROOT,6)
    old=FakeContainer(environment.containers,sid,25565,status="created")
    old.attrs["HostConfig"]["SecurityOpt"]=["no-new-privileges"]
    environment.containers.items[sid]=old
    portal.activate(sid)
    assert old not in environment.containers.items.values()
    assert "security_opt" not in environment.containers.creations[-1]


def test_switch_stops_all_legacy_running_profiles(environment):
    a=install_profile(portal.ROOT,1)
    b=install_profile(portal.ROOT,2)
    c=install_profile(portal.ROOT,3)
    for sid,port in [(a,25566),(b,25567)]:
        environment.containers.items[sid]=FakeContainer(environment.containers,sid,port,status="running")
    portal.activate(c)
    assert environment.containers.items[a].status=="exited"
    assert environment.containers.items[b].status=="exited"
    assert environment.containers.items[c].status=="running"
    assert sum(item.status=="running" for item in environment.containers.items.values())==1


def test_new_profile_player_whitelist_and_curseforge_without_key(environment,monkeypatch):
    monkeypatch.setattr(portal,"CF_KEY","")
    with TestClient(portal.app) as http:
        resp=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={
                "name":"Family Pack","ram_gb":"6",
                "players":"Daughter_1, Friend2 Friend2",
                "cf_url":"https://www.curseforge.com/minecraft/modpacks/test-pack/files/1234",
            })
        assert resp.status_code==200,resp.text
        result=resp.json()
        assert result["whitelist"]==["Daughter_1","Friend2"]
        assert result["status"]=="not-created"
        portal.activate(result["id"])
        created=environment.containers.creations[-1]
        assert created["environment"]["TYPE"]=="AUTO_CURSEFORGE"
        assert "CF_API_KEY" not in created["environment"]
        assert created["environment"]["WHITELIST"]=="Daughter_1,Friend2"
        assert created["environment"]["ENABLE_WHITELIST"]=="TRUE"
        assert "cap_drop" not in created


def test_invalid_minecraft_player_names_are_rejected(environment):
    with TestClient(portal.app) as http:
        resp=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={
                "name":"Bad Names","ram_gb":"6",
                "players":"bad!name",
                "cf_url":"https://www.curseforge.com/minecraft/modpacks/test-pack",
            })
        assert resp.status_code==400
        assert "Minecraft Java usernames" in resp.json()["detail"]

def server_pack_bytes():
    config=chr(10).join([
        "MINECRAFT_VERSION=1.20.1",
        "MODLOADER=Fabric",
        "MODLOADER_VERSION=0.19.3",
        "RECOMMENDED_JAVA_VERSION=17",
        'JAVA_ARGS="-Xmx8G -Xms8G"',
    ]) + chr(10)
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z:
        z.writestr("variables.txt",config)
        z.writestr("start.sh","echo NEVER RUN ME")
        z.writestr("manifest.json",'{"name":"Prominence II"}')
        z.writestr("mods/example.jar","fake mod")
        z.writestr("config/example.toml","enabled=true")
    return out.getvalue()


def test_legacy_outer_server_pack_unwraps_at_activation(environment):
    """The user's already uploaded ZIP should work without reuploading."""
    sid=install_profile(portal.ROOT,4,valid=False)
    data=portal.ROOT/sid/"minecraft"
    with zipfile.ZipFile(data/"outer.zip","w") as outer:
        outer.writestr("Prominence_II_v4_server_pack.zip",server_pack_bytes())
        outer.writestr("readme.txt","Unzip and use the contained server pack")
    # Simulate the old portal's behavior, where it unpacked only the wrapper.
    portal.safe_extract(data/"outer.zip",data)
    (data/"outer.zip").unlink()
    portal.activate(sid)
    meta=portal.meta_for(sid)
    assert meta["mode"]=="fabric"
    assert meta["java_version"]==17
    assert meta["minecraft_version"]=="1.20.1"
    assert meta["loader_version"]=="0.19.3"
    assert (data/"mods"/"example.jar").exists()
    assert (data/"config"/"example.toml").exists()
    assert not (data/"Prominence_II_v4_server_pack.zip").exists()
    created=environment.containers.creations[-1]
    assert created["environment"]["TYPE"]=="FABRIC"
    assert created["environment"]["VERSION"]=="1.20.1"
    assert created["environment"]["FABRIC_LOADER_VERSION"]=="0.19.3"
    assert created["environment"]["MEMORY"]=="6G"  # Ignore original pack's -Xmx8G.
    assert environment.containers.items[sid].status=="running"
    assert portal.active_id()==sid


def test_new_outer_zip_upload_installs_fabric_profile(environment):
    outer=io.BytesIO()
    with zipfile.ZipFile(outer,"w") as z:
        z.writestr("prominence-serverpack.zip",server_pack_bytes())
        z.writestr("readme.txt","Outer package")
    with TestClient(portal.app) as http:
        resp=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Prominence II","ram_gb":"6","players":"TestPlayer"},
            files={"pack":("wrapped.zip",outer.getvalue(),"application/zip")})
        assert resp.status_code==200,resp.text
        sid=resp.json()["id"]
        assert portal.meta_for(sid)["mode"]=="fabric"
        assert portal.meta_for(sid)["java_version"]==17
        portal.activate(sid)
        assert environment.containers.creations[-1]["environment"]["TYPE"]=="FABRIC"


def test_client_export_zip_rejected_early(environment):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("manifest.json",'{"minecraft":{"version":"1.20.1"}}')
    with TestClient(portal.app) as http:
        resp=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Client Export","ram_gb":"6","players":"TestPlayer"},
            files={"pack":("client.zip",archive.getvalue(),"application/zip")})
        assert resp.status_code==400
        assert "client-export" in resp.json()["detail"]


def player_call(sid,action,name):
    return portal.manage_player(sid,portal.PlayerCommand(action=action,username=name),
                                authorization="Bearer "+portal.TOKEN)


def test_players_live_rcon_operations_and_persistence(environment):
    sid=install_profile(portal.ROOT,20)
    portal.activate(sid)
    c=environment.containers.items[sid]
    initial=portal.state(portal.meta_for(sid))
    assert initial["player_controls_ready"] is True
    assert c.attrs["Config"]["Env"]
    assert "EXISTING_WHITELIST_FILE=SKIP" in c.attrs["Config"]["Env"]
    assert "EXISTING_OPS_FILE=SKIP" in c.attrs["Config"]["Env"]
    assert "ENABLE_RCON=TRUE" in c.attrs["Config"]["Env"]
    assert not any(key=="25575/tcp" for key in environment.containers.creations[-1]["ports"])

    result=player_call(sid,"whitelist_add","OtherFriend")
    assert result["whitelist"]==["TestPlayer","OtherFriend"]
    result=player_call(sid,"op","OtherFriend")
    assert result["ops"]==["OtherFriend"]
    assert [cmd for cmd in c.commands]==[
        ["rcon-cli","whitelist","add","OtherFriend"],
        ["rcon-cli","op","OtherFriend"]]
    assert player_call(sid,"op","OtherFriend")["ops"]==["OtherFriend"]
    assert len(c.commands)==2
    with pytest.raises(HTTPException,match="Revoke operator"):
        player_call(sid,"whitelist_remove","OtherFriend")
    player_call(sid,"deop","OtherFriend")
    result=player_call(sid,"whitelist_remove","OtherFriend")
    assert result["whitelist"]==["TestPlayer"]
    assert result["ops"]==[]
    assert portal.meta_for(sid)["whitelist"]==["TestPlayer"]
    portal.stop_active(sid)
    portal.activate(sid)
    assert environment.containers.items[sid] is c
    assert portal.state(portal.meta_for(sid))["whitelist"]==["TestPlayer"]


def test_player_management_rejects_invalid_or_inactive_or_unready(environment):
    sid=install_profile(portal.ROOT,21)
    other=install_profile(portal.ROOT,22)
    portal.activate(other)
    with pytest.raises(HTTPException) as exc:
        player_call(sid,"whitelist_add","Friend123")
    assert exc.value.status_code==409

    with pytest.raises(HTTPException) as exc:
        player_call(other,"whitelist_add","invalid.name")
    assert exc.value.status_code==400
    with pytest.raises(HTTPException,match="Whitelist"):
        player_call(other,"op","NotListed")
    assert portal.meta_for(other).get("ops",[])==[]

    old=environment.containers.items[other]
    old.attrs["Config"]["Env"]=["ENABLE_RCON=FALSE","EXISTING_WHITELIST_FILE=MERGE"]
    assert portal.state(portal.meta_for(other))["player_controls_ready"] is False
    with pytest.raises(HTTPException,match="Enable Players"):
        player_call(other,"whitelist_add","Friend123")
    portal.activate(other)  # One-time restart/migration on deliberate activation.
    assert old not in environment.containers.items.values()
    assert portal.state(portal.meta_for(other))["player_controls_ready"] is True
    assert player_call(other,"whitelist_add","Friend123")["whitelist"]==["TestPlayer","Friend123"]


def test_players_do_not_persist_if_console_rejects_change(environment):
    sid=install_profile(portal.ROOT,23)
    portal.activate(sid)
    c=environment.containers.items[sid]
    c.console_ready=False
    with pytest.raises(HTTPException) as exc:
        player_call(sid,"whitelist_add","Another123")
    assert exc.value.status_code==409
    assert portal.meta_for(sid)["whitelist"]==["TestPlayer"]
    assert len(c.commands)==1


def test_players_endpoint_requires_auth_and_validated_actions(environment):
    sid=install_profile(portal.ROOT,24)
    portal.activate(sid)
    with TestClient(portal.app) as http:
        path="/api/servers/"+sid+"/players"
        unauth=http.post(path,json={"action":"op","username":"TestPlayer"})
        assert unauth.status_code==401
        invalid=http.post(path,headers={"Authorization":"Bearer "+portal.TOKEN},
                          json={"action":"arbitrary command","username":"TestPlayer"})
        assert invalid.status_code==422
        valid=http.post(path,headers={"Authorization":"Bearer "+portal.TOKEN},
                        json={"action":"op","username":"TestPlayer"})
        assert valid.status_code==200
        assert valid.json()["ops"]==["TestPlayer"]


def test_ram_change_offline_pending_until_next_activation(environment):
    sid=install_profile(portal.ROOT,31)
    portal.activate(sid)
    first=environment.containers.items[sid]
    portal.stop_active(sid)

    updated=portal.update_ram(sid,portal.RamChange(ram_gb=8),
                              authorization="Bearer "+portal.TOKEN)
    assert updated["ram_gb"]==8
    assert updated["max_ram_gb"]==portal.MAX_RAM
    assert updated["ram_change_pending"] is True
    assert first is environment.containers.items[sid]
    assert first.status=="exited"
    assert portal.meta_for(sid)["ram_gb"]==8

    portal.activate(sid)
    assert first not in environment.containers.items.values()
    new=environment.containers.items[sid]
    assert "MEMORY=8G" in new.attrs["Config"]["Env"]
    assert new.attrs["HostConfig"]["Memory"]==9*1024**3
    assert portal.state(portal.meta_for(sid))["ram_change_pending"] is False
    assert (portal.ROOT/sid/"minecraft"/"server.jar").exists()


def test_ram_change_unstarted_inactive_profile_is_profile_only(environment):
    sid=install_profile(portal.ROOT,32)
    result=portal.update_ram(sid,portal.RamChange(ram_gb=9),
                             authorization="Bearer "+portal.TOKEN)
    assert result["ram_gb"]==9
    assert result["status"]=="not-created"
    assert result["ram_change_pending"] is False
    assert len(environment.containers.creations)==0
    portal.activate(sid)
    assert environment.containers.items[sid].status=="running"
    assert "MEMORY=9G" in environment.containers.items[sid].attrs["Config"]["Env"]


def test_ram_change_live_requires_confirmation_and_recreates_only_game(environment):
    active=install_profile(portal.ROOT,33)
    other=install_profile(portal.ROOT,34)
    portal.activate(active)
    before=environment.containers.items[active]

    with pytest.raises(HTTPException) as exc:
        portal.update_ram(active,portal.RamChange(ram_gb=8),
                          authorization="Bearer "+portal.TOKEN)
    assert exc.value.status_code==409
    assert "Confirm the restart" in exc.value.detail
    assert portal.meta_for(active)["ram_gb"]==6
    assert before.status=="running"
    assert len(environment.containers.creations)==1

    result=portal.update_ram(active,portal.RamChange(ram_gb=8,restart_running=True),
                             authorization="Bearer "+portal.TOKEN)
    assert result["ram_gb"]==8 and result["status"]=="running"
    assert result["is_active"] is True
    assert before not in environment.containers.items.values()
    assert environment.containers.items[active].policy=="unless-stopped"
    assert environment.containers.items[active].attrs["HostConfig"]["Memory"]==9*1024**3
    assert (portal.ROOT/other/"minecraft"/"server.jar").exists()
    assert portal.active_id()==active
    assert len(environment.containers.items)==1


def test_ram_change_rolls_back_after_failed_new_container_start(environment):
    sid=install_profile(portal.ROOT,35)
    portal.activate(sid)
    environment.containers.fail_once_for.add(sid)
    with pytest.raises(HTTPException) as exc:
        portal.update_ram(sid,portal.RamChange(ram_gb=10,restart_running=True),
                          authorization="Bearer "+portal.TOKEN)
    assert exc.value.status_code==409
    assert "previous 6 GB allocation restored" in exc.value.detail
    assert portal.meta_for(sid)["ram_gb"]==6
    restored=environment.containers.items[sid]
    assert restored.status=="running"
    assert "MEMORY=6G" in restored.attrs["Config"]["Env"]
    assert restored.attrs["HostConfig"]["Memory"]==7*1024**3


def test_ram_change_endpoint_requires_auth_and_valid_range(environment,monkeypatch):
    sid=install_profile(portal.ROOT,36)
    with TestClient(portal.app) as http:
        path="/api/servers/"+sid+"/ram"
        assert http.patch(path,json={"ram_gb":8}).status_code==401
        auth={"Authorization":"Bearer "+portal.TOKEN}
        assert http.patch(path,headers=auth,json={"ram_gb":1}).status_code==422
        assert http.patch(path,headers=auth,json={"ram_gb":portal.MAX_RAM+1}).status_code==400
        assert http.patch(path,headers=auth,json={"ram_gb":"not-a-number"}).status_code==422
        assert portal.meta_for(sid)["ram_gb"]==6
        ok=http.patch(path,headers=auth,json={"ram_gb":8})
        assert ok.status_code==200
        assert ok.json()["ram_gb"]==8


def test_ram_change_rejects_unexpected_running_inactive_profile(environment):
    selected=install_profile(portal.ROOT,37)
    other=install_profile(portal.ROOT,38)
    portal.activate(selected)
    # Simulate a legacy background Minecraft process that should not be modified.
    stray=FakeContainer(environment.containers,other,25565,status="running")
    environment.containers.items[other]=stray
    with pytest.raises(HTTPException) as exc:
        portal.update_ram(other,portal.RamChange(ram_gb=8,restart_running=True),
                          authorization="Bearer "+portal.TOKEN)
    assert exc.value.status_code==409
    assert portal.meta_for(other)["ram_gb"]==6
    assert stray.status=="running"


DREGORA_README = """Important Notices:
DREGORA WILL NOT GENERATE CORRECTLY IF level-name IS NOT DregoraRL & level-type IS NOT OTG.
Quick Windows Server Install Guide
Download the Forge 2860 server installer.
level-type=OTG
level-name=DregoraRL
"""

def dregora_pack(prefix="",readme=DREGORA_README,include_config=True):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr(prefix+"SERVER README OR DIE.txt",readme)
        z.writestr(prefix+"mods/terrain.jar",b"mock OTG mod file")
        if include_config:
            z.writestr(prefix+"config/example.cfg",b"configured=true")
        z.writestr(prefix+"start.sh","echo MUST_NOT_EXECUTE")
    return archive.getvalue()


def test_dregora_zip_installs_and_runs_with_exact_forge_settings(environment):
    with TestClient(portal.app) as http:
        result=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Dregora","ram_gb":"8","players":"TestPlayer"},
            files={"pack":("Dregora-server-pack.zip",dregora_pack(),"application/zip")})
    assert result.status_code==200,result.text
    sid=result.json()["id"]
    meta=portal.meta_for(sid)
    assert meta["mode"]=="forge"
    assert meta["minecraft_version"]=="1.12.2"
    assert meta["loader_version"]=="14.23.5.2860"
    assert meta["java_version"]==8
    assert meta["world_name"]=="DregoraRL"
    portal.activate(sid)
    game=environment.containers.creations[-1]
    assert game["environment"]["TYPE"]=="FORGE"
    assert game["environment"]["VERSION"]=="1.12.2"
    assert game["environment"]["FORGE_VERSION"]=="14.23.5.2860"
    for key,val in {
        "LEVEL":"DregoraRL","LEVEL_TYPE":"OTG",
        "ALLOW_FLIGHT":"TRUE","DIFFICULTY":"hard",
        "MAX_TICK_TIME":"-1","ENABLE_COMMAND_BLOCK":"TRUE",
        "VIEW_DISTANCE":"10","MEMORY":"8G"}.items():
        assert game["environment"][key]==val
    assert game["mem_limit"]=="9g"
    assert "server.jar" not in game["environment"].get("CUSTOM_SERVER","")
    assert "start.sh" not in str(game["environment"])
    assert (portal.ROOT/sid/"minecraft"/"mods"/"terrain.jar").exists()
    assert portal.active_id()==sid


def test_dregora_single_directory_wrapper_is_flattened(environment):
    wrapped=dregora_pack(prefix="RLCraft Dregora server pack/")
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Dregora","players":"TestPlayer"},
            files={"pack":("dregora.zip",wrapped,"application/zip")})
    assert response.status_code==200,response.text
    data=portal.ROOT/response.json()["id"]/"minecraft"
    assert (data/"mods"/"terrain.jar").is_file()
    assert not (data/"RLCraft Dregora server pack").exists()


def test_dregora_world_import_uses_otg_level_name(environment):
    world=io.BytesIO()
    with zipfile.ZipFile(world,"w") as z:
        z.writestr("SavedWorld/level.dat",b"existing mock world")
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Dregora world","players":"TestPlayer"},
            files={"pack":("dregora.zip",dregora_pack(),"application/zip"),
                   "world":("world.zip",world.getvalue(),"application/zip")})
    assert response.status_code==200,response.text
    data=portal.ROOT/response.json()["id"]/"minecraft"
    assert (data/"DregoraRL"/"level.dat").read_bytes()==b"existing mock world"
    assert not (data/"world"/"level.dat").exists()


def test_dregora_malformed_pack_is_rejected_without_starting_any_game(environment):
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"broken","players":"TestPlayer"},
            files={"pack":("dregora.zip",dregora_pack(include_config=False),"application/zip")})
    assert response.status_code==400
    assert "Incomplete Dregora server pack" in response.json()["detail"]
    assert not environment.containers.creations

    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"bad version","players":"TestPlayer"},
            files={"pack":("dregora.zip",dregora_pack(readme="Forge 9999.9999"),"application/zip")})
    assert response.status_code==400
    assert "Unrecognized Dregora server README" in response.json()["detail"]


def delete_profile(sid,name,word="DELETE"):
    return portal.delete_server(sid,portal.DeleteServerConfirmation(
        confirm_name=name,confirm_word=word),
        authorization="Bearer "+portal.TOKEN)


def test_delete_unstarted_inactive_profile_removes_its_data_only(environment):
    active=install_profile(portal.ROOT,41)
    target=install_profile(portal.ROOT,42)
    portal.activate(active)
    target_dir=portal.ROOT/target
    (target_dir/"minecraft"/"world").mkdir()
    (target_dir/"minecraft"/"world"/"level.dat").write_bytes(b"delete-only-this-world")
    result=delete_profile(target,"Pack 42")
    assert result=={"deleted":True,"id":target,"name":"Pack 42"}
    assert not target_dir.exists()
    assert (portal.ROOT/active/"minecraft"/"server.jar").exists()
    assert environment.containers.items[active].status=="running"
    assert portal.active_id()==active


def test_delete_stopped_inactive_container_and_world(environment):
    a=install_profile(portal.ROOT,43)
    b=install_profile(portal.ROOT,44)
    portal.activate(a)
    portal.activate(b)
    assert environment.containers.items[a].status=="exited"
    assert environment.containers.items[b].status=="running"
    (portal.ROOT/a/"minecraft"/"world").mkdir()
    (portal.ROOT/a/"minecraft"/"world"/"level.dat").write_bytes(b"old save")
    deleted=delete_profile(a,"Pack 43")
    assert deleted["deleted"] is True
    assert a not in environment.containers.items
    assert not (portal.ROOT/a).exists()
    assert (portal.ROOT/b/"minecraft"/"server.jar").exists()
    assert portal.active_id()==b
    assert environment.containers.items[b].status=="running"


def test_delete_never_removes_selected_profile_even_if_stopped(environment):
    sid=install_profile(portal.ROOT,45)
    portal.activate(sid)
    with pytest.raises(HTTPException) as e:
        delete_profile(sid,"Pack 45")
    assert e.value.status_code==409
    assert environment.containers.items[sid].status=="running"
    portal.stop_active(sid)
    with pytest.raises(HTTPException) as e:
        delete_profile(sid,"Pack 45")
    assert e.value.status_code==409
    assert (portal.ROOT/sid/"minecraft"/"server.jar").is_file()
    assert sid in environment.containers.items


def test_delete_refuses_unexpected_running_inactive_container(environment):
    selected=install_profile(portal.ROOT,46)
    other=install_profile(portal.ROOT,47)
    portal.activate(selected)
    stray=FakeContainer(environment.containers,other,25566,status="running")
    environment.containers.items[other]=stray
    with pytest.raises(HTTPException) as e:
        delete_profile(other,"Pack 47")
    assert e.value.status_code==409
    assert (portal.ROOT/other/"meta.json").is_file()
    assert stray.status=="running"


def test_delete_rejects_wrong_name_and_never_touches_files(environment):
    sid=install_profile(portal.ROOT,48)
    with pytest.raises(HTTPException) as e:
        delete_profile(sid,"pack 48")
    assert e.value.status_code==400
    assert (portal.ROOT/sid/"meta.json").is_file()
    assert (portal.ROOT/sid/"minecraft"/"server.jar").is_file()


def test_delete_api_checks_bearer_token_and_confirmation_literal(environment):
    sid=install_profile(portal.ROOT,49)
    path="/api/servers/"+sid+"/delete"
    headers={"Authorization":"Bearer "+portal.TOKEN}
    with TestClient(portal.app) as http:
        denied=http.post(path,json={"confirm_name":"Pack 49","confirm_word":"DELETE"})
        assert denied.status_code==401
        missing=http.post(path,headers=headers,json={"confirm_name":"Pack 49"})
        assert missing.status_code==422
        wrong_word=http.post(path,headers=headers,json={"confirm_name":"Pack 49","confirm_word":"YES"})
        assert wrong_word.status_code==422
        wrong_name=http.post(path,headers=headers,
            json={"confirm_name":"Pack 48","confirm_word":"DELETE"})
        assert wrong_name.status_code==400
        assert (portal.ROOT/sid/"minecraft"/"server.jar").exists()
        ok=http.post(path,headers=headers,
            json={"confirm_name":"Pack 49","confirm_word":"DELETE"})
        assert ok.status_code==200,ok.text
        assert ok.json()["deleted"] is True
        assert not (portal.ROOT/sid).exists()
        assert http.get("/api/servers",headers=headers).json()==[]


def test_delete_docker_failure_preserves_profile_files(environment,monkeypatch):
    target=install_profile(portal.ROOT,50)
    portal.activate(target)
    other=install_profile(portal.ROOT,51)
    portal.activate(other)
    c=environment.containers.items[target]
    assert c.status=="exited"
    def fail_remove():
        raise docker.errors.APIError("mock Docker permission error")
    monkeypatch.setattr(c,"remove",fail_remove)
    with pytest.raises(HTTPException) as e:
        delete_profile(target,"Pack 50")
    assert e.value.status_code==409
    assert "profile files preserved" in str(e.value.detail)
    assert target in environment.containers.items
    assert (portal.ROOT/target/"minecraft"/"server.jar").exists()
    assert environment.containers.items[other].status=="running"


def test_delete_refuses_bad_directory_identity(environment):
    sid=install_profile(portal.ROOT,52)
    meta=portal.meta_for(sid)
    meta["id"]=f"{999:032x}"
    (portal.ROOT/sid/"meta.json").write_text(json.dumps(meta))
    with pytest.raises(HTTPException) as e:
        delete_profile(sid,"Pack 52")
    assert e.value.status_code==409
    assert (portal.ROOT/sid/"minecraft"/"server.jar").exists()


def save_settings(sid,changes):
    return portal.update_settings(
        sid,portal.SettingsUpdate(properties=changes),
        authorization="Bearer "+portal.TOKEN)


def issue_console(sid,command):
    return portal.run_console(
        sid,portal.ConsoleCommand(command=command),
        authorization="Bearer "+portal.TOKEN)


def test_settings_read_existing_server_properties_instead_of_generic_defaults(environment):
    sid=install_profile(portal.ROOT,61)
    props=portal.ROOT/sid/"minecraft"/"server.properties"
    props.write_text("# Existing modpack settings\npvp=false\ndifficulty=hard\nview-distance=9\nmax-players=18\n")
    state=portal.state(portal.meta_for(sid))
    assert state["server_properties"]["pvp"] is False
    assert state["server_properties"]["difficulty"]=="hard"
    assert state["server_properties"]["view-distance"]==9
    assert state["server_properties"]["max-players"]==18
    assert state["settings_pending"] is False


def test_settings_live_save_does_not_restart_and_apply_on_explicit_restart(environment):
    sid=install_profile(portal.ROOT,62)
    portal.activate(sid)
    old=environment.containers.items[sid]
    profile_dir=portal.ROOT/sid/"minecraft"
    (profile_dir/"world").mkdir()
    (profile_dir/"world"/"level.dat").write_bytes(b"keep this world")

    response=save_settings(sid,{"pvp":False,"difficulty":"hard",
         "gamemode":"creative","view-distance":8,"max-players":13,
         "allow-flight":True,"enable-command-block":True})
    assert response["server_properties"]["pvp"] is False
    assert response["settings_pending"] is True
    assert old.status=="running"
    assert environment.containers.items[sid] is old
    assert len(environment.containers.creations)==1
    assert portal.meta_for(sid)["server_properties"]["max-players"]==13

    result=portal.restart_active(sid)
    assert result["status"]=="running"
    assert result["settings_pending"] is False
    assert environment.containers.items[sid] is not old
    env=environment.containers.creations[-1]["environment"]
    for key,value in {
        "PVP":"FALSE","DIFFICULTY":"hard","MODE":"creative",
        "VIEW_DISTANCE":"8","MAX_PLAYERS":"13","ALLOW_FLIGHT":"TRUE",
        "ENABLE_COMMAND_BLOCK":"TRUE"}.items():
        assert env[key]==value
    assert (profile_dir/"world"/"level.dat").read_bytes()==b"keep this world"
    assert portal.active_id()==sid


def test_offline_settings_save_applies_on_first_start(environment):
    sid=install_profile(portal.ROOT,63)
    resp=save_settings(sid,{"pvp":False,"spawn-monsters":False,
                           "spawn-protection":0,"allow-nether":False})
    assert resp["settings_pending"] is False
    assert resp["status"]=="not-created"
    assert not environment.containers.creations
    portal.activate(sid)
    env=environment.containers.creations[-1]["environment"]
    assert env["PVP"]=="FALSE"
    assert env["SPAWN_MONSTERS"]=="FALSE"
    assert env["SPAWN_PROTECTION"]=="0"
    assert env["ALLOW_NETHER"]=="FALSE"


def test_settings_update_rejects_invalid_fields_and_types_without_partial_save(environment):
    sid=install_profile(portal.ROOT,64)
    for payload in [
        {"online-mode":False}, {"level-name":"newworld"},
        {"pvp":"false"}, {"difficulty":"extreme"},
        {"max-players":101}, {"max-players":0}, {"view-distance":2},
        {"allow-flight":1}, {"spawn-protection":True},
    ]:
        with pytest.raises(HTTPException) as err:
            save_settings(sid,{"pvp":False,**payload})
        assert err.value.status_code==400
        assert "server_properties" not in portal.meta_for(sid)
    assert not environment.containers.creations


def test_dregora_required_world_settings_are_locked_in_backend(environment):
    sid=install_profile(portal.ROOT,65)
    meta=portal.meta_for(sid)
    meta["pack_format"]="rlcraft-dregora"
    meta["mode"]="forge"
    (portal.ROOT/sid/"meta.json").write_text(json.dumps(meta))
    state=portal.state(meta)
    assert state["server_properties"]["difficulty"]=="hard"
    assert state["server_properties"]["allow-flight"] is True
    assert state["server_properties"]["enable-command-block"] is True
    assert state["server_properties"]["view-distance"]==10
    assert set(portal.DREGORA_LOCKED)==set(state["locked_properties"])
    for key in portal.DREGORA_LOCKED:
        with pytest.raises(HTTPException) as err:
            save_settings(sid,{key:"normal"})
        assert err.value.status_code==409
    saved=save_settings(sid,{"pvp":False,"max-players":9})
    assert saved["server_properties"]["max-players"]==9
    assert portal.meta_for(sid)["server_properties"]["pvp"] is False


def test_settings_endpoint_requires_token_and_valid_json(environment):
    sid=install_profile(portal.ROOT,66)
    with TestClient(portal.app) as http:
        path="/api/servers/"+sid+"/settings"
        auth={"Authorization":"Bearer "+portal.TOKEN}
        assert http.patch(path,json={"properties":{"pvp":False}}).status_code==401
        assert http.patch(path,headers=auth,json={"properties":{"pvp":"no"}}).status_code==400
        ok=http.patch(path,headers=auth,json={"properties":{"pvp":False,"difficulty":"normal"}})
        assert ok.status_code==200,ok.text
        assert ok.json()["server_properties"]["pvp"] is False
        assert ok.json()["server_properties"]["difficulty"]=="normal"


def test_console_commands_are_rcon_only_selected_running_server(environment):
    sid=install_profile(portal.ROOT,67)
    other=install_profile(portal.ROOT,68)
    portal.activate(sid)
    c=environment.containers.items[sid]
    response=issue_console(sid,"/time query daytime")
    assert response["command"]=="time query daytime"
    assert "Minecraft response" in response["output"]
    assert c.commands==[["rcon-cli","time query daytime"]]
    assert issue_console(sid,"say hello")["output"]=="Minecraft response: say hello"

    with pytest.raises(HTTPException) as err:
        issue_console(other,"list")
    assert err.value.status_code==409
    portal.stop_active(sid)
    with pytest.raises(HTTPException) as err:
        issue_console(sid,"list")
    assert err.value.status_code==409
    assert len(c.commands)==2


def test_console_rejects_multiline_overlong_and_unready_without_exec(environment):
    sid=install_profile(portal.ROOT,69)
    portal.activate(sid)
    c=environment.containers.items[sid]
    for bad in ["", "/","list\nstop", "say hello\rworld", "a"*257, "hi\x00there"]:
        try:
            with pytest.raises((HTTPException,ValueError)) as err:
                issue_console(sid,bad)
        except AssertionError:
            raise AssertionError("Invalid console command was accepted: "+repr(bad))
    assert not c.commands
    c.console_ready=False
    with pytest.raises(HTTPException) as err:
        issue_console(sid,"list")
    assert err.value.status_code==409
    assert c.commands==[["rcon-cli","list"]]


def test_console_api_requires_auth_and_rejects_incorrect_action(environment):
    sid=install_profile(portal.ROOT,70)
    portal.activate(sid)
    with TestClient(portal.app) as http:
        path="/api/servers/"+sid+"/console"
        headers={"Authorization":"Bearer "+portal.TOKEN}
        assert http.post(path,json={"command":"list"}).status_code==401
        assert http.post(path,headers=headers,json={"command":"list\nstop"}).status_code==400
        ok=http.post(path,headers=headers,json={"command":"list"})
        assert ok.status_code==200,ok.text
        assert ok.json()["output"]=="Minecraft response: list"


def test_settings_changes_survive_profile_switching_and_restart(environment):
    a=install_profile(portal.ROOT,71)
    b=install_profile(portal.ROOT,72)
    portal.activate(a)
    save_settings(a,{"pvp":False})
    portal.activate(b)
    assert environment.containers.items[a].status=="exited"
    portal.activate(a)
    assert environment.containers.creations[-1]["environment"]["PVP"]=="FALSE"
    assert portal.state(portal.meta_for(a))["settings_pending"] is False


def test_legacy_forge_numeric_game_mode_and_difficulty_display(environment):
    sid=install_profile(portal.ROOT,73)
    (portal.ROOT/sid/"minecraft"/"server.properties").write_text(
        "difficulty=3\ngamemode=1\npvp=true\n")
    props=portal.state(portal.meta_for(sid))["server_properties"]
    assert props["difficulty"]=="hard"
    assert props["gamemode"]=="creative"
    assert props["pvp"] is True


def test_ftb_evolution_2_profile_is_pinned_offline_and_uses_java25(environment):
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"FTB Evolution 2","ram_gb":"8","players":"TestPlayer, OtherFriend",
                  "ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
                  "ftb_version_id":"100566","ftb_java_version":"25"})
    assert response.status_code==200,response.text
    result=response.json()
    sid=result["id"]
    meta=portal.meta_for(sid)
    assert result["status"]=="not-created"
    assert meta["mode"]=="ftb"
    assert meta["ftb_modpack_id"]==135
    assert meta["ftb_version_id"]==100566
    assert meta["java_version"]==25
    assert result["ftb_version_id"]==100566
    assert result["ftb_modpack_id"]==135
    assert result["whitelist"]==["TestPlayer","OtherFriend"]
    assert not environment.containers.creations

    portal.activate(sid)
    kwargs=environment.containers.creations[-1]
    assert environment.containers.images[-1]=="itzg/minecraft-server:java25"
    assert kwargs["environment"]["TYPE"]=="FTBA"
    assert kwargs["environment"]["FTB_MODPACK_ID"]=="135"
    assert kwargs["environment"]["FTB_MODPACK_VERSION_ID"]=="100566"
    assert kwargs["environment"]["MEMORY"]=="8G"
    assert kwargs["mem_limit"]=="9g"
    assert "VERSION" not in kwargs["environment"]
    assert "FTB_FORCE_REINSTALL" not in kwargs["environment"]

    # The pinned version must remain unchanged by ordinary restarts.
    portal.restart_active(sid)
    assert environment.containers.images==["itzg/minecraft-server:java25"]
    assert portal.meta_for(sid)["ftb_version_id"]==100566


@pytest.mark.parametrize("url,expected",[
    ("https://feed-the-beast.com/modpacks/135-ftb-evolution-2",135),
    ("https://www.feed-the-beast.com/modpacks/135",135),
    ("https://www.feed-the-beast.com/modpacks/23-ftb-infinity-evolved-17",23),
])
def test_parse_ftb_official_modpack_urls(url,expected):
    assert portal.parse_ftb_url(url)==expected


@pytest.mark.parametrize("bad_url",[
    "http://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
    "https://evil.example/modpacks/135-ftb-evolution-2",
    "https://www.feed-the-beast.com.evil.example/modpacks/135",
    "https://feed-the-beast.com@evil.example/modpacks/135",
    "https://www.feed-the-beast.com/modpacks/135/versions",
    "https://www.feed-the-beast.com/modpacks/135?next=evil",
    "https://www.feed-the-beast.com/modpacks/0",
    "https://www.feed-the-beast.com/other/135",
    "https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2/../../..",
])
def test_invalid_ftb_urls_are_rejected(bad_url):
    with pytest.raises(ValueError):
        portal.parse_ftb_url(bad_url)


@pytest.mark.parametrize("payload",[
    {"ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2"},
    {"ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
     "ftb_version_id":"not-a-number"},
    {"ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
     "ftb_version_id":"0"},
    {"ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
     "ftb_version_id":"100566","ftb_java_version":"22"},
    {"ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
     "ftb_version_id":"100566",
     "cf_url":"https://www.curseforge.com/minecraft/modpacks/some-pack"},
    {"ftb_version_id":"100566","cf_url":"https://www.curseforge.com/minecraft/modpacks/some-pack"},
])
def test_ftb_invalid_source_version_and_java_rejected_without_profile(environment,payload):
    with TestClient(portal.app) as http:
        result=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Invalid FTB","ram_gb":"8","players":"TestPlayer",**payload})
    assert result.status_code==400,result.text
    assert not list(portal.ROOT.glob("*/meta.json"))
    assert not environment.containers.creations


def test_ftb_java_selection_for_older_packs(environment):
    with TestClient(portal.app) as http:
        result=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Old FTB pack","players":"TestPlayer",
                  "ftb_url":"https://www.feed-the-beast.com/modpacks/23-ftb-infinity-evolved-17",
                  "ftb_version_id":"42","ftb_java_version":"8"})
    assert result.status_code==200,result.text
    portal.activate(result.json()["id"])
    assert environment.containers.images[-1]=="itzg/minecraft-server:java8"
    assert environment.containers.creations[-1]["environment"]["FTB_MODPACK_ID"]=="23"


def test_ftb_corrupt_metadata_refuses_switch_and_preserves_meow(environment):
    meow=install_profile(portal.ROOT,80)
    portal.activate(meow)
    sid=install_profile(portal.ROOT,81,valid=False)
    meta=portal.meta_for(sid)
    meta.update({"mode":"ftb","java_version":25,"ftb_modpack_id":135,
                 "ftb_version_id":"invalid"})
    (portal.ROOT/sid/"meta.json").write_text(json.dumps(meta))
    with pytest.raises(HTTPException) as e:
        portal.activate(sid)
    assert e.value.status_code==409
    assert environment.containers.items[meow].status=="running"
    assert portal.active_id()==meow
    assert len(environment.containers.creations)==1


def test_ftb_source_with_world_import_keeps_world_data(environment):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("WorldSave/level.dat",b"imported FTB world")
    with TestClient(portal.app) as http:
        result=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"FTB imported","players":"TestPlayer",
                  "ftb_url":"https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2",
                  "ftb_version_id":"100566","ftb_java_version":"25"},
            files={"world":("world.zip",archive.getvalue(),"application/zip")})
    assert result.status_code==200,result.text
    sid=result.json()["id"]
    assert (portal.ROOT/sid/"minecraft"/"world"/"level.dat").read_bytes()==b"imported FTB world"


def custom_curseforge_export(version="26.3", loader="fabric-0.19.5", mods=None):
    """A minimal authentic-format private CurseForge client export."""
    manifest={
        "minecraft":{"version":version,"modLoaders":[{"id":loader,"primary":True}]},
        "manifestType":"minecraftModpack","manifestVersion":1,
        "name":"Family's Custom Pack","version":"0.1.0","author":"Family",
        "overrides":"overrides",
        "files":mods if mods is not None else [
            {"projectID":606989,"fileID":9018522,"required":True,"isLocked":False},
            {"projectID":699872,"fileID":8920440,"required":True,"isLocked":False},
            {"projectID":809311,"fileID":8919465,"required":False,"isLocked":False},
        ]
    }
    result=io.BytesIO()
    with zipfile.ZipFile(result,"w") as archive:
        archive.writestr("manifest.json",json.dumps(manifest))
        archive.writestr("modlist.html","<html>Custom modlist</html>")
        archive.writestr("overrides/config/custom-settings.json",'{"custom":true}')
        archive.writestr("overrides/shaderpacks/family-shader.txt",b"family shader")
    return result.getvalue()


def test_custom_curseforge_export_keeps_exact_zip_and_runs_native_installer(environment):
    original=custom_curseforge_export()
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Custom Realism","ram_gb":"8","players":"TestPlayer"},
            files={"pack":("Better Realism custom.zip",original,"application/zip")})
    assert response.status_code==200,response.text
    sid=response.json()["id"]
    meta=portal.meta_for(sid)
    assert meta["mode"]=="curseforge_zip"
    assert meta["pack_format"]=="curseforge-client-export"
    assert meta["minecraft_version"]=="26.3"
    assert meta["loader_version"]=="0.19.5"
    assert meta["loader_type"]=="fabric"
    assert meta["java_version"]==25
    assert meta["mod_count"]==3
    assert meta["pack_sha256"]==hashlib.sha256(original).hexdigest()
    data=portal.ROOT/sid/"minecraft"
    assert (data/".mcd"/"curseforge-pack.zip").read_bytes()==original
    assert not (data/"overrides").exists()  # Docker's installer applies overrides.
    assert not environment.containers.creations  # Installation stays offline.
    portal.activate(sid)
    created=environment.containers.creations[-1]
    assert environment.containers.images[-1]=="itzg/minecraft-server:java25"
    assert created["environment"]["TYPE"]=="AUTO_CURSEFORGE"
    assert created["environment"]["CF_SLUG"]=="custom"
    assert created["environment"]["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-pack.zip"
    assert "CF_PAGE_URL" not in created["environment"]
    assert created["environment"]["MEMORY"]=="8G"
    assert created["mem_limit"]=="9g"
    assert "nano_cpus" not in created
    portal.restart_active(sid)
    assert len(environment.containers.creations)==1
    assert (data/".mcd"/"curseforge-pack.zip").read_bytes()==original


def test_custom_curseforge_world_import_preserves_existing_save(environment):
    world=io.BytesIO()
    with zipfile.ZipFile(world,"w") as archive:
        archive.writestr("Autism Inc/level.dat",b"existing level data")
        archive.writestr("Autism Inc/region/r.0.0.mca",b"region data")
        archive.writestr("Autism Inc/playerdata/player.dat",b"player data")
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Existing World","players":"TestPlayer","ram_gb":"8"},
            files={
                "pack":("custom.zip",custom_curseforge_export(),"application/zip"),
                "world":("Autism Inc.zip",world.getvalue(),"application/zip")
            })
    assert response.status_code==200,response.text
    sid=response.json()["id"]
    data=portal.ROOT/sid/"minecraft"
    assert (data/"world"/"level.dat").read_bytes()==b"existing level data"
    assert (data/"world"/"region"/"r.0.0.mca").read_bytes()==b"region data"
    assert (data/"world"/"playerdata"/"player.dat").read_bytes()==b"player data"
    assert len(environment.containers.creations)==0


@pytest.mark.parametrize("version,loader,java",[
    ("1.16.5","forge-36.2.0",8),
    ("1.20.1","fabric-0.15.1",17),
    ("1.20.5","neoforge-20.5.1",21),
    ("1.21.11","fabric-0.18.0",21),
    ("26.3","fabric-0.19.5",25),
])
def test_curseforge_custom_export_selects_matching_runtime(environment,version,loader,java):
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Runtime test","players":"TestPlayer"},
            files={"pack":("custom.zip",custom_curseforge_export(version,loader),"application/zip")})
    assert response.status_code==200,response.text
    sid=response.json()["id"]
    assert portal.meta_for(sid)["java_version"]==java
    portal.activate(sid)
    assert environment.containers.images[-1]==f"itzg/minecraft-server:java{java}"


def test_missing_custom_curseforge_zip_cannot_stop_existing_server(environment):
    current=install_profile(portal.ROOT,121)
    portal.activate(current)
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Future server","players":"TestPlayer"},
            files={"pack":("custom.zip",custom_curseforge_export(),"application/zip")})
    assert response.status_code==200,response.text
    sid=response.json()["id"]
    (portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-pack.zip").unlink()
    with pytest.raises(HTTPException) as error:
        portal.activate(sid)
    assert error.value.status_code==409
    assert portal.active_id()==current
    assert environment.containers.items[current].status=="running"
    assert len(environment.containers.creations)==1


def test_tampered_custom_curseforge_zip_cannot_stop_existing_server(environment):
    current=install_profile(portal.ROOT,122)
    portal.activate(current)
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Future server","players":"TestPlayer"},
            files={"pack":("custom.zip",custom_curseforge_export(),"application/zip")})
    assert response.status_code==200,response.text
    sid=response.json()["id"]
    archive=portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-pack.zip"
    with archive.open("ab") as output:
        output.write(b"tampered")
    with pytest.raises(HTTPException) as error:
        portal.activate(sid)
    assert error.value.status_code==409
    assert "changed after installation" in str(error.value.detail)
    assert environment.containers.items[current].status=="running"


@pytest.mark.parametrize("manifest_patch",[
    {"manifestVersion":2},
    {"minecraft":{"version":"26.3","modLoaders":[{"id":"fabric-0.19.5","primary":False}]}},
    {"files":[{"projectID":1,"fileID":2,"required":False},{"projectID":1,"fileID":3,"required":True}]},
    {"files":[{"projectID":1,"fileID":2,"required":"true"}]},
    {"overrides":"../unsafe"},
])
def test_malformed_custom_curseforge_exports_are_rejected(environment,manifest_patch):
    with zipfile.ZipFile(io.BytesIO(custom_curseforge_export())) as original:
        manifest=json.loads(original.read("manifest.json"))
    manifest.update(manifest_patch)
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as archive:
        archive.writestr("manifest.json",json.dumps(manifest))
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Invalid export","players":"TestPlayer"},
            files={"pack":("broken.zip",out.getvalue(),"application/zip")})
    assert response.status_code==400,response.text
    assert not list(portal.ROOT.glob("*/meta.json"))


def test_custom_curseforge_zip_with_traversal_is_rejected(environment):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as archive:
        with zipfile.ZipFile(io.BytesIO(custom_curseforge_export())) as original:
            archive.writestr("manifest.json",original.read("manifest.json"))
        archive.writestr("overrides/../../minecraft/server.properties","op=true")
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Unsafe export","players":"TestPlayer"},
            files={"pack":("unsafe.zip",out.getvalue(),"application/zip")})
    assert response.status_code==400,response.text
    assert "Unsafe" in response.json()["detail"]
    assert not list(portal.ROOT.glob("*/meta.json"))



def install_custom_mod_profile(environment, version="26.3"):
    with TestClient(portal.app) as http:
        response=http.post("/api/servers",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            data={"name":"Family Modpack","ram_gb":"8","players":"TestPlayer"},
            files={"pack":("custom.zip",custom_curseforge_export(version=version),"application/zip")})
    assert response.status_code==200,response.text
    return response.json()["id"]


def test_mod_patch_preserves_original_zip_and_shows_client_update_notice(environment):
    sid=install_custom_mod_profile(environment)
    location=portal.ROOT/sid/"minecraft"/".mcd"
    original=(location/"curseforge-pack.zip").read_bytes()
    replacement={
        "project_id":606989,"action":"replace","replacement_file_id":9009764,
        "name":"CreativeCore","client_version":"2.14.16","server_version":"2.14.18",
        "reason":"Fix dedicated-server rendering mixin","client_update_required":True
    }
    with TestClient(portal.app) as http:
        headers={"Authorization":"Bearer "+portal.TOKEN}
        before=http.get(f"/api/servers/{sid}/mods",headers=headers)
        assert before.status_code==200,before.text
        assert before.json()["original_mods"]["606989"]==9018522
        response=http.put(f"/api/servers/{sid}/mods",headers=headers,json={"changes":[replacement]})
        assert response.status_code==200,response.text
        assert response.json()["client_updates"][0]["name"]=="CreativeCore"
        assert not environment.containers.creations
        details=http.get(f"/api/servers/{sid}/mods",headers=headers).json()
    assert details["client_updates"][0]["original_file_id"]==9018522
    assert details["client_updates"][0]["required_file_id"]==9009764
    assert details["client_updates"][0]["server_version"]=="2.14.18"
    assert details["restart_required"] is False  # Offline profile, no game to restart.
    assert (location/"curseforge-pack.zip").read_bytes()==original
    effective=location/"curseforge-effective.zip"
    assert portal.meta_for(sid)["effective_sha256"]==hashlib.sha256(effective.read_bytes()).hexdigest()
    with zipfile.ZipFile(effective) as archive:
        manifest=json.loads(archive.read("manifest.json"))
        ids={item["projectID"]:item["fileID"] for item in manifest["files"]}
        assert ids[606989]==9009764
        assert ids[699872]==8920440
        assert archive.read("overrides/config/custom-settings.json")==b'{"custom":true}'
    portal.activate(sid)
    new=environment.containers.creations[-1]["environment"]
    assert new["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-effective.zip"
    assert new["CF_FORCE_SYNCHRONIZE"]=="true"
    assert "CF_EXCLUDE_MODS" not in new
    assert new["MCD_MOD_REVISION"]
    assert environment.containers.items[sid].status=="running"
    assert portal.state(portal.meta_for(sid))["mod_changes_pending"] is False


def test_mod_changes_saved_without_interrupting_running_game_apply_on_restart(environment):
    sid=install_custom_mod_profile(environment)
    portal.activate(sid)
    old=environment.containers.items[sid]
    old_env=environment.containers.creations[-1]["environment"]
    with TestClient(portal.app) as http:
        result=http.put(f"/api/servers/{sid}/mods",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            json={"changes":[{"project_id":606989,"name":"CreativeCore","action":"replace",
                              "replacement_file_id":9009764,
                              "client_version":"2.14.16","server_version":"2.14.18"}]})
        assert result.status_code==200,result.text
        assert result.json()["mod_changes_pending"] is True
        assert result.json()["client_updates"][0]["name"]=="CreativeCore"
    assert old.status=="running"  # No stop/recreate while changing the rules.
    assert environment.containers.items[sid] is old
    assert old_env["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-pack.zip"
    assert len(environment.containers.creations)==1
    portal.restart_active(sid)
    assert old not in environment.containers.items.values()
    assert environment.containers.creations[-1]["environment"]["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-effective.zip"
    assert portal.state(portal.meta_for(sid))["mod_changes_pending"] is False


def test_mod_exclusion_and_user_notice_are_persisted(environment):
    sid=install_custom_mod_profile(environment)
    original=(portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-pack.zip").read_bytes()
    changes=[
        {"project_id":699872,"name":"Client Rendering Mod","action":"exclude",
         "reason":"Not safe on dedicated server","client_update_required":False},
        {"project_id":606989,"name":"CreativeCore","action":"notice",
         "client_version":"2.14.16","server_version":"2.14.18",
         "reason":"The owner already repaired the server ZIP","client_update_required":True},
    ]
    with TestClient(portal.app) as http:
        response=http.put(f"/api/servers/{sid}/mods",json={"changes":changes},
                          headers={"Authorization":"Bearer "+portal.TOKEN})
        assert response.status_code==200,response.text
        assert len(response.json()["client_updates"])==1
        assert response.json()["client_updates"][0]["name"]=="CreativeCore"
    assert (portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-pack.zip").read_bytes()==original
    assert not (portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-effective.zip").exists()
    portal.activate(sid)
    env=environment.containers.creations[-1]["environment"]
    assert env["CF_EXCLUDE_MODS"]=="699872"
    assert env["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-pack.zip"
    assert env["CF_FORCE_SYNCHRONIZE"]=="true"
    assert portal.meta_for(sid)["mod_changes"]==changes


def test_remove_mod_compatibility_rule_restores_source_manifest_on_restart(environment):
    sid=install_custom_mod_profile(environment)
    with TestClient(portal.app) as http:
        auth={"Authorization":"Bearer "+portal.TOKEN}
        assert http.put(f"/api/servers/{sid}/mods",headers=auth,
            json={"changes":[{"project_id":606989,"name":"CreativeCore","action":"replace",
                "replacement_file_id":9009764}]}).status_code==200
        portal.activate(sid)
        old=environment.containers.items[sid]
        restored=http.put(f"/api/servers/{sid}/mods",headers=auth,json={"changes":[]})
        assert restored.status_code==200,restored.text
        assert restored.json()["mod_changes_pending"] is True
        assert restored.json()["client_updates"]==[]
        assert old.status=="running"
    portal.restart_active(sid)
    assert environment.containers.creations[-1]["environment"]["CF_MODPACK_ZIP"]=="/data/.mcd/curseforge-pack.zip"
    assert "CF_EXCLUDE_MODS" not in environment.containers.creations[-1]["environment"]


@pytest.mark.parametrize("change",[
    {"project_id":1234567,"name":"Unknown","action":"replace","replacement_file_id":9009764},
    {"project_id":606989,"name":"Wrong ID","action":"replace","replacement_file_id":0},
    {"project_id":606989,"name":"Oops","action":"replace","replacement_file_id":"9009764"},
    {"project_id":606989,"name":"Oops","action":"exclude","client_update_required":"yes"},
    {"project_id":606989,"name":"Oops","action":"notice","client_update_required":False},
    {"project_id":606989,"name":"Oops","action":"remove"},
])
def test_bad_mod_override_is_rejected_without_changing_saved_profile(environment,change):
    sid=install_custom_mod_profile(environment)
    before=portal.meta_for(sid)
    with TestClient(portal.app) as http:
        response=http.put(f"/api/servers/{sid}/mods",headers={"Authorization":"Bearer "+portal.TOKEN},
                          json={"changes":[change]})
    assert response.status_code==400,response.text
    assert portal.meta_for(sid)==before
    assert not (portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-effective.zip").exists()


def test_mod_compatibility_rejects_duplicate_project_edits(environment):
    sid=install_custom_mod_profile(environment)
    with TestClient(portal.app) as http:
        result=http.put(f"/api/servers/{sid}/mods",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            json={"changes":[{"project_id":606989,"name":"First","action":"exclude"},
                             {"project_id":606989,"name":"Second","action":"notice"}]})
    assert result.status_code==400
    assert "unique project ID" in result.json()["detail"]


def test_mod_compatibility_api_requires_auth_and_custom_export(environment):
    sid=install_custom_mod_profile(environment)
    normal=install_profile(portal.ROOT,123)
    with TestClient(portal.app) as http:
        assert http.get(f"/api/servers/{sid}/mods").status_code==401
        assert http.put(f"/api/servers/{sid}/mods",json={"changes":[]}).status_code==401
        assert http.get(f"/api/servers/{normal}/mods",
                        headers={"Authorization":"Bearer "+portal.TOKEN}).status_code==409


def test_tampered_derived_curseforge_zip_cannot_switch_away_from_playable_server(environment):
    old=install_profile(portal.ROOT,124)
    portal.activate(old)
    sid=install_custom_mod_profile(environment)
    with TestClient(portal.app) as http:
        response=http.put(f"/api/servers/{sid}/mods",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            json={"changes":[{"project_id":606989,"name":"CreativeCore","action":"replace",
                              "replacement_file_id":9009764}]})
        assert response.status_code==200,response.text
    archive=portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-effective.zip"
    with zipfile.ZipFile(archive,"a") as file:
        manifest=json.loads(file.read("manifest.json"))
        manifest["files"][0]["fileID"]=999999
        file.writestr("manifest.json",json.dumps(manifest))
    with pytest.raises(HTTPException) as exc:
        portal.activate(sid)
    assert exc.value.status_code==409
    assert "Effective CurseForge ZIP" in exc.value.detail
    assert environment.containers.items[old].status=="running"
    assert portal.active_id()==old


def test_fabric_client_rendering_mixin_diagnostic_is_actionable_and_not_overconfident():
    from app.mod_compat import diagnostics
    excerpt="""[init] Running as uid=1000 gid=1000
[main/ERROR]: @Mixin target net.minecraft.client.renderer.GameRenderer was not found creativecore.fabric.mixins.json:GameRendererMixin from mod creativecore
[main/ERROR]: A mod crashed on startup!
"""
    issue=diagnostics(excerpt)
    assert issue["kind"]=="missing-client-class"
    assert issue["mod"]=="creativecore"
    assert "client-only" in issue["message"]
    assert "cannot select a safe replacement" in issue["guidance"]
    assert diagnostics(excerpt+"[01:43:07] [Server thread/INFO]: Done (0.409s)! For help, type \"help\"") is None


def test_diagnostic_returns_none_without_evidence_of_mod_failure():
    from app.mod_compat import diagnostics
    assert diagnostics("[init] Running as uid=1000 gid=1000\nModpack ready") is None
    assert diagnostics("[init] Running as uid=1000 gid=1000\nMinecraft server failed.")["kind"]=="startup-failure"



def test_tampered_derived_overrides_are_detected_before_server_switch(environment):
    current=install_profile(portal.ROOT,125)
    portal.activate(current)
    sid=install_custom_mod_profile(environment)
    with TestClient(portal.app) as http:
        response=http.put(f"/api/servers/{sid}/mods",
            headers={"Authorization":"Bearer "+portal.TOKEN},
            json={"changes":[{"project_id":606989,"name":"CreativeCore","action":"replace",
                "replacement_file_id":9009764}]})
    assert response.status_code==200,response.text
    archive=portal.ROOT/sid/"minecraft"/".mcd"/"curseforge-effective.zip"
    with zipfile.ZipFile(archive,"a") as z:
        z.writestr("overrides/config/malicious.sh","echo do-not-run")
    with pytest.raises(HTTPException) as failure:
        portal.activate(sid)
    assert "checksum" in failure.value.detail
    assert environment.containers.items[current].status=="running"
    assert portal.active_id()==current

