"""Worker events reach the hub (gate R2; spec 13 ``core.event``): the
worker's bounded outbox (rook/core2/worker/events.py), the real worker and
hub runtimes on the loopback relay, the checks the hub makes before an
event goes to hooks, jobs and pages, and the taint it carries. Every test
has a hard timeout."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.hub.identity import Caller as HubCaller  # noqa: E402
from rook.core2.hub.runtime import Config as HubConfig, Hub  # noqa: E402
from rook.core2.transport import LoopbackRelay, Transport, band_of  # noqa: E402
from rook.core2.worker import events  # noqa: E402
from rook.core2.worker.runtime import Config, Worker  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402

PSK = "events-band"
BAND = band_of(PSK)
WID = "5e0a" * 8
ROOT, OP = SigningKey(b"\x41" * 32), SigningKey(b"\x42" * 32)
HOME = wire.key_id(bytes(ROOT.verify_key))
WHY = "Testing worker events."

PHONE_YAML = """manifest: 1
plugin: phone
version: 1.0.0
caps:
  receive:
    doc: Pretend a text arrived.
    risk: write
    scope: self
    args: { from: { type: str }, body: { type: str } }
    run: scripts/phone.py:receive
emits:
  phone.sms.received:
    doc: A text arrived.
    taints: true
    payload: { from: { type: str, required: true }, body: { type: str } }
  phone.moved:
    doc: The phone moved.
    payload: { at: { type: float } }
"""

PHONE_PY = """
def receive(ctx, **text):
    ctx.emit("phone.sms.received", text)
    return {"ok": True}
"""

# A hub plugin that reacts to the worker's events, as jobs or attention would.
LISTEN_YAML = """manifest: 1
plugin: listen
version: 1.0.0
runs_on: hub
uses: [phone.receive]
caps:
  heard:
    doc: What the hooks heard.
    risk: read
    scope: self
    run: scripts/listen.py:heard
hooks:
  on: [ { event: "phone.*", run: scripts/listen.py:on_phone } ]
"""

LISTEN_PY = """
HEARD = []

async def on_phone(ctx, event, payload):
    HEARD.append({"event": event, "payload": payload, "tainted": ctx.caller.tainted})

def heard(ctx):
    return HEARD
