"""No Docker daemon required; verify the one-active-container invariant."""
import json
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
        if self.fail_start:
            raise docker.errors.APIError("deliberate start failure")
        # No two Minecraft processes should ever overlap in fake runtime.
        assert not any(c.status=="running" for sid,c in self.owner.items.items() if sid!=self.sid)
        self.status="running"

    def stop(self, timeout):
        assert timeout>=45
        self.status="exited"

    def remove(self):
        assert self.status!="running"
        del self.owner.items[self.sid]

    def exec_run(self,cmd,stdout,stderr):
        self.commands.append(cmd)
        if not self.console_ready:
            return SimpleNamespace(exit_code=1,output=b"RCON connection refused")
        if cmd[0]!="rcon-cli":
            raise AssertionError("Only rcon-cli console commands are permitted")
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
        self.creations=[]

    def get(self, name):
        sid=name.removeprefix("mcd-")
        if sid not in self.items:
            raise docker.errors.NotFound("not found")
        return self.items[sid]

    def create(self, image, **kwargs):
        sid=kwargs["name"].removeprefix("mcd-")
        port=kwargs["ports"]["25565/tcp"]
        c=FakeContainer(self,sid,port,fail_start=sid in self.fail_for)
        c.attrs["Config"]["Env"]=[key+"="+str(val) for key,val in kwargs["environment"].items()]
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
