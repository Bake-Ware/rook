"""Core v2 transport (rook/core2/transport): every message kind round-trips
hub <-> worker over the in-memory relay, plus multi-band, hub loss,
reconnect, large replies and bundles over Drop, and one real Telesthete
round trip over a UDP relay on 127.0.0.1. Every test has a hard timeout."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import CORE_VERSION, wire  # noqa: E402
from rook.core2.transport import (FEATURES, LoopbackRelay, TelestheteLink, Transport,  # noqa: E402
                                  TransportError, band_of, from_config)
from rook.core2.transport import conn as conn_mod  # noqa: E402

PSK, PSK2 = "test-band-psk-one", "test-band-psk-two"
BAND, BAND2 = band_of(PSK), band_of(PSK2)
HUB, HUB2, WORKER, OTHER = "a1" * 16, "a2" * 16, "b0" * 16, "c0" * 16
OP = SigningKey(bytes(range(32)))
NOW = 1793300000


def arun(coro, seconds=10):
    """Run a coroutine with a hard timeout so no test can hang CI."""
    return asyncio.run(asyncio.wait_for(coro, seconds))


async def until(cond, seconds=5.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def worker_announce(worker_id=WORKER, core=CORE_VERSION):
    return {"kind": "announce", "worker_id": worker_id, "name": "kaiju", "core": core,
            "manifests": [], "manifests_hash": hashlib.sha256(b"[]").hexdigest(),
            "features": FEATURES}


def heartbeat(worker_id=WORKER):
    return {"kind": "heartbeat", "worker_id": worker_id, "core": CORE_VERSION, "ts": NOW,
            "manifests_hash": hashlib.sha256(b"[]").hexdigest()}


def hub_announce(band=BAND, seq=1, node=HUB):
    return wire.sign(OP, {"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": node,
                          "name": "rook", "hub": "ab" * 8, "band": band, "core": CORE_VERSION,
                          "ts": NOW, "seq": seq, "features": FEATURES})


def ticket(call_id, cap, args, worker=WORKER, band=BAND):
    return wire.sign(OP, {"typ": "rook-ticket", "v": 2, "hub": "ab" * 8, "band": band,
                          "call": call_id, "nonce": os.urandom(16).hex(), "worker": worker,
                          "cap": cap, "args_hash": wire.args_hash(args), "agent": "agent:test",
                          "session": "s_1", "why": "Transport test.", "iat": NOW, "exp": NOW + 60})


def request(cap, args, target=WORKER, band=BAND):
    cid = os.urandom(16).hex()
    return {"id": cid, "cap": cap, "target": target, "args": args,
            "ticket": ticket(cid, cap, args, worker=target, band=band)}


class Node:
    """A transport plus the messages it received."""

    def __init__(self, node_id, links, **kw):
        self.t = Transport(node_id, links, **kw)
        self.got: list[tuple[str, dict]] = []
        self.serve = None       # async (band, msg) -> reply dict | None

    async def start(self):
        await self.t.start(self.on_message)

    async def on_message(self, band, msg):
        self.got.append((band, msg))
        if self.serve is not None and "cap" in msg:
            out = await self.serve(band, msg)
            if out is not None:
                await self.t.send(band, out)

    def kinds(self, kind, band=None):
        return [m for b, m in self.got if m.get("kind") == kind and band in (None, b)]

    def reply(self, rid):
        return next((m for _, m in self.got if m.get("id") == rid and "ok" in m and "cap" not in m), None)


async def pair(relay=None, **worker_kw):
    relay = relay or LoopbackRelay()
    seq = iter(range(1, 10**6))
    hub = Node(HUB, [relay.link(PSK)], hello=lambda b: hub_announce(b, next(seq)),
               beat=lambda b: hub_announce(b, next(seq)), every=0.05)
    worker = Node(WORKER, [relay.link(PSK)], hello=lambda b: worker_announce(), **worker_kw)
    await hub.start()
    await worker.start()
    await until(lambda: WORKER in hub.t.peers(BAND) and HUB in worker.t.peers(BAND))
    return relay, hub, worker


def ok(msg, result):
    return {"id": msg["id"], "from": WORKER, "ok": True, "result": result}


# -- every message kind -----------------------------------------------------------

def test_every_message_kind_round_trips_on_loopback():
    async def main():
        relay, hub, worker = await pair(beat=lambda b: heartbeat(), every=0.05)

        # announces both ways (5.3, 5.4); the signed hub announce still verifies
        got = hub.kinds("announce")[0]
        assert got == worker_announce()
        assert worker.t.peers(BAND)[HUB].name == "rook"
        assert wire.verify_sig(wire.pub_text(OP), worker.kinds("announce")[0])
        # heartbeats (5.5)
        await until(lambda: len(hub.kinds("heartbeat")) >= 2)
        assert hub.kinds("heartbeat")[0] == heartbeat()

        async def serve(band, msg):
            if msg["cap"] == "shell.exec":
                return ok(msg, {"code": 0, "stdout": "up 3 days"})
            if msg["cap"] == "core.lease.drop":
                return ok(msg, {"dropped": 1})
            if msg["cap"] == "core.trust.set":
                return ok(msg, {"ok": True})
            return {"id": msg["id"], "from": WORKER, "ok": False, "error": f"unknown capability: {msg['cap']}"}
        worker.serve = serve

        # a call with its ticket, and its result (6.1, 6.2, 6.4)
        call = request("shell.exec", {"argv": ["uptime"]})
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]))
        assert hub.reply(call["id"])["result"] == {"code": 0, "stdout": "up 3 days"}
        seen = next(m for _, m in worker.got if m.get("id") == call["id"])
        assert seen["ticket"] == call["ticket"] and wire.verify_sig(wire.pub_text(OP), seen["ticket"])
        assert seen["args"] == call["args"] and "drop" in seen["accept"]
        assert wire.args_hash(seen["args"]) == call["ticket"]["args_hash"]

        # revoke (core.lease.drop) and push (core.trust.set) are calls like any other
        drop = request("core.lease.drop", {"session": "s_1"})
        doc = wire.sign(OP, {"typ": "rook-membership", "v": 2, "serial": "cd" * 16, "worker": WORKER,
                             "hub": "ab" * 8, "root": wire.pub_text(OP), "band": BAND2, "role": "guest",
                             "ceiling": {"grants": ["light.*"]}, "by": "person:bake", "iat": NOW})
        push = request("core.trust.set", {"membership": doc, "connect": {"relay": "r:7474", "psk": "x"}})
        bad = request("nope.cap", {})
        for m in (drop, push, bad):
            await hub.t.send(BAND, m)
        await until(lambda: all(hub.reply(m["id"]) for m in (drop, push, bad)))
        assert hub.reply(drop["id"])["result"] == {"dropped": 1}
        assert hub.reply(push["id"])["ok"] is True
        assert hub.reply(bad["id"]) == {"id": bad["id"], "from": WORKER, "ok": False,
                                        "error": "unknown capability: nope.cap"}
        pushed = next(m for _, m in worker.got if m.get("id") == push["id"])
        assert wire.verify_sig(wire.pub_text(OP), pushed["args"]["membership"])

        # elevation: the worker asks the hub inside the call, the hub answers with a lease (7.2, 13)
        lease = wire.sign(OP, {"typ": "rook-lease", "v": 2, "id": "ee" * 16, "hub": "ab" * 8, "band": BAND,
                               "worker": WORKER, "agent": "agent:test", "session": "s_1",
                               "iat": NOW, "exp": NOW + 900})

        async def hub_serve(band, msg):
            if msg["cap"] == "core.elevate":
                return {"id": msg["id"], "from": HUB, "ok": True, "result": {"lease": lease}}
            if msg["cap"] == "core.call":
                return {"id": msg["id"], "from": HUB, "ok": True, "result": {"sub": msg["args"]["cap"]}}
        hub.serve = hub_serve
        elevate = {"id": os.urandom(16).hex(), "cap": "core.elevate", "target": HUB,
                   "args": {"ticket": call["ticket"], "reason": "upfront"}}
        answer = await worker.t.request(BAND, elevate, timeout=5)
        assert answer["result"]["lease"] == lease and wire.verify_sig(wire.pub_text(OP), lease)
        assert not worker.reply(elevate["id"]), "a request() reply skips on_message"
        sub = await worker.t.request(BAND, {"id": os.urandom(16).hex(), "cap": "core.call", "target": HUB,
                                            "args": {"ticket": call["ticket"], "cap": "x.y", "target": OTHER,
                                                     "args": {}, "why": "sub-call"}}, timeout=5)
        assert sub["result"] == {"sub": "x.y"}
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_requests_are_addressed_and_replies_go_to_rr():
    async def main():
        relay, hub, worker = await pair()
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, None))
        call = request("shell.exec", {})
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]))
        sent = [(json.loads(p), r) for _, p, r in relay.sent if p[:1] == b"{"]
        req = next((m, r) for m, r in sent if m.get("cap") == "shell.exec")
        rep = next((m, r) for m, r in sent if m.get("id") == call["id"] and "ok" in m)
        hub_route = wire_route(HUB)
        assert req[1] == wire_route(WORKER) and req[0]["rr"] == hub_route
        assert rep[1] == hub_route
        # announces and heartbeats are broadcast
        assert all(r == 0 for m, r in sent if m.get("kind") == "announce")
        await hub.t.close()
        await worker.t.close()
    arun(main())


def wire_route(name):
    from rook.worker.wire.codec import route_id_for
    return route_id_for(name)


def test_requests_for_another_node_and_foreign_chatter_stay_silent():
    async def main():
        relay = LoopbackRelay()
        hub = Node(HUB, [relay.link(PSK)])
        worker = Node(WORKER, [relay.link(PSK)])
        other = Node(OTHER, [relay.link(PSK)])
        for n in (hub, worker, other):
            await n.start()
        await until(lambda: all(n.t.connected(BAND) for n in (hub, worker, other)))
        await hub.t.send(BAND, request("shell.exec", {}, target=OTHER))
        await hub.t.send(BAND, {"hello": "world"})               # no kind, no cap: foreign
        await hub.t.send(BAND, {"kind": "stream.post", "x": 1})  # a newer kind: passed on
        await until(lambda: len(other.got) == 2)
        await asyncio.sleep(0.05)
        assert [m.get("kind") for _, m in worker.got] == ["stream.post"]
        assert other.got[0][1]["target"] == OTHER
        for n in (hub, worker, other):
            await n.t.close()
    arun(main())


# -- versions --------------------------------------------------------------------

def test_requests_to_an_incompatible_peer_are_refused():
    async def main():
        relay = LoopbackRelay()
        hub = Node(HUB, [relay.link(PSK)])
        old = Node(OTHER, [relay.link(PSK)], hello=lambda b: worker_announce(OTHER, core="3.0"))
        newer = Node(WORKER, [relay.link(PSK)], hello=lambda b: worker_announce(WORKER, core="2.7"))
        for n in (hub, old, newer):
            await n.start()
        await until(lambda: OTHER in hub.t.peers(BAND) and WORKER in hub.t.peers(BAND))
        peers = hub.t.peers(BAND)
        assert peers[OTHER].core is None and not peers[OTHER].compatible
        assert peers[WORKER].core == "2.0" and peers[WORKER].compatible
        with pytest.raises(TransportError, match="incompatible"):
            await hub.t.send(BAND, request("shell.exec", {}, target=OTHER))
        await hub.t.send(BAND, request("shell.exec", {}, target=WORKER))
        # an unknown node is not refused (5.6: stored status is only a hint)
        await hub.t.send(BAND, request("shell.exec", {}, target="dd" * 16))
        for n in (hub, old, newer):
            await n.t.close()
    arun(main())


# -- several bands -----------------------------------------------------------------

def test_a_worker_on_two_bands_keeps_each_separate():
    async def main():
        relay = LoopbackRelay()
        home = Node(HUB, [relay.link(PSK)], hello=lambda b: hub_announce(b, node=HUB))
        guest = Node(HUB2, [relay.link(PSK2)], hello=lambda b: hub_announce(b, node=HUB2))
        lost = []
        worker = Node(WORKER, [relay.link(PSK), relay.link(PSK2)],
                      hello=lambda b: {**worker_announce(), "ceiling": {"band": b}},
                      beat=lambda b: {**heartbeat(), "status": {"band": b}}, every=0.05,
                      on_lost=lost.append, lost_after=0.3)
        for n in (home, guest, worker):
            await n.start()
        assert sorted(worker.t.bands) == sorted([BAND, BAND2])
        await until(lambda: home.kinds("heartbeat") and guest.kinds("heartbeat"))
        # each band hears only its own announce and heartbeats
        assert {m["ceiling"]["band"] for m in home.kinds("announce")} == {BAND}
        assert {m["ceiling"]["band"] for m in guest.kinds("announce")} == {BAND2}
        assert {m["status"]["band"] for m in guest.kinds("heartbeat")} == {BAND2}
        assert HUB2 not in worker.t.peers(BAND) and HUB not in worker.t.peers(BAND2)
        # each band answers on its own band
        async def serve(band, msg):
            return {"id": msg["id"], "from": WORKER, "ok": True, "result": {"band": band}}
        worker.serve = serve
        for hub, band in ((home, BAND), (guest, BAND2)):
            call = request("info.ping", {}, band=band)
            await hub.t.send(band, call)
            await until(lambda: hub.reply(call["id"]))
            assert hub.reply(call["id"])["result"] == {"band": band}
        # hub loss is per band: only the guest band stops making contact
        worker.t.contact(BAND)
        worker.t.contact(BAND2)
        for _ in range(12):
            worker.t.contact(BAND)
            await asyncio.sleep(0.05)
        assert lost == [BAND2] and worker.t.lost(BAND2) and not worker.t.lost(BAND)
        # leaving a band stops its heartbeats; the other keeps going
        await worker.t.remove(BAND2)
        n = len(guest.kinds("heartbeat"))
        before = len(home.kinds("heartbeat"))
        await asyncio.sleep(0.2)
        assert len(guest.kinds("heartbeat")) == n and len(home.kinds("heartbeat")) > before
        with pytest.raises(TransportError, match="not on band"):
            await worker.t.send(BAND2, heartbeat())
        for n in (home, guest, worker):
            await n.t.close()
    arun(main())


def test_hub_loss_and_contact_are_surfaced():
    async def main():
        events = []
        relay = LoopbackRelay()
        worker = Node(WORKER, [relay.link(PSK)], lost_after=0.15,
                      on_lost=lambda b: events.append(("lost", b)),
                      on_contact=lambda b: asyncio.sleep(0, events.append(("contact", b))))
        await worker.start()
        assert worker.t.lost(BAND), "no hub contact yet"
        worker.t.contact(BAND)
        await until(lambda: events == [("contact", BAND)])
        assert not worker.t.lost(BAND)
        await until(lambda: events[-1] == ("lost", BAND))
        worker.t.contact(BAND)
        await until(lambda: events[-1] == ("contact", BAND))
        assert events == [("contact", BAND), ("lost", BAND), ("contact", BAND)]
        await worker.t.close()
    arun(main())


# -- links coming and going --------------------------------------------------------

def test_link_start_retries_with_backoff_and_reconnect_re_announces():
    async def main():
        relay = LoopbackRelay()
        hub = Node(HUB, [relay.link(PSK)])
        link = relay.link(PSK)
        link.fail_starts = 3
        worker = Node(WORKER, [link], hello=lambda b: worker_announce(), backoff_base=0.01)
        await hub.start()
        await worker.start()
        await until(lambda: len(hub.kinds("announce")) == 1)
        assert link.fail_starts == 0
        link.cut()
        with pytest.raises(TransportError, match="link down"):
            await worker.t.send(BAND, heartbeat())
        await link.heal()
        await until(lambda: len(hub.kinds("announce")) == 2)
        await hub.t.close()
        await worker.t.close()
    arun(main())


# -- large payloads over Drop --------------------------------------------------------

def test_large_reply_travels_over_drop_and_arrives_whole():
    async def main():
        relay, hub, worker = await pair()
        blob = os.urandom(150 * 1024).hex()            # 300 KB of JSON
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, {"data": blob}))
        call = request("file.read", {"path": "/big"})
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]))
        assert hub.reply(call["id"]) == ok(call, {"data": blob})
        sent = [json.loads(p) for _, p, _ in relay.sent if p[:1] == b"{"]
        stub = [m for m in sent if m.get("id") == call["id"] and "ok" in m]
        assert len(stub) == 1 and stub[0]["drop"]["size"] > 300_000 and "result" not in stub[0]
        # through request() too
        answer = await hub.t.request(BAND, request("file.read", {"path": "/big"}), timeout=5)
        assert answer["result"]["data"] == blob
        await hub.t.close()
        await worker.t.close()
    arun(main(), 20)


def test_large_reply_without_drop_is_sent_inline_and_oversize_is_an_error(monkeypatch):
    monkeypatch.setattr(conn_mod, "MAX_MESSAGE", 200_000)

    async def main():
        relay, hub, worker = await pair()
        sizes = {"small": 100_000, "huge": 300_000}
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, "x" * sizes[msg["args"]["n"]]))
        for n in sizes:
            msg = request("x.y", {"n": n})
            await worker.t._band(BAND)._payload(json.dumps({**msg, "accept": []}).encode())
        await until(lambda: len([m for _, m in hub.got if "ok" in m]) == 2)
        replies = [m for _, m in hub.got if "ok" in m]
        assert any(m.get("result") == "x" * 100_000 for m in replies)
        assert any(m.get("error", "").startswith("reply too large: ") for m in replies)
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_plugin_bundle_is_offered_and_pulled_with_its_hash_checked():
    async def main():
        relay, hub, worker = await pair()
        bundle = os.urandom(400 * 1024)
        installed = []

        async def serve(band, msg):
            data = await worker.t.pull(band, msg["args"]["bundle"], source=HUB, timeout=5)
            installed.append(data)
            return ok(msg, {"ok": True, "sha256": hashlib.sha256(data).hexdigest()})
        worker.serve = serve
        desc = hub.t.offer(BAND, bundle, to=WORKER)
        assert desc["size"] == len(bundle) and desc["sha256"] == hashlib.sha256(bundle).hexdigest()
        answer = await hub.t.request(BAND, request("core.plugin.install", {"bundle": desc}), timeout=10)
        assert answer["result"]["sha256"] == desc["sha256"] and installed == [bundle]
        # a descriptor nobody serves, or a lying one, fails cleanly
        with pytest.raises(TransportError):
            await worker.t.pull(BAND, {**desc, "id": (desc["id"] + 1) | 0x8000}, source=HUB, timeout=0.3)
        with pytest.raises(TransportError, match="malformed"):
            await worker.t.pull(BAND, {"id": "x"}, source=HUB)
        await hub.t.close()
        await worker.t.close()
    arun(main(), 20)


# -- acks and resends ------------------------------------------------------------------

@pytest.fixture
def quick_acks(monkeypatch):
    monkeypatch.setattr(conn_mod, "ACK_DELAY", 0.02)
    monkeypatch.setattr(conn_mod, "ACK_FIRST", 0.1)
    monkeypatch.setattr(conn_mod, "ACK_EVERY", 0.1)
    monkeypatch.setattr(conn_mod, "TICK", 0.02)


def _msgs(relay, sender=None):
    out = []
    for _, p, _ in relay.sent:
        if p[:1] == b"{":
            m = json.loads(p)
            if sender is None or m.get("from") == sender:
                out.append(m)
    return out


def _lose_first(pred):
    """A relay ``lose`` hook that drops the first payload matching ``pred``."""
    lost = []

    def lose(payload):
        if not lost and payload[:1] == b"{" and pred(json.loads(payload)):
            lost.append(payload)
            return True
        return False
    return lose, lost


def test_a_lost_request_is_resent_and_runs_once(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        runs = []
        worker.serve = lambda band, msg: asyncio.sleep(0, runs.append(msg["id"]) or ok(msg, "up"))
        call = request("shell.exec", {"argv": ["uptime"]})
        relay.lose, lost = _lose_first(lambda m: m.get("id") == call["id"] and "cap" in m)
        t0 = asyncio.get_running_loop().time()
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]))
        assert lost and runs == [call["id"]]
        assert asyncio.get_running_loop().time() - t0 < 1.0      # ACK_FIRST, not a call timeout
        await asyncio.sleep(0.3)                                   # nothing more goes
        assert runs == [call["id"]] and not hub.t._band(BAND).unacked
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_a_lost_reply_is_resent_without_running_the_call_again(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        runs = []
        worker.serve = lambda band, msg: asyncio.sleep(0, runs.append(msg["id"]) or ok(msg, "up"))
        call = request("shell.exec", {"argv": ["uptime"]})
        relay.lose, lost = _lose_first(lambda m: m.get("id") == call["id"] and "ok" in m)
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]))
        assert lost and runs == [call["id"]]
        await asyncio.sleep(0.3)
        # the hub saw the reply once, and both sides stopped resending
        assert len([m for _, m in hub.got if m.get("id") == call["id"]]) == 1
        assert not hub.t._band(BAND).unacked and not worker.t._band(BAND).unacked
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_a_long_call_is_acked_and_its_lost_reply_still_arrives(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        go = asyncio.Event()

        async def serve(band, msg):
            await go.wait()
            return ok(msg, "done")
        worker.serve = serve
        call = request("soak.sleep", {})
        await hub.t.send(BAND, call)
        await until(lambda: any(call["id"] in m.get("ids", ()) for m in _msgs(relay, WORKER)
                                if m.get("kind") == "ack"))
        await asyncio.sleep(0.3)                    # acked: the request is not sent again
        assert sum(1 for m in _msgs(relay) if m.get("id") == call["id"] and "cap" in m) == 1
        relay.lose, lost = _lose_first(lambda m: m.get("id") == call["id"] and "ok" in m)
        go.set()
        await until(lambda: hub.reply(call["id"]))
        assert lost and hub.reply(call["id"])["result"] == "done"
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_quick_replies_need_no_request_ack_and_acks_are_batched(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, "up"))
        calls = [request("shell.exec", {"n": i}) for i in range(30)]
        await asyncio.gather(*(hub.t.send(BAND, c) for c in calls))
        await until(lambda: all(hub.reply(c["id"]) for c in calls))
        await asyncio.sleep(0.2)
        acks_from_worker = [m for m in _msgs(relay, WORKER) if m.get("kind") == "ack"]
        acks_from_hub = [m for m in _msgs(relay, HUB) if m.get("kind") == "ack"]
        assert not acks_from_worker                        # every reply beat its ack
        acked = [i for m in acks_from_hub for i in m["ids"]]
        assert sorted(acked) == sorted(c["id"] for c in calls)
        assert len(acks_from_hub) <= 4                     # batched, ACK_IDS per message
        assert not worker.t._band(BAND).unacked
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_a_reminted_ticket_for_the_same_call_is_a_new_request(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        runs = []
        worker.serve = lambda band, msg: asyncio.sleep(0, runs.append(msg["ticket"]["nonce"]) or ok(msg, "up"))
        call = request("shell.exec", {"argv": ["uptime"]})
        await hub.t.send(BAND, call)
        await until(lambda: hub.reply(call["id"]) and not worker.t._band(BAND).unacked)
        await hub.t.send(BAND, call)                       # the same request again: a resend
        await asyncio.sleep(0.2)
        again = {**call, "ticket": ticket(call["id"], call["cap"], call["args"])}
        await hub.t.send(BAND, again)                      # same id, fresh nonce: runs
        await until(lambda: len(runs) == 2)
        assert runs == [call["ticket"]["nonce"], again["ticket"]["nonce"]]
        await until(lambda: len([m for _, m in hub.got if m.get("id") == call["id"]]) == 2)
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_no_resends_to_a_peer_without_ack_or_after_forget(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        worker.serve = None                                # never replies
        old = request("shell.exec", {})
        hub.t._band(BAND).peers[WORKER].features = ["drop", "route"]
        relay.lose = lambda p: p[:1] == b"{" and json.loads(p).get("kind") == "ack"
        await hub.t.send(BAND, old)
        await asyncio.sleep(0.4)
        assert sum(1 for m in _msgs(relay) if m.get("id") == old["id"]) == 1
        hub.t._band(BAND).peers[WORKER].features = FEATURES
        call = request("shell.exec", {})
        await hub.t.send(BAND, call)
        await until(lambda: sum(1 for m in _msgs(relay) if m.get("id") == call["id"]) >= 3)
        hub.t.forget(BAND, call["id"])
        n = sum(1 for m in _msgs(relay) if m.get("id") == call["id"])
        await asyncio.sleep(0.4)
        assert sum(1 for m in _msgs(relay) if m.get("id") == call["id"]) == n
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_unanswered_resends_refresh_the_link_and_recovery_re_announces(quick_acks):
    async def main():
        relay, hub, worker = await pair()
        link = hub.t._band(BAND).link
        refreshed = []

        async def refresh():
            refreshed.append(1)
        link.refresh = refresh
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, "up"))
        announces = len(worker.kinds("announce"))
        relay.lose = lambda p: True                        # the relay is gone
        call = request("shell.exec", {})
        await hub.t.send(BAND, call)
        await until(lambda: refreshed)
        relay.lose = None                                  # back
        await until(lambda: hub.reply(call["id"]))
        await until(lambda: len(worker.kinds("announce")) > announces)
        assert len(refreshed) == 1
        await hub.t.close()
        await worker.t.close()
    arun(main())


def test_a_large_reply_pulls_through_lost_drop_packets(monkeypatch):
    async def main():
        relay, hub, worker = await pair()
        blob = os.urandom(400 * 1024).hex()
        worker.serve = lambda band, msg: asyncio.sleep(0, ok(msg, {"data": blob}))
        n = iter(range(10**9))
        relay.lose = lambda p: p[:1] != b"{" and next(n) % 17 == 5       # ~6 % of Drop packets
        answer = await hub.t.request(BAND, request("file.read", {"path": "/big"}), timeout=15)
        assert answer["result"]["data"] == blob
        await hub.t.close()
        await worker.t.close()
    arun(main(), 30)


# -- config -------------------------------------------------------------------------

def test_from_config_builds_one_link_per_band(monkeypatch):
    monkeypatch.setenv("ROOK_TEST_PSK", PSK2)
    t = from_config({"node_id": WORKER, "bands": [{"psk": PSK, "relay": "127.0.0.1:7474"},
                                                  {"psk_env": "ROOK_TEST_PSK", "ws_url": "wss://hub.example/band"}]})
    assert t.bands == [BAND, BAND2] and t.node_id == WORKER
    assert TelestheteLink(PSK, relay="[::1]:7475").t.hub == ("::1", 7475)
    assert TelestheteLink(PSK, relay="relay.lan").t.hub == ("relay.lan", 7474)
    assert band_of(PSK) == hashlib.sha256(PSK.encode()).digest()[:16].hex()
    with pytest.raises(ValueError):
        from_config({"node_id": WORKER, "bands": [{"relay": "x:1"}]})
    with pytest.raises(ValueError):
        from_config({"node_id": WORKER, "bands": [{"psk": PSK}]})
    with pytest.raises(ValueError, match="already on band"):
        from_config({"node_id": WORKER, "bands": [{"psk": PSK, "relay": "a:1"}, {"psk": PSK, "relay": "b:1"}]})


# -- a real Telesthete round trip ------------------------------------------------------

class _Relay(asyncio.DatagramProtocol):
    """Minimal telesthitium: forward each datagram to the band's other peers."""

    def __init__(self):
        self.peers: dict[bytes, set] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        band = data[:16]
        self.peers.setdefault(band, set()).add(addr)
        for peer in self.peers[band]:
            if peer != addr:
                self.transport.sendto(data, peer)


