"""Core v2 messages over band links (docs/spec/core-v2.md sections 2, 3.1,
5.3-5.5, 6, 9.4): one :class:`Transport` per process, hub or worker, holding
one link per band.

    t = Transport(node_id, [link_a, link_b], hello=announce, beat=heartbeat,
                  on_lost=gate_tick, on_contact=...)
    await t.start(on_message)          # on_message(band, msg)
    await t.send(band, msg)            # requests, replies, announces, anything
    reply = await t.request(band, msg, timeout=10)    # e.g. core.elevate
    await t.close()

What it does for every band, independently (a worker on several bands keeps
one connection, one heartbeat and one hub-loss clock per band, 9.4):

- **Connect** in the background with jittered backoff until the link comes
  up; every (re)connect sends ``hello(band)`` (the full announce, 5.3/5.4).
- **Beat**: ``beat(band)`` every ``every`` seconds +-20 % (the worker's
  heartbeat 5.5, the hub's signed announce 5.4).
- **Receive**: JSON objects go to ``on_message(band, msg)``, except
  requests whose ``target`` is not this node (6.1: stay silent), messages
  that are neither request, reply nor a ``kind`` (3.1: foreign chatter),
  keepalives and Drop packets.
- **Peers** (:meth:`peers`): every node heard from, with its name, stated
  and negotiated ``core`` (2.2), ``features`` and when it was last heard,
  from announces, heartbeats and replies. Requests to a peer whose ``core``
  has nothing in common with ours are refused (2.2), and requests to a peer
  that announced ``route`` go ADDRESSED to it (core v1 14.1).
- **Large replies** (core v1 14.2): a reply over 32 KB to a request that
  accepts ``drop`` is served over Drop and sent as a stub; the caller side
  pulls it and hands ``on_message`` the full reply, as if inline. Requests
  sent through here always accept ``drop``.
- **Bulk** (:meth:`offer`, :meth:`pull`): any blob, plugin bundles above
  all, served over Drop and pulled receiver-driven with a SHA-256 check. A
  plugin push is a ticketed call whose args carry the descriptor, so the
  ticket's ``args_hash`` binds the bundle's hash.
- **Acks and resends** (feature ``ack``): requests sent here accept
  ``ack``. The target of such a request acks it (``{"kind": "ack",
  "from", "ids"}``, batched, after ACK_DELAY unless the reply went out
  first) and resends its reply until the caller acks it; the caller resends
  a request until it is acked or answered, unless the target announced
  without ``ack``. Both resend after ACK_FIRST, then every ACK_EVERY, for at most
  ACK_FOR (or until :meth:`forget`), holding at most RESEND_BYTES. A
  request seen again with the same id and ticket nonce is a resend: never
  delivered twice (a re-minted ticket for the same call id is new); a
  reply seen again is only acked. Resends going unanswered refresh our
  relay registration and route (a restarted relay forgot them).
- **Hub loss** (5.4, 7.4): the host calls :meth:`contact` whenever it
  accepts hub contact on a band (a verified hub announce or ticket; the
  transport can't verify signatures). After ``lost_after`` seconds without
  one, ``on_lost(band)`` fires once; the next contact fires ``on_contact``.
  A worker host drops that hub's elevation leases there.

What it does not do: verify anything (the worker gate and hub dispatch do),
compress (14.3; inbound compressed payloads are still inflated by the link
codec) or send native DROP frames (14.4; Drop rides pass-through packets).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rook.core.backoff import Backoff
from rook.worker.ota_drop import DropReplyServer, drop_id_of, is_drop_packet, valid_descriptor
from rook.worker.wire.codec import route_id_for

from .. import CORE_VERSION, wire
from . import drop
from .links import Link

log = logging.getLogger("rook.core2.transport")

FEATURES = ["drop", "route", "ack"]    # what this transport speaks; put it in announces (2.1)
BEAT_SECS = 30.0                # announce/heartbeat period, +-20 % (5.4, 5.5)
LOST_SECS = 90.0                # no hub contact this long: hub lost (5.4)
PEER_SECS = 90.0                # roster eviction (5.5)
DROP_THRESHOLD = 32 * 1024      # replies above this go over Drop when accepted (v1 14.2)
MAX_MESSAGE = 32 * 1024 * 1024  # largest inline message (the codec's reassembly bound)
BULK_MAX = 64 * 1024 * 1024     # bytes served over Drop at once, per band
BULK_TTL = 300.0                # seconds an unpulled offer stays served
PULL_SECS = 60.0                # default time to pull a large reply or a bundle
KEEP_SECS = 900.0               # request ids remembered (for rr, accept, drop pulls, resends)
KEEP_MAX = 10_000
ACK_DELAY = 0.2                 # acks wait this long, batched: a quick reply acks its request itself
ACK_FIRST = 1.0                 # an unacked request or reply goes again after this long,
ACK_EVERY = 2.0                 # then this often,
ACK_FOR = 60.0                  # for at most this long
ACK_IDS = 24                    # ids per ack message (one frame)
RESEND_BYTES = 8 * 1024 * 1024  # unacked messages held for resending, per band
TICK = 0.25                     # the band loop's period (resends, hub loss)

OnMessage = Callable[[str, dict], Awaitable[None]]
Make = Callable[[str], "dict | None | Awaitable[dict | None]"]
OnBand = Callable[[str], Any]


class TransportError(Exception):
    """A send that could not go out: no such band, link down, peer
    incompatible, message too large, or a failed pull."""


@dataclass
class Peer:
    node_id: str
    name: str | None = None
    stated: str | None = None           # the core version it announced, as sent
    core: str | None = None             # negotiated (2.2); None while unknown or incompatible
    features: list = field(default_factory=list)
    last_heard: float = 0.0             # time.monotonic()

    @property
    def compatible(self) -> bool:
        """False only once the peer stated a core version with nothing in
        common with ours; unknown counts as compatible (5.6)."""
        return self.stated is None or self.core is not None

    def alive(self, within: float = PEER_SECS) -> bool:
        return time.monotonic() - self.last_heard <= within


def _encode(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_request(msg: dict) -> bool:
    return isinstance(msg.get("cap"), str) and bool(msg["cap"])


def _is_reply(msg: dict) -> bool:
    return not _is_request(msg) and "id" in msg and "from" in msg and "ok" in msg


def _remember(table: dict, key: Any, value: Any) -> None:
    table.pop(key, None)
    table[key] = value
    while len(table) > KEEP_MAX:
        table.pop(next(iter(table)))


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _Band:
    """One band's connection: the link plus everything kept per band."""

    def __init__(self, t: "Transport", link: Link):
        self.t, self.link, self.band = t, link, link.band
        self.peers: dict[str, Peer] = {}
        self.sent: dict[str, float] = {}                    # our request id -> when, until answered
        self.answered: dict[str, float] = {}                # our request id -> when its reply came
        self.inbound: dict[str, list] = {}                  # their request id -> [rr, accept, when, nonce, replied]
        self.unacked: dict[str, list] = {}                  # id -> [data, route, due, resends, until]
        self.unacked_bytes = 0
        self.acks: dict[int, set[str]] = {}                 # route -> ids to ack
        self.ack_timer: asyncio.TimerHandle | None = None
        self.stale = False                                  # resends going unanswered
        self.waiting: dict[str, tuple[str | None, asyncio.Future]] = {}
        self.server = DropReplyServer(link.band_id, self._raw, worker_id=t.node_id,
                                      ttl=BULK_TTL, max_bytes=BULK_MAX)
        self.pulls: dict[int, Any] = {}                     # drop id -> receiver
        self.contact_at = 0.0
        self.lost = True
        self.task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self.link.set_route(self.t.node_id)
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        backoff = Backoff(base=self.t.backoff_base, cap=30.0)
        while True:
            try:
                await self.link.start(self._payload, self._connected)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = backoff.next()
                log.warning("band %s: link failed (%s); retrying in %.1fs", self.band[:8], e, delay)
                await asyncio.sleep(delay)
        every = self.t.every
        next_beat = time.monotonic() + every * random.uniform(0.8, 1.2)
        tick = min(TICK, self.t.lost_after / 4, every / 4)
        while True:
            now = time.monotonic()
            if now >= next_beat:
                next_beat = now + every * random.uniform(0.8, 1.2)
                if self.t.beat is not None:
                    await self._emit(self.t.beat)
            if not self.lost and now - self.contact_at > self.t.lost_after:
                self.lost = True
                self._fire(self.t.on_lost)
            self._prune(now)
            await self._resend(now)
            await asyncio.sleep(max(0.01, min(tick, next_beat - now)))

    async def stop(self) -> None:
        tasks = [self.task, *self.tasks] if self.task else list(self.tasks)
        for task in tasks:
            task.cancel()
        if self.ack_timer is not None:
            self.ack_timer.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _, fut in self.waiting.values():
            if not fut.done():
                fut.set_exception(TransportError("transport closed"))
        self.server.close()
        try:
            await self.link.stop()
        except Exception:
            log.debug("band %s: link stop failed", self.band[:8], exc_info=True)

    async def _connected(self) -> None:
        if self.t.hello is not None:
            await self._emit(self.t.hello)

    async def _emit(self, make: Make) -> None:
        try:
            msg = await _maybe(make(self.band))
            if msg:
                await self.send(msg)
        except Exception as e:
            log.debug("band %s: announce/heartbeat not sent: %s", self.band[:8], e)

    def _fire(self, cb: OnBand | None) -> None:
        if cb is None:
            return
        try:
            out = cb(self.band)
            if inspect.isawaitable(out):
                self._spawn(out)
        except Exception:
            log.exception("band %s: lost/contact callback failed", self.band[:8])

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _prune(self, now: float) -> None:
        for table, at in ((self.sent, lambda v: v), (self.answered, lambda v: v), (self.inbound, lambda v: v[2])):
            while table and now - at(next(iter(table.values()))) > KEEP_SECS:
                table.pop(next(iter(table)))
        for nid in [n for n, p in self.peers.items() if now - p.last_heard > KEEP_SECS]:
            del self.peers[nid]

    def contact(self) -> None:
        self.contact_at = time.monotonic()
        if self.lost:
            self.lost = False
            self._fire(self.t.on_contact)

    # -- outbound ----------------------------------------------------------------

    async def _raw(self, payload: bytes, route: int = 0) -> None:
        if not self.link.connected:
            raise TransportError(f"band {self.band[:8]}: link down")
        try:
            await self.link.send(payload, route)
        except TransportError:
            raise
        except Exception as e:
            raise TransportError(f"band {self.band[:8]}: send failed: {e}") from e

    def _route_to(self, node_id: Any) -> int:
        peer = self.peers.get(node_id) if isinstance(node_id, str) else None
        return route_id_for(node_id) if peer and "route" in peer.features else 0

    async def send(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            raise TransportError("a message is a JSON object")
        route, resend, reply = 0, None, False
        if _is_request(msg):
            target = msg.get("target")
            peer = self.peers.get(target) if isinstance(target, str) else None
            if peer and not peer.compatible:
                raise TransportError(f"incompatible: {target} speaks core {peer.stated!r}")
            accept = list(msg.get("accept") or [])
            msg = {**msg, "accept": accept + [f for f in ("drop", "ack") if f not in accept]}
            if self.link.route and "rr" not in msg:
                msg["rr"] = self.link.route
            rid = msg.get("id") if isinstance(msg.get("id"), str) else None
            if rid is not None:
                _remember(self.sent, rid, time.monotonic())
                self.answered.pop(rid, None)        # sent again (a re-minted ticket): a reply is new
                self._acked(rid)
                # Unheard peers too: a hub that just restarted calls workers
                # it remembers before their announce reaches the transport.
                if peer is None or "ack" in peer.features:
                    resend = rid
            route = self._route_to(target)
            data = _encode(msg)
        elif _is_reply(msg) and isinstance(msg.get("id"), str):
            reply = True
            seen = self.inbound.get(msg["id"])
            rr, accept = (seen[0], seen[1]) if seen else (0, ())
            if seen:
                seen[4] = True
                if "ack" in accept:
                    resend = msg["id"]
            route = rr
            data = _encode(msg)
            if len(data) > DROP_THRESHOLD and "drop" in accept:
                desc = self.server.serve(msg["id"], data, route=route)
                stub = {"id": msg["id"], "from": msg.get("from"), "ok": msg.get("ok")}
                data = _encode({**stub, "drop": desc} if desc else
                               {**stub, "ok": False, "error": f"reply too large: {len(data)} bytes"})
            elif len(data) > MAX_MESSAGE:
                data = _encode({"id": msg["id"], "from": msg.get("from"), "ok": False,
                                "error": f"reply too large: {len(data)} bytes"})
        else:
            data = _encode(msg)
        if len(data) > MAX_MESSAGE:
            raise TransportError(f"message too large: {len(data)} bytes")
        if resend is not None and reply:
            self._hold(resend, data, route)         # held first: a reply the link refused goes later
        await self._raw(data, route)
        if resend is not None and not reply and resend not in self.answered:    # answered meanwhile
            self._hold(resend, data, route)

    async def request(self, msg: dict, timeout: float) -> dict:
        rid = msg.get("id")
        if not isinstance(rid, str) or rid in self.waiting:
            raise TransportError("a request needs a fresh string id")
        fut = asyncio.get_running_loop().create_future()
        self.waiting[rid] = (msg.get("target"), fut)
        try:
            await self.send(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiting.pop(rid, None)
            self._acked(rid)

    # -- acks and resends --------------------------------------------------------

    def _hold(self, rid: str, data: bytes, route: int) -> None:
        """Keep ``data`` to send again until ``rid`` is acked."""
        self._acked(rid)
        if len(data) > RESEND_BYTES:
            return
        now = time.monotonic()
        self.unacked[rid] = [data, route, now + ACK_FIRST, 0, now + ACK_FOR]
        self.unacked_bytes += len(data)
        while self.unacked_bytes > RESEND_BYTES:
            self._acked(next(iter(self.unacked)))

    def _acked(self, rid: Any) -> None:
        held = self.unacked.pop(rid, None)
        if held is not None:
            self.unacked_bytes -= len(held[0])

    async def _resend(self, now: float) -> None:
        unanswered = False
        for rid, held in list(self.unacked.items()):
            if now >= held[4]:
                self._acked(rid)
            elif now >= held[2]:
                held[3] += 1
                held[2] = now + ACK_EVERY
                unanswered = unanswered or held[3] >= 2             # its second resend or later
                try:
                    await self._raw(held[0], held[1])
                except TransportError:
                    pass
        if unanswered and not self.stale:
            # A relay that restarted knows neither us nor our route until we
            # tell it: say both now rather than at the next keepalive.
            self.stale = True
            refresh = getattr(self.link, "refresh", None)
            if refresh is not None:
                self._spawn(refresh())

    def _ack(self, route: int, rid: str) -> None:
        """Ack ``rid`` soon, batched with others to the same route."""
        self.acks.setdefault(route, set()).add(rid)
        if self.ack_timer is None:
            self.ack_timer = asyncio.get_running_loop().call_later(ACK_DELAY, self._flush_acks)

    def _flush_acks(self) -> None:
        self.ack_timer = None
        acks, self.acks = self.acks, {}
        for route, ids in acks.items():
            # A request that was answered meanwhile is acked by its reply.
            ids = [i for i in ids if not (self.inbound.get(i) or (0, 0, 0, 0, False))[4]]
            for at in range(0, len(ids), ACK_IDS):
                data = _encode({"kind": "ack", "from": self.t.node_id, "ids": ids[at:at + ACK_IDS]})
                self._spawn(self._quiet(data, route))

    async def _quiet(self, data: bytes, route: int) -> None:
        try:
            await self._raw(data, route)
        except TransportError:
            pass

    def _heard_back(self) -> None:
        """An ack or reply came: if resends had gone unanswered, the band is
        back, so resend what is pending and announce at once (the far end
        may have lost us)."""
        if self.stale:
            self.stale = False
            for held in self.unacked.values():      # everything still pending goes now, not at its slot
                held[2] = 0.0
            if self.t.hello is not None:
                self._spawn(self._emit(self.t.hello))

    def offer(self, blob: bytes, to: str | None = None) -> dict:
        desc = self.server.serve(secrets.token_hex(16), bytes(blob), route=self._route_to(to))
        if desc is None:
            raise TransportError(f"too large to offer: {len(blob)} bytes (at most {BULK_MAX})")
        return desc

    async def pull(self, desc: Any, source: str | None, timeout: float) -> bytes:
        parsed = valid_descriptor(desc, BULK_MAX)
        if parsed is None:
            raise TransportError("malformed drop descriptor")
        if not self.link.connected:
            raise TransportError(f"band {self.band[:8]}: link down")
        data = await drop.pull(self.link.band_id, parsed, self._raw, route=self._route_to(source),
                               timeout=timeout, register=self._register)
        if data is None:
            raise TransportError(f"drop {parsed[0]}: not pulled within {timeout:g}s or hash mismatch")
        return data

    def _register(self, did: int, rx: Any) -> None:
        if rx is None:
            self.pulls.pop(did, None)
        else:
            self.pulls[did] = rx

    # -- inbound -----------------------------------------------------------------

    async def _payload(self, payload: bytes) -> None:
        if is_drop_packet(payload, self.link.band_id):
            if not self.server.feed(payload):
                rx = self.pulls.get(drop_id_of(payload))
                if rx is not None:
                    rx.feed(payload)
            return
        try:
            msg = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        now = time.monotonic()
        if _is_request(msg):
            if msg.get("target") != self.t.node_id:
                return
            rr, accept = msg.get("rr"), msg.get("accept")
            rr = rr if isinstance(rr, int) and not isinstance(rr, bool) and 0 < rr <= 0xFFFF else 0
            accept = tuple(a for a in accept if isinstance(a, str)) if isinstance(accept, list) else ()
            rid = msg.get("id")
            if isinstance(rid, str):
                ticket = msg.get("ticket")
                nonce = ticket.get("nonce") if isinstance(ticket, dict) else None
                seen = self.inbound.get(rid)
                if seen is not None and seen[3] == nonce:       # a resend: never run twice
                    held = self.unacked.get(rid)
                    if seen[4] and held is not None:
                        await self._quiet(held[0], held[1])     # our reply was lost: again, now
                    elif not seen[4] and "ack" in accept:
                        self._ack(rr, rid)
                    return
                _remember(self.inbound, rid, [rr, accept, now, nonce, False])
                if "ack" in accept:
                    self._ack(rr, rid)
        elif msg.get("kind") == "ack":
            ids = msg.get("ids")
            if isinstance(ids, list):
                for rid in ids[:4 * ACK_IDS]:
                    if isinstance(rid, str):
                        self._acked(rid)
                self._heard_back()
            return
        elif msg.get("kind") in ("announce", "heartbeat"):
            self._heard(msg.get("worker_id"), now, msg)
        elif _is_reply(msg):
            self._heard(msg.get("from"), now)
            rid = msg.get("id")
            if isinstance(rid, str) and (rid in self.sent or rid in self.answered):
                self._ack(self._route_to(msg.get("from")), rid)
                self._heard_back()
                if rid in self.answered:            # a resend of a reply we have
                    return
                del self.sent[rid]
                _remember(self.answered, rid, now)
                self._acked(rid)                    # the reply acks the request
                if "drop" in msg:
                    self._spawn(self._pull_reply(msg))
                    return
            if self._answer(msg):
                return
        elif not isinstance(msg.get("kind"), str):
            return
        await self.t._deliver(self.band, msg)

    def _heard(self, node_id: Any, now: float, msg: dict | None = None) -> None:
        if not isinstance(node_id, str) or not node_id or node_id == self.t.node_id:
            return
        peer = self.peers.get(node_id) or Peer(node_id)
        _remember(self.peers, node_id, peer)    # bounded: any PSK holder can invent ids
        peer.last_heard = now
        if msg is None:
            return
        if isinstance(msg.get("name"), str):
            peer.name = msg["name"]
        if "core" in msg:
            peer.stated = msg["core"] if isinstance(msg["core"], str) else repr(msg["core"])
            peer.core = wire.negotiate(CORE_VERSION, msg["core"])
        if isinstance(msg.get("features"), list):
            peer.features = [f for f in msg["features"] if isinstance(f, str)]

    def _answer(self, reply: dict) -> bool:
        """Complete a :meth:`request` waiting for this reply."""
        waiting = self.waiting.get(reply.get("id"))
        if waiting is None or (waiting[0] is not None and reply.get("from") != waiting[0]):
            return False
        if not waiting[1].done():
            waiting[1].set_result(reply)
        return True

    async def _pull_reply(self, stub: dict) -> None:
        full = None
        try:
            data = await self.pull(stub["drop"], stub.get("from"), PULL_SECS)
            full = json.loads(data)
        except (TransportError, ValueError, UnicodeDecodeError) as e:
            log.warning("band %s: large reply %s not pulled: %s", self.band[:8], stub.get("id"), e)
        if not (isinstance(full, dict) and full.get("id") == stub["id"] and full.get("from") == stub.get("from")):
            full = {"id": stub["id"], "from": stub.get("from"), "ok": False,
                    "error": "drop: the large reply could not be pulled"}
        if not self._answer(full):
            await self.t._deliver(self.band, full)


class Transport:
    """Core v2 messages on one or more bands; the same object on the hub and
    on a worker. ``node_id`` is this node's id on every band (a worker's
    ``worker_id``, the hub node's id): requests must target it, and it is
    the route this node declares."""

    def __init__(self, node_id: str, links: list[Link] = (), *,
                 hello: Make | None = None, beat: Make | None = None, every: float = BEAT_SECS,
                 on_lost: OnBand | None = None, on_contact: OnBand | None = None,
                 lost_after: float = LOST_SECS, backoff_base: float = 1.0):
        self.node_id = node_id
        self.hello, self.beat, self.every = hello, beat, every
        self.on_lost, self.on_contact, self.lost_after = on_lost, on_contact, lost_after
        self.backoff_base = backoff_base
        self.features = list(FEATURES)      # for this node's announces
        self.on_message: OnMessage | None = None
        self._bands: dict[str, _Band] = {}
        self._started = False
        for link in links:
            self._add(link)

    def _add(self, link: Link) -> _Band:
        if link.band in self._bands:
            raise ValueError(f"already on band {link.band}")
        b = self._bands[link.band] = _Band(self, link)
        return b

    def _band(self, band: str) -> _Band:
        b = self._bands.get(band)
        if b is None:
            raise TransportError(f"not on band {band}")
        return b

    @property
    def bands(self) -> list[str]:
        return list(self._bands)

    async def start(self, on_message: OnMessage) -> None:
        """Start every band's connection in the background and return."""
        self.on_message = on_message
        self._started = True
        for b in self._bands.values():
            b.start()

    async def add(self, link: Link) -> str:
        """Join one more band (a new membership); returns its band id."""
        b = self._add(link)
        if self._started:
            b.start()
        return link.band

    async def remove(self, band: str) -> None:
        """Leave a band (membership removed)."""
        b = self._bands.pop(band, None)
        if b is not None:
            await b.stop()

    async def close(self) -> None:
        self._started = False
        bands, self._bands = list(self._bands.values()), {}
        await asyncio.gather(*(b.stop() for b in bands))

    async def send(self, band: str, msg: dict) -> None:
        await self._band(band).send(msg)

    async def request(self, band: str, msg: dict, timeout: float) -> dict:
        """Send a request and wait for the reply from its ``target`` (which
        then skips ``on_message``). Raises TransportError or TimeoutError."""
        return await self._band(band).request(msg, timeout)

    def forget(self, band: str, rid: str) -> None:
        """Stop resending request ``rid``: the caller gave up on it."""
        b = self._bands.get(band)
        if b is not None:
            b._acked(rid)

    def offer(self, band: str, blob: bytes, to: str | None = None) -> dict:
        """Serve ``blob`` over Drop on ``band``; returns the descriptor
        ``{"id", "size", "sha256"}`` for the receiver to :meth:`pull`."""
        return self._band(band).offer(blob, to)

    async def pull(self, band: str, desc: dict, source: str | None = None,
                   timeout: float = PULL_SECS) -> bytes:
        """Pull an offered blob from ``source``; checks its SHA-256."""
        return await self._band(band).pull(desc, source, timeout)

    def peers(self, band: str) -> dict[str, Peer]:
        return dict(self._band(band).peers)

    def connected(self, band: str) -> bool:
        return self._band(band).link.connected

    def contact(self, band: str) -> None:
        """The host accepted hub contact on ``band`` (5.4)."""
        self._band(band).contact()

    def lost(self, band: str) -> bool:
        return self._band(band).lost

    async def _deliver(self, band: str, msg: dict) -> None:
        if self.on_message is None:
            return
        try:
            await self.on_message(band, msg)
        except Exception:
            log.exception("band %s: message handler failed", band[:8])
