"""Setup state through a real hub (brief HA3): agent.setup and agent.look as the agents plugin serves
them (hub_setup.py beside HA2's hub.py), the first load that tells a new hub from one made before
setup existed, and the first-run skill carried on the house agent's turns while setup is unfinished.

A hub made by an older core is made as tests/test_default_plugins.py makes one: with the shipped
defaults that core had (no agents plugin), then started with today's."""

import asyncio
import json
import shutil

from rook_hub import Hub, plugins
from rook_hub.plugins import DEFAULTS


async def hub_cap(hub, cap, args, user):
    reply = await hub.call("hub", cap, args, "The wizard test checks.", user=user)
    assert reply["type"] == "result", reply
    return reply["result"]


def test_a_new_hub_starts_in_setup_and_done_ends_it(tmp_path):
    asyncio.run(new_hub(tmp_path))


async def new_hub(tmp_path):
    hub = Hub(tmp_path / "hub")
    owner = hub.auth.open_until_claimed("bake")['guid']       # after the first load, as --no-password does
    assert await hub_cap(hub, "agent.setup", {}, owner) == {"done": False}
    site = await hub_cap(hub, "agent.site", {}, owner)
    assert site["setup"] == {"done": False} and site["look"]["next"] == "sound"
    hub.close()
    hub = Hub(tmp_path / "hub")                                # a restart keeps it unfinished
    assert await hub_cap(hub, "agent.setup", {}, owner) == {"done": False}
    hub.auth.change_password(owner, "correct horse", user=owner)            # step 5: still unfinished
    assert await hub_cap(hub, "agent.setup", {}, owner) == {"done": False}
    assert await hub_cap(hub, "agent.setup", {"done": True}, owner) == {"done": True}
    hub.close()
    hub = Hub(tmp_path / "hub")
    assert (await hub_cap(hub, "agent.site", {}, owner))["setup"] == {"done": True}
    assert await hub_cap(hub, "agent.setup", {"done": False}, owner) == {"done": False}   # and again
    hub.close()


def test_a_hub_made_before_setup_comes_up_done(tmp_path, monkeypatch):
    """The beta's case: a hub with people, made by a core with no agents plugin."""
    asyncio.run(made_before(tmp_path, monkeypatch))


async def made_before(tmp_path, monkeypatch):
    old = tmp_path / "old-defaults"
    for folder in DEFAULTS.iterdir():
        if folder.is_dir() and not folder.name.startswith("agents"):
            shutil.copytree(folder, old / folder.name)
    monkeypatch.setattr(plugins, "DEFAULTS", old)
    hub = Hub(tmp_path / "hub")
    alice = hub.auth.create_user("Alice", "correct horse", first=True)["guid"]
    assert "agents" not in [p["name"] for p in hub.plugins.list()]
    hub.close()
    monkeypatch.setattr(plugins, "DEFAULTS", DEFAULTS)          # today's core
    hub = Hub(tmp_path / "hub")
    assert "agents" in [p["name"] for p in hub.plugins.list()]
    assert await hub_cap(hub, "agent.setup", {}, alice) == {"done": True}
    assert (await hub_cap(hub, "agent.site", {}, alice))["setup"] == {"done": True}
    assert hub.auth.unclaimed_owner() is None
    hub.close()
    hub = Hub(tmp_path / "hub")
    assert await hub_cap(hub, "agent.setup", {}, alice) == {"done": True}
    hub.close()


def test_install_facts_and_the_look(tmp_path):
    asyncio.run(facts_and_look(tmp_path))


async def facts_and_look(tmp_path):
    hub = Hub(tmp_path / "hub")
    facts = {"by": "deploy/install/install.sh", "folder": "/opt/rook", "http": "0.0.0.0:8740 (every interface)",
             "units": ["rook-hub", "rook-worker"], "vault_key": "/opt/rook/vault.key"}
    (tmp_path / "hub" / "install.json").write_text(json.dumps(facts))
    alice = hub.auth.open_until_claimed("alice")["guid"]
    bob = hub.auth.create_user("Bob", "battery staple")["guid"]
    assert await hub_cap(hub, "agent.setup", {}, alice) == {"done": False, "install": facts}
    look = await hub_cap(hub, "agent.look", {}, alice)
    assert look["next"] == "sound" and "kawaii" in look["styles"] and "none" in look["styles"] and "rook" in look["themes"]
    await hub_cap(hub, "agent.look", {"sound": True, "rookie": "kawaii", "theme": "rook", "name": "Jeeves"}, alice)
    mine, his = await hub_cap(hub, "agent.look", {}, alice), await hub_cap(hub, "agent.look", {}, bob)
    assert (mine["sound"], mine["rookie"], mine["theme"], mine["next"]) == (True, "kawaii", "rook", None)
    assert (his["sound"], his["rookie"], his["next"]) == (False, "pixel", "sound") and his["name"] == "Jeeves"
    assert (await hub_cap(hub, "agent.site", {}, alice))["look"]["name"] == "Jeeves"     # the dock's name
    hub.close()


def test_the_first_run_skill_rides_every_house_turn_until_setup_is_done(tmp_path):
    asyncio.run(carried(tmp_path))


async def carried(tmp_path):
    from rook_hub.library import Call
    hub = Hub(tmp_path / "hub")
    alice = hub.auth.open_until_claimed("alice")["guid"]
    call = Call(hub, "agents", {"user": alice, "token": None}, alice, None)
    [(name, lead, path)] = await call.carried()
    assert name == "first-run" and path.name == "first-run.md"
    assert lead == ("Setup isn't finished: you are walking this person through first-run setup. Carry on from the "
                    "first step not done yet. The next look-and-feel question is: sound.")
    await hub_cap(hub, "agent.look", {"sound": False, "rookie": "pixel", "theme": "stone", "name": "Rookie"}, alice)
    [(_, lead, _)] = await call.carried()
    assert lead.endswith("Carry on from the first step not done yet.")
    await hub_cap(hub, "agent.setup", {"done": True}, alice)
    assert await call.carried() == []
    hub.close()