def test_telesthete_round_trip_on_localhost():
    async def main():
        loop = asyncio.get_running_loop()
        relay_t, _ = await loop.create_datagram_endpoint(_Relay, local_addr=("127.0.0.1", 0))
        port = relay_t.get_extra_info("socket").getsockname()[1]
        relay = f"127.0.0.1:{port}"
        hub = Node(HUB, [TelestheteLink(PSK, relay=relay)], hello=lambda b: hub_announce(b))
        worker = Node(WORKER, [TelestheteLink(PSK, relay=relay)], hello=lambda b: worker_announce(),
                      beat=lambda b: heartbeat(), every=0.2)
        try:
            await hub.start()
            await until(lambda: hub.t.connected(BAND))
            await worker.start()
            await until(lambda: WORKER in hub.t.peers(BAND) and hub.kinds("heartbeat"))
            # the hub announce went out before the worker joined; the next one is heard
            await hub.t.send(BAND, hub_announce(seq=2))
            await until(lambda: HUB in worker.t.peers(BAND))
            assert wire.verify_sig(wire.pub_text(OP), worker.kinds("announce")[0])

            big = os.urandom(60 * 1024).hex()

            async def serve(band, msg):
                if msg["cap"] == "core.plugin.install":
                    data = await worker.t.pull(band, msg["args"]["bundle"], source=HUB, timeout=10)
                    return ok(msg, {"sha256": hashlib.sha256(data).hexdigest()})
                return ok(msg, {"stdout": big if msg["args"].get("big") else "up"})
            worker.serve = serve
            small = await hub.t.request(BAND, request("shell.exec", {"argv": ["uptime"]}), timeout=5)
            assert small["result"] == {"stdout": "up"}
            large = await hub.t.request(BAND, request("shell.exec", {"big": True}), timeout=10)
            assert large["result"] == {"stdout": big}
            bundle = os.urandom(100 * 1024)
            desc = hub.t.offer(BAND, bundle, to=WORKER)
            done = await hub.t.request(BAND, request("core.plugin.install", {"bundle": desc}), timeout=15)
            assert done["result"]["sha256"] == hashlib.sha256(bundle).hexdigest()
            lease = {"lease": {"id": "ee" * 16}}
            hub.serve = lambda band, msg: asyncio.sleep(0, {"id": msg["id"], "from": HUB, "ok": True,
                                                             "result": lease})
            got = await worker.t.request(BAND, {"id": os.urandom(16).hex(), "cap": "core.elevate",
                                                "target": HUB, "args": {"reason": "upfront"}}, timeout=5)
            assert got["result"] == lease
        finally:
            await hub.t.close()
            await worker.t.close()
            relay_t.close()
    arun(main(), 30)