"""


def go(body, seconds=30):
    async def main():
        return await asyncio.wait_for(body(), seconds)
    return asyncio.run(main())


async def until(cond, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not (got := cond()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
    return got


# -- the outbox -----------------------------------------------------------------

def _ev(i: int) -> dict:
    return events.make("x.tick", "x", {"i": i}, 8)


def test_outbox_is_bounded_and_counts_what_it_dropped():
    async def body():
        sent, gate = [], asyncio.Event()

        async def send(batch, dropped):
            await gate.wait()
            sent.append(([e["payload"]["i"] for e in batch], dropped))

        box = events.Outbox("hub", send, max_queued=5)
        box.start()
        box.put(_ev(0))
        await asyncio.sleep(0.02)            # the first batch is in flight; the hub doesn't answer
        for i in range(1, 9):                # meanwhile more come than the queue keeps
            box.put(_ev(i))
        assert [ev["payload"]["i"] for _, ev in box.queue] == [4, 5, 6, 7, 8]
        assert box.dropped == 3              # 1-3; 0 left the queue too, but it is in flight
        gate.set()
        await until(lambda: not box.queue)
        assert sent == [([0], 0), ([4, 5, 6, 7, 8], 3)] and box.dropped == 0
        assert all("_size" not in e for e in box.queue)
        await box.stop()
    go(body)


def test_outbox_sends_one_batch_at_a_time_and_retries_until_the_hub_answers():
    async def body():
        tries, inflight, most = [], 0, 0

        async def send(batch, dropped):
            nonlocal inflight, most
            inflight += 1
            most = max(most, inflight)
            try:
                tries.append([e["id"] for e in batch])
                await asyncio.sleep(0.01)
                if len(tries) < 3:
                    raise ConnectionError("hub away")
            finally:
                inflight -= 1

        box = events.Outbox("hub", send, retry_cap=60.0)
        box._backoff.base = 0.01
        box.start()
        for i in range(events.BATCH + 7):
            box.put(_ev(i))
        await until(lambda: not box.queue)
        assert most == 1                                   # backpressure: never two batches in flight
        assert tries[0] == tries[1] == tries[2]            # the same events (ids) until answered
        assert len(tries[0]) == events.BATCH and len(tries[3]) == 7
        await box.stop()
    go(body)


def test_outbox_kick_ends_the_backoff_and_new_events_do_not():
    async def body():
        calls = []

        async def send(batch, dropped):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise ConnectionError("hub away")

        box = events.Outbox("hub", send)
        box._backoff.base = box._backoff.cap = 30.0       # a long wait
        box.start()
        box.put(_ev(1))
        await until(lambda: calls)
        box.put(_ev(2))                                    # more events don't hurry a hub that is away
        await asyncio.sleep(0.1)
        assert len(calls) == 1
        box.kick()                                         # hub contact
        await until(lambda: not box.queue, 2)
        assert len(calls) == 2
        await box.stop()
    go(body)


def test_outbox_batches_stay_under_the_byte_limit():
    async def body():
        sizes = []

        async def send(batch, dropped):
            sizes.append(len(json.dumps(batch)))

        box = events.Outbox("hub", send)
        box.start()
        for i in range(10):
            body_ = "x" * 6000
            box.put(events.make("x.big", "x", {"body": body_}, len(json.dumps({"body": body_}))))
        await until(lambda: not box.queue)
        assert len(sizes) > 1 and max(sizes) < 32 * 1024
        await box.stop()
    go(body)


# -- a real hub and worker ----------------------------------------------------------

@pytest.fixture
def band(tmp_path):
    phone = tmp_path / "local" / "phone"
    (phone / "scripts").mkdir(parents=True)
    (phone / "plugin.yaml").write_text(PHONE_YAML)
    (phone / "scripts" / "phone.py").write_text(PHONE_PY)
    listen = tmp_path / "hub" / "library" / "listen"
    (listen / "scripts").mkdir(parents=True)
    (listen / "plugin.yaml").write_text(LISTEN_YAML)
    (listen / "scripts" / "listen.py").write_text(LISTEN_PY)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(ROOT), "band": BAND}))
    keys = tmp_path / "hub" / "keys"
    keys.mkdir(parents=True)
    (keys / "root.key").write_text(bytes(ROOT).hex())
    (keys / "op.key").write_text(bytes(OP).hex())
    node = secrets.token_hex(16)
    (tmp_path / "hub" / "hub.json").write_text(json.dumps({"node": node}))
    relay = LoopbackRelay()
    cfg = Config(data=tmp_path / "data", anchor=anchor, name="pixel", worker_id=WID,
                 bands=[{"psk": PSK}], plugins=[tmp_path / "local"])
    hub_cfg = HubConfig(data=tmp_path / "hub", band=BAND, library=tmp_path / "hub" / "library",
                        users={"bake": {"preset": "owner"}}, doors={})

    def worker(**opts):
        return Worker(cfg, make_link=lambda spec: relay.link(spec["psk"]),
                      transport_opts={"every": 0.05, "lost_after": 0.5, **opts})

    def hub():
        return Hub(hub_cfg, transport=Transport(node, [relay.link(PSK)], every=0.05))
    return worker, hub


async def _call(hub, cap, args=None, worker="pixel"):
    return await hub.call(HubCaller(person="person:bake"),
                          {"worker": worker, "cap": cap, "args": args, "why": WHY}, "test")


async def _heard(hub) -> list:
    out = await _call(hub, "listen.heard", worker="rook")
    assert out["ok"], out
    return out["result"]


def test_a_worker_event_reaches_hub_hooks_listeners_and_taints(band):
    make_worker, make_hub = band

    async def body():
        w, hub = make_worker(), make_hub()
        await w.start()
        await hub.start()
        await until(lambda: "phone.receive" in getattr(hub.dispatcher.workers.get(WID), "caps", {}))
        hub.enroll.enrolled[WID] = {"name": "pixel", "owner": "person:bake"}
        seen = []
        hub.listeners.append(lambda e, p, src: seen.append((e, p, src)))

        out = await _call(hub, "phone.receive", {"from": "+15550100", "body": "hi"})
        assert out["ok"], out
        await until(lambda: seen)
        payload = {"from": "+15550100", "body": "hi", "person": "person:bake", "worker": "pixel"}
        assert seen == [("phone.sms.received", payload, "phone")]
        heard = []
        for _ in range(100):
            heard = await _heard(hub)
            if heard:
                break
            await asyncio.sleep(0.02)
        assert heard == [{"event": "phone.sms.received", "payload": payload, "tainted": True}]
        assert hub.taints("phone.sms.received") and not hub.taints("phone.moved")

        # a hook acting on it calls in a tainted session of its own; a clean caller stays clean
        await hub.call_as(KitCaller(plugin="listen", tainted=True), "phone.receive",
                          {"from": "+1", "body": "x"}, uses=["phone.receive"])
        await hub.call_as(KitCaller(plugin="listen"), "phone.receive", {"from": "+1", "body": "y"},
                          uses=["phone.receive"])
        dirty = hub.dispatcher.sessions[hub._component_sessions[("plugin.listen", True)]]
        clean = hub.dispatcher.sessions[hub._component_sessions[("plugin.listen", False)]]
        assert dirty.tainted and not clean.tainted
        await hub.call_as(KitCaller(person="person:bake", tainted=True), "phone.receive",
                          {"from": "+1", "body": "z"})
        assert hub.dispatcher.sessions[hub._acting_sessions[("person:bake", None, True)]].tainted

        await hub.stop()
        await w.stop()
    go(body)


def test_events_wait_for_a_hub_that_is_away(band):
    make_worker, make_hub = band

    async def body():
        w, hub = make_worker(lost_after=2.0), make_hub()
        await w.start()
        await hub.start()
        await until(lambda: "phone.receive" in getattr(hub.dispatcher.workers.get(WID), "caps", {}))
        seen = []
        await hub.stop()
        await until(lambda: w.transport.lost(BAND), 10)
        for i in range(5):                       # emitted while the hub is gone
            w._emit("phone.moved", {"at": float(i)}, "phone")
        await asyncio.sleep(0.2)
        box = w.outboxes[(HOME, BAND)]
        assert len(box.queue) == 5
        hub = make_hub()
        hub.listeners.append(lambda e, p, src: seen.append(p["at"]) if e == "phone.moved" else None)
        await hub.start()
        await until(lambda: len(seen) == 5, 15)
        assert seen == [0.0, 1.0, 2.0, 3.0, 4.0] and not box.queue
        await hub.stop()
        await w.stop()
    go(body, 45)


def test_the_hub_refuses_events_it_cannot_vouch_for(band):
    make_worker, make_hub = band

    async def body():
        w, hub = make_worker(), make_hub()
        await w.start()
        await hub.start()
        await until(lambda: "phone.receive" in getattr(hub.dispatcher.workers.get(WID), "caps", {}))
        seen = []
        hub.listeners.append(lambda e, p, src: seen.append((e, p)))

        def ev(name, payload, plugin="phone", eid=None):
            return {"id": eid or secrets.token_hex(8), "event": name, "plugin": plugin, "ts": 1.0,
                    "payload": payload}

        def take(*evs, worker=WID, band_=BAND):
            return hub._worker_events(band_, {"worker_id": worker, "events": list(evs)})

        assert take(ev("phone.moved", {}), worker="ab" * 16)["retry"] is True     # never announced
        assert take(ev("phone.moved", {}), band_="cd" * 16)["retry"] is True      # not on this band
        assert take()["ok"] and take()["result"] == {"taken": 0}
        assert not hub._worker_events(BAND, {"worker_id": WID, "events": "nope"})["ok"]
        bad = [ev("phone.unheard", {}),                          # not declared
               ev("shell.done", {}),                             # declared by nobody it announced
               ev("phone.moved", {}, plugin="other"),            # declared by another plugin
               ev("phone.sms.received", {"body": "no sender"}),  # payload misses a required field
               ev("phone.moved", {"at": "noon"}),                # wrong type
               ev("phone.moved", {"at": 1.0, "extra": True}),    # undeclared field
               ev("phone.moved", "not a map"),
               ev("phone.moved", {"at": 1.0, "pad": "x" * 20000})]
        assert take(*bad)["result"] == {"taken": 0} and seen == []
        # the worker can't name whose data it is: the hub says (its owner; none known here)
        same = ev("phone.moved", {"at": 2.0})
        assert take(same, same)["result"] == {"taken": 1}
        assert take(same)["result"] == {"taken": 0}                                # a batch sent again
        assert seen == [("phone.moved", {"at": 2.0, "person": None, "worker": "pixel"})]
        # outside the ceiling the hub holds for the worker
        hub.dispatcher.workers[WID].ceiling = {"grants": ["phone.receive", "phone.sms.*"]}
        assert take(ev("phone.moved", {"at": 3.0}))["result"] == {"taken": 0}
        assert take(ev("phone.sms.received", {"from": "+1"}))["result"] == {"taken": 1}
        await hub.stop()
        await w.stop()
    go(body)


def test_a_ceiling_keeps_events_from_a_guest_hub(band, tmp_path):
    make_worker, _ = band

    async def body():
        w = make_worker()
        await w.start()
        assert w.host.sends("phone", "phone.moved", {"grants": ["*"]})
        assert w.host.sends("phone", "phone.moved", {"grants": ["phone.moved@pixel"]}, "pixel")
        assert not w.host.sends("phone", "phone.moved", {"grants": ["phone.moved@other"]}, "pixel")
        assert not w.host.sends("phone", "phone.moved", {"grants": ["phone.receive"]})
        assert not w.host.sends("phone", "phone.nope", {"grants": ["*"]})        # not declared
        assert not w.host.sends("ghost", "phone.moved", {"grants": ["*"]})       # not loaded
        # a band that only hears the plugin's events still sees its manifest (9.4)
        view = await w.host.announce({"grants": ["phone.sms.*"]})
        assert [m["plugin"] for m in view["manifests"]] == ["core", "phone"]   # core: always
        await w.stop()
    go(body)
