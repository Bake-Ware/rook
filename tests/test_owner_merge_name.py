"""A worker's owner changed (W6), two bands merged (P4, B1, B2), and the hub's name
("Hubs have names"; P4 Settings, hub name): through the hub, the hub cap, the MCP
door and the site's API over real HTTP on localhost, as the browser drives it.

Workers are in-memory links, as in test_site."""

import asyncio
import pathlib

import pytest

from helpers import Clock, eventually
from rook_hub import Hub, bands, devices, journal
from rook_hub.errors import HubError
from test_door import mcp_client, tool
from test_site import Box, ask, browser, by_band, login

HOME, OFFICE, SHED, CAFE = "02:aa:00:00:00:01", "02:bb:00:00:00:02", "02:cc:00:00:00:03", "02:dd:00:00:00:04"
LAMP = pathlib.Path(__file__).parent / "plugins" / "lamp"


def owners(view):
    """{worker name: owner name} from GET /api/workers."""
    return {w["name"]: w["owner"]["name"] for g in view["groups"] for w in g["workers"]}


def band_changes(hub):
    return [e["result"]["payload"] for e in reversed(journal.read(hub, kind="event"))
            if e["result"]["event"] == "band.change"]


def test_owner_reassigned(tmp_path):
    asyncio.run(owner_scenario(tmp_path))


async def owner_scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    bob = hub.auth.create_user("Bob", "battery staple")
    carol = hub.auth.create_user("Carol", "tr0ub4dor")
    desk = Box(hub, "desk", alice["guid"])
    await desk.connect(HOME)
    agent = hub.auth.mint("agent", user=alice["guid"])
    try:
        # 1. The hub function: Bob owns the desk now. Journaled with who did it (L1, W6).
        record = hub.reassign(desk.moniker, bob["moniker"], user=alice["guid"])
        assert record["owner"] == bob["guid"] and hub.get_worker(desk.guid)["owner"] == bob["guid"]
        [entry] = journal.read(hub, kind="auth", worker=desk.guid, text="owner_changed")
        assert (entry["user"], entry["result"]) == (
            alice["moniker"], {"owner_changed": desk.guid, "from": alice["guid"], "to": bob["guid"]})

        # 2. The hub cap, as an agent through the MCP door: Carol owns it. One call in the
        #    journal under the agent's token, and the change itself (L1).
        async with mcp_client(f"{base}/mcp", agent) as session:
            failed, caps = await tool(session, "caps", worker="hub")
            assert "worker.reassign" in [c["name"] for c in caps["caps"][0]["caps"]]
            failed, reply = await tool(session, "call", worker="hub", cap="worker.reassign",
                                       args={"worker": desk.moniker, "owner": carol["moniker"]},
                                       why="Carol looks after the desk now")
        assert not failed and reply == {"worker": {"guid": desk.guid, "moniker": desk.moniker, "name": "desk"},
                                        "owner": {"moniker": carol["moniker"], "name": "Carol"}}
        assert hub.get_worker(desk.guid)["owner"] == carol["guid"]
        [call] = journal.read(hub, kind="call", cap="worker.reassign")
        assert (call["why"], call["token"]) == ("Carol looks after the desk now", hub.auth.verify(agent)["moniker"])

        #    An unknown user or worker is refused, saying what to do; nothing changes.
        for args, code in (({"worker": desk.moniker, "owner": "nobody"}, "unknown_user"),
                           ({"worker": "no-such-box", "owner": bob["moniker"]}, "unknown_worker"),
                           ({"worker": desk.moniker}, "bad_message")):
            reply = await hub.call("hub", "worker.reassign", args, "try", token=agent)
            assert reply["type"] == "error" and reply["code"] == code and reply["next"], args
        assert hub.get_worker(desk.guid)["owner"] == carol["guid"]

        async with browser() as client:
            # 3. Without a session the site refuses (T4).
            status, reply = await ask(client, "POST", f"{base}/api/workers/{desk.moniker}/owner",
                                      {"owner": alice["moniker"]})
            assert status == 401 and reply["code"] == "no_session"

            # 4. Logged in, the Workers page lists the users to choose from and shows the owner on
            #    the row; the row's action gives it back to Alice, through the hub cap (P3, L1).
            assert (await login(client, base, "alice", "correct horse"))[0] == 200
            status, view = await ask(client, "GET", base + "/api/workers")
            assert owners(view) == {"desk": "Carol"}
            assert [u["name"] for u in view["users"]] == ["Alice", "Bob", "Carol"]
            status, reply = await ask(client, "POST", f"{base}/api/workers/{desk.moniker}/owner",
                                      {"owner": alice["moniker"]})
            assert status == 200 and reply["owner"]["name"] == "Alice"
            status, view = await ask(client, "GET", base + "/api/workers")
            assert owners(view) == {"desk": "Alice"}
            assert journal.read(hub, kind="call", cap="worker.reassign")[0]["why"] == "Owner changed on the Workers page."

            status, reply = await ask(client, "POST", f"{base}/api/workers/{desk.moniker}/owner", {"owner": "x"})
            assert status == 400 and reply["code"] == "unknown_user"

        # 5. Owner is a fact, not a gate (W6): Bob, who no longer owns the desk, still calls it.
        reply = await hub.call(desk.guid, "shell.open", {}, "not the owner", user=bob["guid"])
        assert reply["type"] == "stream_open"
    finally:
        await desk.stop()
        await hub.stop_serving()
        hub.close()


