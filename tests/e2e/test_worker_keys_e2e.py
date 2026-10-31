"""Worker keys on real processes: the worker signs its requests to the hub
with its own key (castling/review.md H2, docs/spec/core-v2.md 5.8 and 13.1).

Another node on the band (any PSK holder) reads the ticket of a call in
flight and asks the hub for a sub-call with it. Before worker keys the hub ran
that as the call's person; now it refuses, and the worker's own signed
sub-call still works."""
from __future__ import annotations

import asyncio
import json
import secrets
import time

from nacl.signing import SigningKey

from rook.core2 import wire
from rook.core2.transport import TelestheteLink

from harness import until
from test_castling_e2e import on_band


def test_enrollment_pins_the_workers_key(band):
    pinned = band.owner.get("/journal?kind=worker_key")["entries"]
    key = SigningKey(bytes.fromhex((band.worker_dir / "data" / "worker.key").read_text().strip()))
    assert [(e["worker"], e["via"], e["key"]) for e in pinned] == \
        [(band.worker_id, "enrollment", wire.key_id(wire.pub_text(key)))]
    enrolled = band.owner.get("/journal?kind=enroll")["entries"]
    assert enrolled[0]["key"] == wire.key_id(wire.pub_text(key))


def test_an_in_flight_ticket_cant_be_borrowed_by_another_band_member(band):
    hub_node = json.loads((band.hub_dir / "data" / "hub.json").read_text())["node"]
    seen: list = []

    async def borrow(t):
        spy = TelestheteLink(band.psk, relay=band.relay_addr)   # sees every frame the relay forwards

        async def got(payload):
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("cap") == "e2e.relay":
                seen.append(msg)

        async def connected():
            await spy.send(json.dumps({"kind": "e2e-hello"}).encode())
        await spy.start(got, connected)
        try:
            call = asyncio.ensure_future(asyncio.to_thread(band.owner.call, "e2e.relay", {"wait": 4}))
            await asyncio.to_thread(until, lambda: seen, 10, "the hub's request on the band")
            ticket = seen[0]["ticket"]

            def borrowed():
                return {"id": secrets.token_hex(16), "cap": "core.call", "target": hub_node,
                        "args": {"ticket": ticket, "cap": "e2e.ping", "target": "e2e-worker",
                                 "args": {"text": "borrowed"}, "why": "Not the worker."}}
            bare = await t.request(band.band, borrowed(), 10)
            req = borrowed()
            req["proof"] = wire.request_proof(SigningKey.generate(), req, worker=band.worker_id, hub=band.hub_id,
                                              band=band.band, nonce=secrets.token_hex(16), ts=int(time.time()))
            forged = await t.request(band.band, req, 10)
            return bare, forged, await call
        finally:
            await spy.stop()

    bare, forged, out = on_band(band, borrow, 40)
    assert (bare["ok"], bare["error"]) == (False, "denied: worker request: no_proof"), bare
    assert (forged["ok"], forged["error"]) == (False, "denied: worker request: unknown_key"), forged
    # the worker's own sub-call, signed with its key, ran inside the call
    assert out["ok"] and out["result"]["text"] == "inner" and out["result"]["person"] == "person:bake", out
    pings = band.owner.get("/journal?cap=e2e.ping")["entries"]
    assert [(e["args"]["text"], e.get("parent")) for e in pings] == [("inner", out["id"])]
