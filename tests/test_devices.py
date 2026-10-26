"""Device workers (rook.core.devices): a delegate worker announces the devices
its plugins drive as workers of their own and answers calls aimed at them."""

import json

import pytest

from rook.core import authz
from rook.core.devices import Device, clean_device, device_worker_id, valid_device_name
from rook.worker.plugin import Plugin, capability


class Lamp(Device):
    NAMESPACE = "lamp"
    TYPE = "light"
    DRIVER = "demo"

    def __init__(self, key, name):
        super().__init__(key, name, description="demo lamp")
        self.on = False

    def heartbeat(self):
        return {"on": self.on}

    @capability("set", risk="write", tags=("physical",), scope="multi-user")
    async def _set(self, on: bool) -> dict:
        """Switch the lamp."""
        self.on = bool(on)
        return {"on": self.on}

    @capability("status", risk="read", scope="multi-user")
    def _status(self) -> dict:
        """Lamp state."""
        return {"on": self.on, "name": self.name}

    @capability("opts", risk="read", scope="multi-user")
    async def _opts(self) -> dict:
        """The reply options this call runs under."""
        from rook.worker.wire import envelope
        return envelope.current_reply_opts.get()._asdict()


class Lamps(Plugin):
    NAMESPACE = "lamps"

    def __init__(self, names=("desk", "porch")):
        super().__init__()
        self.lamps = [Lamp(n, n) for n in names]

    def devices(self):
        return list(self.lamps)


class _Transport:
    band_id = bytes(16)

    def __init__(self):
        self.sent = []

    async def send(self, data, route=0):
        self.sent.append(json.loads(data))


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from rook.worker import admin, audit, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    w = core.Worker(_Transport(), enabled=["info"], name="hub-box")
    w.lamps = Lamps()
    w.plugins.append(w.lamps)
    return w


async def _call(w, cap, target=None, **args):
    msg = {"id": "m1", "cap": cap, "args": args}
    if target:
        msg["target"] = target
    before = len(w.transport.sent)
    await w._on_message(json.dumps(msg).encode(), ())
    return w.transport.sent[before:]


# -- core --------------------------------------------------------------------

def test_device_ids_are_stable_hex_and_distinct():
    a = device_worker_id("w1", "webos", "office-tv")
    assert a == device_worker_id("w1", "webos", "office-tv")
    assert len(a) == 32 and int(a, 16) >= 0
    assert a != device_worker_id("w2", "webos", "office-tv")
    assert a != device_worker_id("w1", "webos", "den")


def test_device_names_and_blocks_are_checked():
    assert valid_device_name("office-tv") and valid_device_name("Living room 2")
    assert not valid_device_name("") and not valid_device_name("-x") and not valid_device_name("a/b")
    with pytest.raises(ValueError):
        Lamp("k", "bad/name")
    assert clean_device({"type": "tv", "driver": "webos", "model": "OLED55CX",
                         "nested": {"x": 1}, "long": "y" * 100}) == \
        {"type": "tv", "driver": "webos", "model": "OLED55CX"}
    assert clean_device(None) == {"type": "device"}


def test_device_caps_land_in_the_tier_floor():
    table = authz.builtin_table()
    assert table["tv.status"][0] == "read" and table["tv.pair"][0] == "admin"
    assert table["tv.launch"] == ("write", ("physical",))


# -- worker: announce and dispatch --------------------------------------------

@pytest.mark.asyncio
async def test_delegate_announces_its_devices_as_workers(worker):
    await worker.announce()
    own, desk, porch = worker.transport.sent[-3:]
    ids = [device_worker_id(worker.worker_id, "lamps", k) for k in ("desk", "porch")]
    assert own["worker_id"] == worker.worker_id and own["devices"] == ids
    assert [desk["worker_id"], porch["worker_id"]] == ids
    assert desk["kind"] == "announce" and desk["name"] == "desk"
    assert desk["delegate"] == worker.worker_id
    assert desk["device"] == {"type": "light", "driver": "demo"}
    assert desk["features"] == []          # never "route": calls reach the delegate
    assert desk["caps"] == ["caps.describe", "info.ping", "lamp.opts", "lamp.set", "lamp.status"]
    assert desk["tiers"] == {"lamp.opts": "read", "lamp.set": "write", "lamp.status": "read"}
    assert desk["hb"] == {"lamp": {"on": False}}
    assert desk["authz"] == own["authz"] and desk["build"] == own["build"]
    assert "lamp.set" not in own["caps"]   # the delegate does not hold device caps