def test_bands_merged(tmp_path):
    asyncio.run(merge_scenario(tmp_path))


async def merge_scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    boxes = {name: Box(hub, name, alice["guid"]) for name in ("desk", "nas", "shop", "phone", "late")}
    home = bands.create(hub, "Home", HOME, user=alice["guid"])
    bands.add_gateway(hub, home["moniker"], SHED, user=alice["guid"])
    office = bands.create(hub, "Office", OFFICE, user=alice["guid"])
    cafe = bands.create(hub, "Cafe", CAFE, user=alice["guid"])
    for name, gateway in (("desk", HOME), ("nas", SHED), ("shop", OFFICE), ("phone", None)):
        await boxes[name].connect(gateway)
    hub.plugins.install(str(LAMP), {"user": alice["guid"], "token": None})
    added = await hub.call("hub", "device.add", {"name": "Hall lamp", "driver": "lamp", "route": "floating",
                                                 "band": home["moniker"], "config": {"url": "http://127.0.0.1:9"}},
                           "a lamp on Home", user=alice["guid"])
    assert added["type"] == "result"
    try:
        # 1. Home merges into Office through the hub: Office holds all three gateways, its
        #    name, GUID and moniker stay; Home is gone (P4, B2).
        merged = bands.merge(hub, home["moniker"], office["moniker"], user=alice["guid"])
        assert (merged["guid"], merged["moniker"], merged["name"]) == (office["guid"], office["moniker"], "Office")
        assert sorted(merged["gateways"]) == sorted([OFFICE, HOME, SHED])
        assert [b["name"] for b in bands.list_bands(hub)] == ["Office", "Cafe"]
        with pytest.raises(HubError) as gone:
            bands.get(hub, home["moniker"])
        assert gone.value.message["code"] == "unknown_band"

        # 2. The workers on Home's gateways are in Office (B1), each with a band.change (J2);
        #    the device on Home moved with them (D1).
        names = {name: (bands.of_worker(hub, box.guid) or {}).get("name") for name, box in boxes.items()}
        assert names == {"desk": "Office", "nas": "Office", "shop": "Office", "phone": None, "late": None}
        await eventually(lambda: len(band_changes(hub)) >= 2)
        moved = {c["name"]: (c["from"]["name"], c["to"]["name"]) for c in band_changes(hub) if c["from"]}
        assert moved == {"desk": ("Home", "Office"), "nas": ("Home", "Office")}
        [lamp] = devices.list_devices(hub)
        assert lamp["band"]["guid"] == office["guid"]

        # 3. A worker that turns up on a gateway that was Home's lands in Office.
        await boxes["late"].connect(SHED)
        assert bands.of_worker(hub, boxes["late"].guid)["name"] == "Office"

        # 4. Journaled with who did it (L1).
        [entry] = journal.read(hub, kind="band", text="band_merged")
        assert (entry["user"], entry["result"]) == (alice["moniker"], {
            "band_merged": home["guid"], "name": "Home", "into": office["guid"], "gateways": [HOME, SHED]})

        #    A band cannot merge into itself, nor into a band that does not exist; nothing changes.
        for band, into, code in ((office["moniker"], office["guid"], "bad_message"),
                                 (office["moniker"], "no-such-band", "unknown_band"),
                                 (home["moniker"], cafe["moniker"], "unknown_band")):
            with pytest.raises(HubError) as refused:
                bands.merge(hub, band, into, user=alice["guid"])
            assert refused.value.message["code"] == code and refused.value.message["next"]
        assert [b["name"] for b in bands.list_bands(hub)] == ["Office", "Cafe"]

        async with browser() as client:
            # 5. Without a session the site refuses (T4).
            status, reply = await ask(client, "POST", f"{base}/api/bands/{cafe['moniker']}/merge",
                                      {"into": office["moniker"]})
            assert status == 401 and reply["code"] == "no_session"

            # 6. Logged in, the group header's merge: Office joins Cafe. Every worker on
            #    Office's gateways is under Cafe's name; Office is gone from the page.
            assert (await login(client, base, "alice", "correct horse"))[0] == 200
            status, reply = await ask(client, "POST", f"{base}/api/bands/{office['moniker']}/merge",
                                      {"into": cafe["moniker"]})
            assert status == 200 and reply["name"] == "Cafe" and sorted(reply["gateways"]) == sorted(
                [CAFE, OFFICE, HOME, SHED])
            status, view = await ask(client, "GET", base + "/api/workers")
            assert {k: sorted(v) for k, v in by_band(view).items()} == {
                "Cafe": ["desk", "late", "nas", "shop"], "roaming": ["phone"]}
            assert [g["band"]["name"] for g in view["groups"] if g["band"]] == ["Cafe"]
            [entry, _] = journal.read(hub, kind="band", text="band_merged")
            assert entry["token"] is not None    # the site acts through the person's session (T4)

            status, reply = await ask(client, "POST", f"{base}/api/bands/{cafe['moniker']}/merge", {"into": ""})
            assert status == 400 and reply["code"] == "bad_message"
    finally:
        for box in boxes.values():
            if hasattr(box, "link"):
                await box.stop()
        await hub.stop_serving()
        hub.close()


