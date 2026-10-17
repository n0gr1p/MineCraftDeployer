"""No Docker daemon required; verify the one-active-container invariant."""
import json
import os
import tempfile

import docker
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

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
        self.attrs={"HostConfig":{"PortBindings":{"25565/tcp":[{"HostPort":str(port)}]}}}

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