@pytest.mark.asyncio
async def test_calls_to_a_device_are_answered_in_its_name(worker):
    await worker.announce()
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    [reply] = await _call(worker, "lamp.set", target=desk, on=True)
    assert reply == {"id": "m1", "from": desk, "ok": True, "result": {"on": True}}
    assert worker.lamps.lamps[0].on and not worker.lamps.lamps[1].on
    [reply] = await _call(worker, "lamp.status", target=desk)
    assert reply["result"] == {"on": True, "name": "desk"}
    [reply] = await _call(worker, "caps.describe", target=desk)
    assert set(reply["result"]) == {"caps.describe", "info.ping", "lamp.opts", "lamp.set",
                                    "lamp.status"}
    [reply] = await _call(worker, "shell.exec", target=desk, cmd="id")
    assert reply == {"id": "m1", "from": desk, "ok": False,
                     "error": "unknown capability: shell.exec"}
    # An open call is the delegate's own business, never a device's.
    assert await _call(worker, "lamp.status") == []
    # A device of another delegate is not ours to answer.
    assert await _call(worker, "lamp.status", target="0" * 32) == []


@pytest.mark.asyncio
async def test_device_replies_go_plain_whatever_the_caller_accepts(worker):
    await worker.announce()
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    msg = {"id": "m2", "cap": "lamp.opts", "target": desk, "args": {}, "rr": 77,
           "accept": ["z", "drop", "drop-native"]}
    await worker._on_message(json.dumps(msg).encode(), ())
    assert worker.transport.sent[-1]["result"] == {"route": 77, "z": False, "drop": False,
                                                   "drop_native": False}


@pytest.mark.asyncio
async def test_tickets_are_checked_against_the_device_id(worker, monkeypatch):
    await worker.announce()
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    seen = []
    worker.guard.anchors = ["anchor"]

    def fake_verify(ticket, *, target, **kw):
        seen.append(target)
        return True, "ok"
    monkeypatch.setattr(authz, "verify_ticket", fake_verify)
    msg = {"id": "m9", "cap": "lamp.set", "target": desk, "args": {"on": True},
           "ticket": {"kid": "k", "tier": "write"}}
    await worker._on_message(json.dumps(msg).encode(), ())
    await _call(worker, "info.ping")
    assert seen[0] == desk


@pytest.mark.asyncio
async def test_a_shed_call_to_a_device_gets_a_busy_reply_from_it(worker):
    await worker.announce()
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    worker._on_shed(json.dumps({"id": "s1", "cap": "lamp.set", "target": desk}).encode(), ())
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    reply = worker.transport.sent[-1]
    assert reply["from"] == desk and reply["ok"] is False and reply["error"].startswith("busy")


@pytest.mark.asyncio
async def test_devices_follow_the_plugin(worker):
    await worker.announce()
    worker.lamps.lamps.pop()                 # the plugin stopped driving "porch"
    await worker.announce()
    own = [m for m in worker.transport.sent if m.get("worker_id") == worker.worker_id][-1]
    assert own["devices"] == [device_worker_id(worker.worker_id, "lamps", "desk")]
    worker.plugins.remove(worker.lamps)
    await worker.announce()
    assert "devices" not in worker.transport.sent[-1]
    porch = device_worker_id(worker.worker_id, "lamps", "porch")
    assert await _call(worker, "lamp.status", target=porch) == []


# -- hub ----------------------------------------------------------------------

def _client():
    from rook.band_mcp.client import BandClient
    return BandClient("test-band")


def _announces(worker):
    return [m for m in worker.transport.sent if m.get("kind") == "announce"]


@pytest.mark.asyncio
async def test_hub_keeps_devices_and_drops_unlisted_ones(worker):
    c = _client()
    await worker.announce()
    for m in _announces(worker):
        c._handle_announce(m)
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    porch = device_worker_id(worker.worker_id, "lamps", "porch")
    assert c.workers[desk]["delegate"] == worker.worker_id
    assert c.workers[desk]["device"] == {"type": "light", "driver": "demo"}
    assert c.workers[desk]["hb"] == {"lamp": {"on": False}}
    assert c.workers[worker.worker_id]["delegate"] is None
    worker.lamps.lamps.pop()
    worker.transport.sent.clear()
    await worker.announce()
    c._handle_announce(_announces(worker)[0])      # the delegate's own announce
    assert porch not in c.workers and desk in c.workers


@pytest.mark.asyncio
async def test_calls_to_a_device_are_addressed_to_its_delegate(worker):
    from rook.worker.wire import envelope
    from rook.worker.wire.codec import route_id_for
    c = _client()
    await worker.announce()
    for m in _announces(worker):
        c._handle_announce(m)
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    assert c._delegate_route(c.workers[desk]) == 0          # the delegate declared no route
    c.workers[worker.worker_id]["features"] = [envelope.F_ROUTE]
    c.transport.set_route("hub")
    assert c._delegate_route(c.workers[desk]) == route_id_for(worker.worker_id)
    assert c._delegate_route(c.workers[worker.worker_id]) == 0