def test_hub_name(tmp_path):
    asyncio.run(name_scenario(tmp_path))


async def name_scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    alice = hub.auth.create_user("Alice", "correct horse")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    try:
        async with browser() as client:
            # 1. Without a session the name is neither read nor set (T4).
            for method, path, body in (("GET", "/api/hub", None), ("POST", "/api/hub/name", {"name": "Home"})):
                status, reply = await ask(client, method, base + path, body)
                assert status == 401 and reply["code"] == "no_session"

            # 2. A new hub has no name. Settings has a Hub section, first, as P4 lists hub name first.
            assert (await login(client, base, "alice", "correct horse"))[0] == 200
            assert (await ask(client, "GET", base + "/api/hub")) == (200, {"name": None})
            assert (await ask(client, "GET", base + "/api/session"))[1]["hub"] == {"name": None}
            status, settings = await ask(client, "GET", base + "/api/settings")
            assert [s["id"] for s in settings["sections"]][:2] == ["hub", "plugins"]
            async with client.get(base + "/site/sections/hub.js") as response:
                assert response.status == 200

            # 3. Set in Settings, it is served to the site with the session (the sidebar and tab).
            assert (await ask(client, "POST", base + "/api/hub/name", {"name": "  Bakeforge  "})) == (
                200, {"name": "Bakeforge"})
            assert (await ask(client, "GET", base + "/api/hub")) == (200, {"name": "Bakeforge"})
            assert (await ask(client, "GET", base + "/api/session"))[1]["hub"] == {"name": "Bakeforge"}
            status, reply = await ask(client, "POST", base + "/api/hub/name", {"name": " "})
            assert status == 400 and reply["code"] == "bad_message"

        # 4. Journaled with who did it (L1).
        [entry] = journal.read(hub, kind="auth", text="hub_named")
        assert (entry["user"], entry["result"]) == (alice["moniker"], {"hub_named": "Bakeforge", "from": None})
        hub.set_name("Forge", user=alice["guid"])
        assert journal.read(hub, kind="auth", text="hub_named")[0]["result"] == {"hub_named": "Forge",
                                                                                "from": "Bakeforge"}
    finally:
        await hub.stop_serving()
        hub.close()

    # 5. Kept across a restart: a new Hub on the same data directory has the name.
    again = Hub(tmp_path / "hub", clock=Clock())
    try:
        assert again.name() == "Forge"
    finally:
        again.close()
