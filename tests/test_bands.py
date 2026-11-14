"""Bands (B1-B6): the brief's eight-step scenario, end to end.

Heartbeats travel over in-memory links to the hub; the agent in step 7 uses
the real MCP door over HTTP."""

import asyncio

import pytest
from nacl.signing import SigningKey

from helpers import Clock, link_fields
from memory_link import link_pair
from rook_hub import Hub, bands, journal
from rook_hub.errors import HubError
from rook_protocol import decode, encode
from test_door import mcp_client, tool

HOME_GW, CAFE_GW, HOTEL_GW = "02:aa:00:00:00:01", "02:bb:00:00:00:02", "02:cc:00:00:00:03"


class Box:
    """An approved worker on an in-memory link, heartbeating whatever gateway MAC it is given."""

    def __init__(self, hub, name, owner):
        self.hub, self.name = hub, name
        signing_key = SigningKey.generate()
        self.key = signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        self.guid = hub.approve(self.key, user=owner)["guid"]
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(hub.serve_link(hub_end))

    async def heartbeat(self, gateway_mac):
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python",
                      "version": "0.1.0", "macs": []},
            "caps": [], "band": {"gateway_mac": gateway_mac}, "health": {}}))
        assert decode(await self.link.recv())["type"] == "heartbeat_reply"

    async def stop(self):
        self.link.close()
        await self.serving


def band_names(hub, *boxes):
    """Each box's band name, or None when it is roaming."""
    return [(bands.of_worker(hub, box.guid) or {}).get("name") for box in boxes]


def unrecognised(hub):
    return {entry["gateway_mac"]: sorted(w["name"] for w in entry["workers"]) for entry in bands.unrecognised(hub)}


def test_bands(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    bake = hub.auth.create_user("Bake", "a password")
    agent = hub.auth.mint("agent", user=bake["guid"])
    a, b, c = (Box(hub, name, bake["guid"]) for name in ("desk", "nas", "laptop"))
    try:
        # 1. Two workers on one gateway, a third on another. No bands: all roaming,
        #    and both gateways are listed with the workers on each (B2, B3).
        await a.heartbeat(HOME_GW)
        await b.heartbeat(HOME_GW.upper())          # MACs are compared in lowercase
        await c.heartbeat(CAFE_GW)
        assert bands.list_bands(hub) == []
        assert band_names(hub, a, b, c) == [None, None, None]
        assert unrecognised(hub) == {HOME_GW: ["desk", "nas"], CAFE_GW: ["laptop"]}

        # 2. A user names the first gateway "Home". The two workers on it are in Home
        #    at once; the third is still roaming (B1, B2, G1).
        home = bands.create(hub, "Home", HOME_GW, user=bake["guid"])
        assert (home["name"], home["gateways"], len(home["moniker"].split("-"))) == ("Home", [HOME_GW], 3)
        assert bands.get(hub, home["moniker"]) == bands.get(hub, home["guid"]) == home
        assert band_names(hub, a, b, c) == ["Home", "Home", None]
        assert unrecognised(hub) == {CAFE_GW: ["laptop"]}

        #    A gateway belongs to one band, so a worker is in one band at a time (B6).
        with pytest.raises(HubError) as taken:
            bands.create(hub, "Also home", HOME_GW, user=bake["guid"])
        assert taken.value.message["code"] == "gateway_taken" and taken.value.message["next"]
        assert len(bands.list_bands(hub)) == 1

        # 3. The third worker's gateway is added to Home: all three are in Home,
        #    and nothing is unrecognised. An agent does it, through its token.
        home = bands.add_gateway(hub, home["moniker"], CAFE_GW, token=agent)
        assert home["gateways"] == [HOME_GW, CAFE_GW]
        assert band_names(hub, a, b, c) == ["Home", "Home", "Home"]
        assert unrecognised(hub) == {}

        # 4. A worker's next heartbeat carries a gateway nobody has named: it is
        #    roaming at once, and no band was made for it (B2, B3).
        await c.heartbeat(HOTEL_GW)
        assert band_names(hub, a, b, c) == ["Home", "Home", None]
        assert unrecognised(hub) == {HOTEL_GW: ["laptop"]}
        assert [band["guid"] for band in bands.list_bands(hub)] == [home["guid"]]

        # 5. A worker with no gateway MAC (cellular) is roaming, and is not listed
        #    as an unrecognised network.
        await b.heartbeat(None)
        assert band_names(hub, a, b, c) == ["Home", None, None]
        assert unrecognised(hub) == {HOTEL_GW: ["laptop"]}

        # 6. Home is renamed. Its workers follow the new name; its GUID and moniker stay (G1).
        renamed = bands.rename(hub, home["guid"], "House", user=bake["guid"])
        assert (renamed["guid"], renamed["moniker"], renamed["gateways"]) == (
            home["guid"], home["moniker"], home["gateways"])
        assert band_names(hub, a, b, c) == ["House", None, None]

        #    Bands carry no permissions (B4): a roaming worker is reached like any other.
        reply = await hub.call(b.guid, "demo.echo", {}, "reach a roaming worker", user=bake["guid"])
        assert reply["code"] == "unknown_cap"           # refused for the cap it lacks, not for its band

        # 7. An agent listing workers through the MCP door sees each one's band or
        #    roaming state (B5).
        host, port = await hub.serve("127.0.0.1", 0)
        try:
            async with mcp_client(f"http://{host}:{port}/mcp", agent) as session:
                failed, listing = await tool(session, "workers")
        finally:
            await hub.stop_serving()
        assert not failed
        seen = {w["name"]: (w["band"], w["roaming"], w["gateway_mac"]) for w in listing["workers"]}
        house = {"guid": home["guid"], "moniker": home["moniker"], "name": "House"}
        assert seen == {"desk": (house, False, HOME_GW), "nas": (None, True, None),
                        "laptop": (None, True, HOTEL_GW)}

        # 8. The journal holds the create, the add and the rename, each with who did it (L1).
        token_moniker = hub.auth.verify(agent)["moniker"]
        entries = journal.read(hub, kind="band")
        assert [(e["user"], e["token"], e["worker"], e["result"]) for e in reversed(entries)] == [
            (bake["moniker"], None, None, {"band_created": home["guid"], "name": "Home", "gateway_mac": HOME_GW}),
            (bake["moniker"], token_moniker, None, {"gateway_added": CAFE_GW, "band": home["guid"]}),
            (bake["moniker"], None, None, {"band_renamed": home["guid"], "from": "Home", "to": "House"}),
        ]
    finally:
        for box in (a, b, c):
            await box.stop()
        hub.close()


def test_band_errors(tmp_path):
    # Each refusal says what to try next (C3), and nothing is written.
    hub = Hub(tmp_path / "hub")
    try:
        bake = hub.auth.create_user("Bake", "a password")["guid"]
        for call, code in ((lambda: bands.create(hub, "", HOME_GW, user=bake), "bad_message"),
                           (lambda: bands.create(hub, "Home", None, user=bake), "bad_message"),
                           (lambda: bands.add_gateway(hub, "no-such-band", HOME_GW, user=bake), "unknown_band"),
                           (lambda: bands.rename(hub, "no-such-band", "Home", user=bake), "unknown_band"),
                           (lambda: bands.create(hub, "Home", HOME_GW), "no_actor")):
            with pytest.raises(HubError) as refused:
                call()
            assert refused.value.message["code"] == code and refused.value.message["next"]
        assert bands.list_bands(hub) == [] and journal.read(hub, kind="band") == []
    finally:
        hub.close()
