"""A worker written only from docs/spec/worker-profile.md, against a real hub
over a real (UDP) relay. It is also the example implementation the profile
points to: no tickets, keys, signatures, anchor or replay store, and nothing
of rook.core2.worker. The carrier (Telesthete frames, spec 2.1) comes from
the transport library's band link; everything above it is here."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import urllib.request

import pytest

pytest.importorskip("telesthete")
from rook.core2.hub.runtime import Config, Hub  # noqa: E402  (the hub under test)
from rook.core2.transport import TelestheteLink  # noqa: E402  (the carrier only)

# -- the worker -----------------------------------------------------------------

MANIFEST = {"manifest": 1, "plugin": "tiny", "version": "1.0.0", "title": "Tiny",
            "doc": "A worker written from the minimal profile.", "builtin": True,
            "caps": {"echo": {"doc": "Say the text back.", "risk": "read", "scope": "self",
                              "args": {"text": {"type": "str", "required": True}}}}}


def canonical(value) -> bytes:
    """Canonical JSON (RFC 8785) for values made of objects, lists, strings,
    integers and booleans: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class MinimalWorker:
    def __init__(self, psk: str, relay: str, name: str, every: float = 30.0, sends_manifests: bool = True):
        self.id, self.name, self.every = secrets.token_hex(16), name, every
        self.sends_manifests = sends_manifests      # False: a fixed device the hub's library carries
        self.link = TelestheteLink(psk, relay=relay)
        self.entries = [{"plugin": "tiny", "version": "1.0.0", "hash": sha256(MANIFEST)}]
        self.asked: list[str] = []

    async def send(self, msg: dict) -> None:
        await self.link.send(json.dumps(msg).encode())

    async def start(self) -> None:
        await self.link.start(self.received, self.announce)     # announces on every (re)connect
        self.beating = asyncio.ensure_future(self.heartbeat())

    async def stop(self) -> None:
        self.beating.cancel()
        await self.link.stop()

    async def announce(self) -> None:
        await self.send({"kind": "announce", "worker_id": self.id, "name": self.name, "core": "2.0",
                         "manifests": self.entries, "manifests_hash": sha256(self.entries)})

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            await self.send({"kind": "heartbeat", "worker_id": self.id, "core": "2.0",
                             "manifests_hash": sha256(self.entries)})

    async def received(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return                                  # not JSON (Drop packets): not ours
        if not (isinstance(msg, dict) and isinstance(msg.get("cap"), str) and msg.get("target") == self.id):
            return                                  # announces, acks, replies, calls to others
        cap, args = msg["cap"], msg.get("args") or {}
        self.asked.append(cap)
        reply = {"id": msg["id"], "from": self.id, "ok": True}
        if cap == "core.manifests" and self.sends_manifests:
            wanted = args.get("plugins")
            reply["result"] = {"manifests": [MANIFEST] if not wanted or "tiny" in wanted else []}
        elif cap == "tiny.echo":
            reply["result"] = {"text": args.get("text")}
        else:
            reply.update(ok=False, error=f"unknown capability: {cap}")
        await self.send(reply)


# -- the test -------------------------------------------------------------------

class Relay(asyncio.DatagramProtocol):
    """Forwards each datagram to the other peers of its band (the first 16 bytes)."""

    def __init__(self):
        self.peers: dict[bytes, set] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        peers = self.peers.setdefault(data[:16], set())
        peers.add(addr)
        for peer in peers - {addr}:
            self.transport.sendto(data, peer)


async def door(hub: Hub, token: str, path: str, body: dict | None = None) -> dict:
    def go():
        req = urllib.request.Request(f"http://127.0.0.1:{hub.doors[0].port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    return await asyncio.to_thread(go)


async def until(cond, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while not (got := await cond()):
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)
    return got


@pytest.mark.parametrize("in_library", [False, True], ids=["sends-manifests", "fixed-device"])
def test_a_worker_written_from_the_profile_alone(tmp_path, in_library):
    now = [time.time()]             # the hub's clock: moved on to pass heartbeats and liveness

    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        if in_library:
            (tmp_path / "library" / "tiny").mkdir(parents=True)
            (tmp_path / "library" / "tiny" / "plugin.yaml").write_text(json.dumps(MANIFEST))
        hub = Hub(Config(data=tmp_path / "hub", library=tmp_path / "library" if in_library else None,
                         users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}), clock=lambda: now[0])
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        w = MinimalWorker(psk, addr, "tiny-box", every=0.3, sends_manifests=not in_library)

        async def listed():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "tiny-box"), None)

        async def beat_heard():
            return (await listed())["last_seen"] == now[0]
        try:
            await w.start()
            me = await until(listed)
            assert (me["id"], me["online"], me["plugins"], me["problem"]) == (w.id, True, ["tiny"], None)

            out = await door(hub, token, "/call", {"worker": "tiny-box", "cap": "tiny.echo",
                                                   "args": {"text": "hello"}, "why": "Test the minimal worker."})
            assert out["ok"] and out["result"] == {"text": "hello"}, out
            assert w.asked == (["tiny.echo"] if in_library else ["core.manifests", "tiny.echo"])

            for _ in range(3):      # three minutes of hub time: its heartbeats keep it online
                now[0] += 60
                await until(beat_heard)
                hub._liveness()
                assert (await listed())["online"] is True

            await w.stop()
            await asyncio.sleep(0.5)
            now[0] += 91
            hub._liveness()
            assert (await listed())["online"] is False
        finally:
            if getattr(w, "beating", None) and not w.beating.done():
                await w.stop()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 60))