@pytest.mark.asyncio
async def test_roster_shows_devices_by_delegate_and_keeps_star(worker):
    from rook.band_mcp import roster
    c = _client()
    await worker.announce()
    for m in _announces(worker):
        c._handle_announce(m)
    rows = {r["name"]: r for r in roster.workers_view(c.workers)}
    assert rows["desk"]["delegate"] == "hub-box"
    assert rows["desk"]["device"] == {"type": "light", "driver": "demo"}
    assert rows["desk"]["hb"] == {"lamp": {"on": False}}
    assert "delegate" not in rows["hub-box"] and "device" not in rows["hub-box"]
    full = {r["name"]: r for r in roster.workers_view(c.workers, fields="all")}
    assert "delegate" not in full["hub-box"] and full["desk"]["delegate"] == "hub-box"
    caps = roster.caps_view(c.workers)
    assert caps["workers"] == 1
    assert caps["caps"]["info.ping"] == "*"                 # devices don't break "*"
    assert caps["caps"]["lamp.set"] == ["desk", "porch"]


def test_devices_are_not_update_members():
    from rook.hub.update_feed import member_kind
    assert member_kind({"worker_id": "d", "name": "tv", "build": 900, "delegate": "w"})["kind"] \
        == "device"
    assert member_kind({"worker_id": "w", "name": "hub-box", "build": 900})["kind"] == "worker"


@pytest.mark.asyncio
async def test_a_device_claim_counts_only_when_the_delegate_lists_it(worker):
    """Review #1: a worker can't make itself an undeauthable 'device' by
    claiming a delegate; the delegate's own announce has to list it."""
    from rook.core.devices import is_device
    c = _client()
    await worker.announce()
    own, desk, porch = _announces(worker)[-3:]
    # A device announce before its delegate's: not linked yet.
    c._handle_announce(desk)
    assert not is_device(c.workers[desk["worker_id"]])
    c._handle_announce(own)                       # the delegate lists it: linked
    assert is_device(c.workers[desk["worker_id"]])
    # An impostor claiming the same delegate stays an ordinary worker.
    c._handle_announce({"kind": "announce", "worker_id": "e" * 32, "name": "sneaky",
                        "caps": ["shell.exec"], "delegate": worker.worker_id,
                        "device": {"type": "tv"}})
    sneaky = c.workers["e" * 32]
    assert not is_device(sneaky) and sneaky["device"] is None
    c._handle_announce({"kind": "announce", "worker_id": "f" * 32, "name": "ghost",
                        "caps": [], "delegate": "0" * 32})     # delegate never seen
    assert not is_device(c.workers["f" * 32])
    c._handle_announce(own)                       # re-announce: the impostor is not pruned
    assert "e" * 32 in c.workers and not is_device(c.workers["e" * 32])


@pytest.mark.asyncio
async def test_a_reloaded_plugin_never_answers_through_its_old_devices(worker):
    """Review #2: calls re-read the table, so a device of a stopped plugin
    object is not used between announces."""
    await worker.announce()
    desk = device_worker_id(worker.worker_id, "lamps", "desk")
    old = worker.lamps.lamps[0]
    fresh = Lamps(("desk",))
    worker.plugins[worker.plugins.index(worker.lamps)] = fresh   # a reload
    [reply] = await _call(worker, "lamp.set", target=desk, on=True)
    assert reply["ok"] and fresh.lamps[0].on and not old.on
    worker.plugins.remove(fresh)                                  # a disable
    assert await _call(worker, "lamp.set", target=desk, on=False) == []


@pytest.mark.asyncio
async def test_one_broken_device_does_not_stop_the_delegate_announcing(worker):
    """Review #3."""
    class Broken(Lamp):
        def caps(self):
            raise RuntimeError("boom")
    worker.lamps.lamps.append(Broken("bad", "bad"))
    await worker.announce()
    own = [m for m in _announces(worker) if m["worker_id"] == worker.worker_id][-1]
    assert len(own["devices"]) == 2
    assert {m["name"] for m in _announces(worker)} >= {"desk", "porch"}


@pytest.mark.asyncio
async def test_devices_leave_with_their_delegate(worker):
    from rook.band_mcp import client as client_mod
    c = _client()
    await worker.announce()
    for m in _announces(worker):
        c._handle_announce(m)
    c.workers[worker.worker_id]["last_seen"] = 0.0      # the delegate went quiet
    for e in c.workers.values():
        if e["worker_id"] != worker.worker_id:
            e["last_seen"] = 10 ** 12                    # its devices look fresh
    import asyncio
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 1:
            raise asyncio.CancelledError
    orig = client_mod.asyncio.sleep
    client_mod.asyncio.sleep = fake_sleep
    try:
        await c._gc_loop()
    finally:
        client_mod.asyncio.sleep = orig
    assert c.workers == {}
