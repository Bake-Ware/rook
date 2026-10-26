"""Band client: joins a Telesthete band via the hub, tracks workers + caps,
fires capability calls, awaits matching replies via futures.

This is the read-side companion to :mod:`rook.worker.core`. It speaks the
same JSON wire format and reuses the worker's hub transport for I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid

from ..core.aiocompat import timeout as _timeout
from ..core.devices import clean_device
from ..core.facts import clean_facts
from ..worker.transports.telesthete_hub import TelestheteHubTransport
from ..worker.wire import envelope
from ..worker.wire.codec import route_id_for

log = logging.getLogger("rook.band_mcp.client")

# Idle-worker eviction: drop a worker we haven't heard from in this many
# seconds. Workers re-announce every 30s by default, so 90s tolerates a
# couple missed broadcasts before they disappear from `rook_workers()`.
WORKER_STALE_SECS = 90.0
# The hub's own node (hub-placed plugins, served as worker "rook") announces on
# the same cadence as workers.
LOCAL_ANNOUNCE_SECS = 30.0
# What this client can take in a reply (request ``accept``, core spec 14).
CLIENT_ACCEPT = [envelope.A_DROP, envelope.A_DROP_NATIVE, envelope.A_Z]
# Largest reply this client pulls over Drop (a descriptor announcing more is
# refused).
MAX_PULL_BYTES = envelope.DEFAULT_MAX_REPLY_BYTES
# Only used to tell a caller whose timeout ran out during a large-reply pull
# how long a pull of that size needs (the caller's timeout is never extended).
PULL_MIN_SECS = 15.0
PULL_BYTES_PER_SEC = 256 * 1024
# A worker that sent a hash-only announce we can't resolve is asked for its
# cap list at most this often.
CAPS_FETCH_EVERY = 30.0
# Single-fragment budget of the §6.6 envelope. Extension keys are left off a
# request to a node that never announced them if they alone would push the
# request past one fragment (the ESP32/RP2040 firmware reassembles nothing).
_ONE_FRAGMENT = 1003


def _denial(decision, target: str | None) -> dict:
    """A policy denial in the shape of a band reply (permissions 3.7)."""
    return {"id": uuid.uuid4().hex, "from": target or "rook", **decision.denial()}


def _authorize(authz, cap: str, target: str | None, entry: dict | None,
               identity: str | None, principal, local: bool = False, args: dict | None = None):
    """Run E1 (permissions 3.5). Returns the Decision, or None when this
    client has no authorizer (tests, tools that never enforce)."""
    if authz is None:
        return None
    return authz.check(cap, target, entry, identity=identity, principal=principal, local=local,
                       args=args)


async def _settle(authz, decision, args: dict | None, target: str | None):
    """Wait out an approval hold before anything is sent (permissions 3.11).
    Returns ``(decision, reply)``: ``reply`` is set when the call must not be
    sent (a person denied it, or it is still held when the wait ends). The
    wait comes before the call's own timeout starts."""
    if authz is None or decision is None or not decision.held:
        return decision, None
    out = await authz.resolve(decision, args or {})
    if isinstance(out, dict):
        return None, {"id": uuid.uuid4().hex, "from": target or "rook", **out}
    if out.denied:
        return out, _denial(out, target)
    return out, None


async def _call_local(node, cap: str, args: dict | None, identity: str | None,
                      timeout: float) -> dict:
    """In-process call to the hub node: same reply shape as a band reply."""
    mid = uuid.uuid4().hex
    try:
        async with _timeout(timeout):
            body = await node.dispatch(cap, args or {}, identity, source="local")
    except TimeoutError as e:
        raise _as_asyncio_timeout(e)
    return {"id": mid, "from": node.worker_id, **body}


def _as_asyncio_timeout(e: BaseException) -> BaseException:
    """``call`` raises ``asyncio.TimeoutError``: the same class as the builtin
    on 3.11+, a different one on 3.10, where aiocompat's timeout raises the
    builtin and callers catch ``asyncio.TimeoutError``."""
    if isinstance(e, asyncio.TimeoutError):
        return e
    err = asyncio.TimeoutError(*e.args)
    err.__cause__ = e
    return err


class WorkerEntry(dict):
    """Just a dict with named keys for readability."""

    @property
    def worker_id(self) -> str:
        return self["worker_id"]

    @property
    def name(self) -> str:
        return self.get("name", self["worker_id"])

    @property
    def caps(self) -> list[str]:
        return self.get("caps", [])

    @property
    def last_seen(self) -> float:
        return self.get("last_seen", 0.0)


class BandClient:
    def __init__(self, psk: str, hub_host: str = "127.0.0.1",
                 hub_port: int = 7474, use_ws: bool = False,
                 route_name: str | None = None) -> None:
        self.transport = TelestheteHubTransport(
            psk=psk, hub_host=hub_host, hub_port=hub_port, use_ws=use_ws,
        )
        # The band's enrollment key hash (enrollment.db ``bands.psk_hash``):
        # how grants on a band find the workers seen on it.
        self.psk_hash = hashlib.sha256(psk.encode("utf-8")).hexdigest()
        # Our route identity (telesthete SPEC §10.1): replies to our calls
        # carry ``rr`` = route_id_for(this) and a routing hub delivers them to
        # us alone. Stable for the process; attach_local() replaces it with
        # the hub node's id, so band calls to `rook` are addressable too.
        self.route_name = route_name or f"client:{uuid.uuid4().hex}"
        if envelope.routing_enabled():
            self.transport.set_route(self.route_name)
        # In-flight Drop pulls of large replies, by drop_id, and the lock (with
        # its user count) that serializes pulls of one drop_id; removed when
        # the last user is done.
        self._drop_rx: dict[int, object] = {}
        self._drop_locks: dict[int, list] = {}
        # Hash-only announces we could not resolve: worker_id -> last fetch.
        self._caps_fetch: dict[str, float] = {}
        self._caps_tasks: set[asyncio.Task] = set()
        self.workers: dict[str, WorkerEntry] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._stopping = False
        self._gc_task: asyncio.Task | None = None
        # Active in-band OTA push (telesthete Drop), if any. Inbound
        # REQUEST/DONE packets from the receiving worker route here.
        self._ota_sender = None
        # The hub's own node (rook.hub.node.HubNode), if attached: announced on
        # this band as worker "rook" and answering requests addressed to it.
        self._local = None
        self._local_task: asyncio.Task | None = None
        self._local_calls: set[asyncio.Task] = set()
        self._started = False
        # Hub-side policy enforcement + ticket signing (rook.hub.authz). None
        # = no evaluation (the build-167 world); the MCP bridge and dashboard
        # install one.
        self.authz = None
        self._impostors: set[str] = set()

    @property
    def band_hex(self) -> str:
        return self.transport.band_id.hex()

    def _anchors(self) -> list[str]:
        if self.authz is not None:
            return self.authz.anchors()
        from ..core.authz import default_anchors
        return default_anchors()

    # -- local (hub) node ----------------------------------------------------

    def attach_local(self, node) -> None:
        """Serve ``node`` (a HubNode) on this band: it joins the roster, is
        announced every ~30s, and band requests for it are dispatched to it.
        Calls from this client to its id short-circuit in-process."""
        self._local = node
        self.workers[node.worker_id] = WorkerEntry(node.entry())
        # One route per hub peer: the node's id, so workers can address both
        # replies (rr) and calls to `rook` to this client.
        self.route_name = node.worker_id
        if envelope.routing_enabled():
            self.transport.set_route(self.route_name)
        if self._started:
            self._start_local_announcer()

    def _start_local_announcer(self) -> None:
        if self._local is None or self._local_task is not None:
            return
        try:
            self._local_task = asyncio.get_running_loop().create_task(self._local_loop())
        except RuntimeError:
            self._local_task = None  # no loop yet; start() will pick it up

    async def _announce_local(self) -> None:
        node = self._local
        if node is None:
            return
        self.workers[node.worker_id] = WorkerEntry(node.entry())
        msg = node.announce_msg()
        if self.transport.route_id is not None:
            # Unsigned additive key: this node is addressable (core spec 14).
            msg["features"] = [envelope.F_ROUTE]
        if self.authz is not None:
            # is_hub grant + op-key signature: proves this node is `rook`.
            msg = self.authz.decorate_announce(msg, self.band_hex)
        try:
            await self.transport.send(json.dumps(msg).encode())
        except Exception:
            log.debug("hub node announce failed", exc_info=True)

    async def _local_loop(self) -> None:
        while not self._stopping:
            try:
                await self._announce_local()
                await asyncio.sleep(LOCAL_ANNOUNCE_SECS)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("hub node announce loop failed")
                await asyncio.sleep(LOCAL_ANNOUNCE_SECS)

    async def _on_connect(self) -> None:
        if self._local is not None:
            await self._announce_local()

    def _serve_local(self, msg: dict) -> None:
        node = self._local
        if node is None or not node.wants(msg):
            return
        task = asyncio.get_running_loop().create_task(self._answer_local(node, msg))
        self._local_calls.add(task)
        task.add_done_callback(self._local_calls.discard)

    async def _answer_local(self, node, msg: dict) -> None:
        decision = None
        if self.authz is not None:
            # Band-originated: any PSK holder, so no authenticated principal.
            from ..hub.authz import BAND_UNAUTHENTICATED
            decision = _authorize(self.authz, msg.get("cap") or "", node.worker_id,
                                  node.entry(), msg.get("identity"), BAND_UNAUTHENTICATED,
                                  local=True, args=msg.get("args"))
        refused = None
        if decision is not None and not decision.denied:
            decision, refused = await _settle(self.authz, decision, msg.get("args", {}),
                                              node.worker_id)
        if refused is not None:
            body = {k: v for k, v in refused.items() if k not in ("id", "from")}
        elif decision is not None and decision.denied:
            body = decision.denial()
        else:
            body = await node.dispatch(msg.get("cap"), msg.get("args", {}),
                                       msg.get("identity"), source="band")
        reply = {"from": node.worker_id, **body}
        if msg.get("id") is not None:
            reply = {"id": msg["id"], **reply}
        opts = envelope.reply_opts(msg)
        try:
            await self._send(json.dumps(reply).encode(), opts.route)
        except Exception:
            log.exception("hub node reply send failed")

    async def _send(self, payload: bytes, route: int = 0) -> None:
        # Same rule as Worker._send: address only through a routing transport.
        if route and getattr(self.transport, "route_id", None) is not None:
            await self.transport.send(payload, route=route)
        else:
            await self.transport.send(payload)

    async def start(self) -> None:
        await self.transport.start(self._on_message, on_connect=self._on_connect)
        self._started = True
        self._gc_task = asyncio.create_task(self._gc_loop())
        self._start_local_announcer()
        log.info("band-mcp client up (band_id=%s, hub=%s:%d)",
                 self.transport.band_id.hex()[:16],
                 self.transport.hub[0], self.transport.hub[1])

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._gc_task, self._local_task, *self._local_calls,
                     *self._caps_tasks):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        await self.transport.stop()

    # -- inbound -------------------------------------------------------------

    async def _on_message(self, payload: bytes, peer_id: tuple) -> None:
        # Binary Drop packets: chunks of a large reply we are pulling, or the
        # REQUEST/DONE of an active in-band OTA push. Route them by drop_id
        # before JSON parsing.
        if self._drop_rx or self._ota_sender is not None:
            from ..worker.ota_drop import drop_id_of, is_drop_packet
            if is_drop_packet(payload, self.transport.band_id):
                rx = self._drop_rx.get(drop_id_of(payload))
                if rx is not None:
                    rx.feed(payload)
                elif self._ota_sender is not None:
                    self._ota_sender.feed(payload)
                return

        try:
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        if msg.get("kind") == "announce":
            self._handle_announce(msg)
            return

        # A capability request: only the hub node (if attached) answers.
        if msg.get("cap") and "ok" not in msg:
            if self._local is not None:
                self._serve_local(msg)
            return

        # Looks like a reply if it has an id, "from", and an "ok" boolean.
        if "id" in msg and "ok" in msg and "from" in msg:
            self._handle_reply(msg)
            return

    def _handle_announce(self, msg: dict) -> None:
        wid = msg.get("worker_id")
        if not wid:
            return
        local = self._local
        if local is not None and wid == local.worker_id:
            return  # our own announce echoed back
        name = msg.get("name", wid)
        # Roles only from verified grants whose key signed this announce
        # (permissions 4.4); self-reported facts never count.
        roles: frozenset = frozenset()
        if "grants" in msg:
            from ..core.facts import roles_from_announce
            try:
                roles = roles_from_announce(msg, self.band_hex, self._anchors())
            except Exception:
                roles = frozenset()
        claims_rook = isinstance(name, str) and name.lower() == "rook"
        quarantined = False
        if claims_rook and (local is not None or "is_hub" not in roles):
            # "rook" is reserved for the hub node (4.6): only the holder of a
            # valid is_hub grant for this band gets the name. Anyone else is
            # quarantined under a disambiguated name, so name resolution stays
            # exact, and journaled as an impostor (once per id).
            name = f"{name}~{str(wid)[:8]}"
            quarantined = local is None or "is_hub" not in roles
            if quarantined and wid not in self._impostors:
                self._impostors.add(wid)
                log.warning("ROOK AUTHZ ALERT: %s announced the reserved name 'rook' "
                            "without a valid is_hub grant; quarantined as %s", wid, name)
                if self.authz is not None:
                    self.authz.record_event("audit.impostor", name,
                                            {"worker_id": wid, "claimed": "rook"})
        facts = msg.get("facts")
        entry = self.workers.get(wid) or WorkerEntry()
        caps = self._announced_caps(wid, msg, entry)
        tiers = msg.get("tiers")
        entry["tiers"] = ({str(k): str(v) for k, v in list(tiers.items())[:2000]}
                          if isinstance(tiers, dict) else {})
        entry["authz"] = msg["authz"] if isinstance(msg.get("authz"), dict) else {}
        entry["roles"] = sorted(roles)
        entry["claims_rook"] = claims_rook
        entry["quarantined"] = quarantined
        entry.update({
            "worker_id": wid,
            "name": name,
            "description": msg.get("description", "")[:280] if isinstance(msg.get("description", ""), str) else "",
            "caps": caps,
            "caps_hash": msg.get("caps_hash") if isinstance(msg.get("caps_hash"), str) else None,
            "features": sorted(envelope.features_of(msg)),
            "plugins": list(msg.get("plugins", [])),
            "hb": dict(msg.get("hb") or {}),
            "version": msg.get("version"),
            "build": msg.get("build"),
            "app_release": msg.get("app_release") if isinstance(msg.get("app_release"), dict) else {},
            # Self-reported platform/hardware facts (absent on build-167 workers).
            "facts": clean_facts(facts),
            "last_seen": time.time(),
        })
        # A device worker (rook.core.devices) is one its delegate answers for.
        # Its claim is kept apart and only becomes ``delegate``/``device``
        # once the delegate's own announce lists it (_link_device), so no
        # worker can make itself a "device" (undeauthable, never updated) by
        # saying so.
        claim = msg.get("delegate")
        entry["delegate_claim"] = (claim if isinstance(claim, str) and claim
                                   and claim != wid else None)
        entry["device_info"] = (clean_device(msg.get("device"))
                                if entry["delegate_claim"] else None)
        listed = msg.get("devices")
        entry["devices"] = ([str(d) for d in listed[:1000]]
                            if isinstance(listed, list) else [])
        if wid not in self.workers:
            log.info("worker joined: id=%s name=%s caps=%d",
                     wid, entry["name"], len(entry["caps"]))
        self.workers[wid] = entry
        self._link_device(wid)
        self._prune_devices(wid)
        for cid in entry["devices"]:
            self._link_device(cid)

    def _link_device(self, cid: str) -> None:
        """Set ``delegate``/``device`` on entry ``cid`` when the delegate it
        claims is a live worker (not itself a device) whose own announce lists
        it; clear them otherwise."""
        e = self.workers.get(cid)
        if e is None:
            return
        claim = e.get("delegate_claim")
        d = self.workers.get(claim) if claim else None
        ok = d is not None and not d.get("delegate") and cid in (d.get("devices") or ())
        e["delegate"] = claim if ok else None
        e["device"] = e.get("device_info") if ok else None

    def _prune_devices(self, wid: str) -> None:
        """Drop devices of delegate ``wid`` that its latest announce no longer
        lists (``devices``, core spec 14.6), at once rather than 90 s later.
        Only linked devices: a worker merely claiming ``wid`` stays a worker."""
        keep = set(self.workers[wid].get("devices") or ())
        for cid in [c for c, e in self.workers.items()
                    if e.get("delegate") == wid and c not in keep]:
            log.info("device dropped by its delegate: id=%s name=%s",
                     cid, self.workers[cid].get("name", cid))
            self.workers.pop(cid, None)

    def _announced_caps(self, wid: str, msg: dict, entry: dict) -> list:
        """The cap list for an announce. A full announce carries ``caps``. A
        trimmed one (ROOK_ANNOUNCE_CAPS_HASH on the worker) carries only
        ``caps_hash``: keep what we know if the hash matches; otherwise keep
        the old list for now and fetch the real one with caps.describe."""
        caps = msg.get("caps")
        if isinstance(caps, list):
            return list(caps)
        chash = msg.get("caps_hash")
        known = list(entry.get("caps") or [])
        if not isinstance(chash, str):
            return known  # neither key: an odd announce; keep what we had
        if known and envelope.caps_hash(known) == chash:
            return known
        self._fetch_caps(wid, chash)
        return known

    def _fetch_caps(self, wid: str, chash: str) -> None:
        now = time.time()
        if now - self._caps_fetch.get(wid, 0.0) < CAPS_FETCH_EVERY:
            return
        self._caps_fetch[wid] = now
        try:
            task = asyncio.get_running_loop().create_task(self._refresh_caps(wid, chash))
        except RuntimeError:
            return
        self._caps_tasks.add(task)
        task.add_done_callback(self._caps_tasks.discard)

    async def _refresh_caps(self, wid: str, chash: str) -> None:
        from ..hub.authz import system
        try:
            reply = await self.call("caps.describe", target=wid, timeout=15.0,
                                    principal=system("roster"))
        except Exception as e:
            log.debug("caps.describe for %s failed: %s", wid, e)
            return
        result = reply.get("result") if reply.get("ok") else None
        if not isinstance(result, dict):
            return
        caps = sorted(str(c) for c in result)
        entry = self.workers.get(wid)
        if entry is None:
            return
        if envelope.caps_hash(caps) != chash:
            log.info("caps.describe of %s does not match its announced caps_hash; "
                     "using it anyway", wid)
        entry["caps"] = caps
        self._caps_fetch.pop(wid, None)

    def _handle_reply(self, msg: dict) -> None:
        mid = msg.get("id")
        fut = self._pending.pop(mid, None)
        if fut is None:
            return  # late or unsolicited
        if not fut.done():
            fut.set_result(msg)
        # Replies also count as a sign of life.
        from_id = msg.get("from")
        if from_id and from_id in self.workers:
            self.workers[from_id]["last_seen"] = time.time()

    # -- outbound ------------------------------------------------------------

    async def call(self, cap: str, args: dict | None = None,
                   target: str | None = None, timeout: float = 15.0,
                   identity: str | None = None, principal=None,
                   _decision=None) -> dict:
        """Send a capability call and wait for the first matching reply.

        ``identity`` is the caller's display identity (from the bearer-token
        name); it rides in the envelope so the target worker can audit who
        called. ``principal`` (a :class:`rook.hub.policy.Principal`) is who the
        call is authorized for; by default the one the MCP wrapper or dashboard
        middleware put in context, else a ``system:*`` principal.

        With an authorizer installed the call is evaluated first (a denial in
        ``enforce`` mode comes back as an ``ok: false`` reply with ``denied``
        and nothing is sent) and targeted calls carry a signed ticket.

        Returns the reply dict (``{"id", "from", "ok", "result"|"error"}``).
        Raises ``asyncio.TimeoutError`` on no reply. ``timeout`` bounds the
        whole call, including pulling a large reply over Drop (core spec
        14.2): a pull that can't finish in what is left of it is abandoned and
        the call returns ``ok: false`` with an error starting ``timeout:``
        (the worker did run the call). Give big transfers a big ``timeout``.
        """
        local = self._local is not None and target == self._local.worker_id
        decision = _decision
        if decision is None:
            entry = self._local.entry() if local else self.workers.get(target) if target else None
            decision = _authorize(self.authz, cap, target, entry, identity, principal, local=local,
                                  args=args)
        if decision is not None and decision.denied:
            return _denial(decision, target)
        decision, refused = await _settle(self.authz, decision, args, target)
        if refused is not None:
            return refused
        if local:
            return await _call_local(self._local, cap, args, identity, timeout)
        if len(self._pending) >= 512:
            raise RuntimeError("band call capacity reached")
        entry = self.workers.get(target) if target else None
        feats = envelope.features_of(entry)
        mid = self._new_id(target)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict = {"id": mid, "cap": cap, "args": args or {}}
        if target:
            msg["target"] = target
        if identity:
            msg["identity"] = identity
        if target and self.authz is not None and decision is not None:
            ticket = self.authz.ticket(decision, band=self.band_hex, target=target, msg_id=mid,
                                       args=msg["args"], entry=self.workers.get(target))
            if ticket is not None:
                msg["ticket"] = ticket  # build-167 workers ignore unknown keys
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            payload, route = self._encode_request(msg, target, feats)
            if not route:
                route = self._delegate_route(entry)
            async with _timeout(timeout):
                await self._send(payload, route)
                reply = await fut
        except TimeoutError as e:
            raise _as_asyncio_timeout(e)
        finally:
            self._pending.pop(mid, None)
            if not fut.done():
                fut.cancel()
        if "drop" in reply and "result" not in reply and "error" not in reply:
            reply = await self._pull_reply(reply, max(deadline - loop.time(), 0.0))
        return reply

    def _delegate_route(self, entry: dict | None) -> int:
        """A device declares no route of its own (its delegate answers for
        it), so address the call to the delegate's route when it has one:
        the delegate alone receives it instead of the whole band. 0 = send
        it broadcast, as before."""
        delegate = (entry or {}).get("delegate")
        d = self.workers.get(delegate) if delegate else None
        if d is None or not self.transport.route_id \
                or envelope.F_ROUTE not in envelope.features_of(d):
            return 0
        return route_id_for(delegate)

    def _new_id(self, target: str | None) -> str:
        """A request id whose reply drop id from ``target`` (see
        ota_drop.reply_drop_id) no pull of ours is using, so two of our pulls
        never share a drop_id (they would feed each other's receiver)."""
        from ..worker.ota_drop import reply_drop_id
        mid = uuid.uuid4().hex
        if not target or not self._drop_rx:
            return mid
        for _ in range(8):
            if reply_drop_id(mid, target) not in self._drop_rx:
                break
            mid = uuid.uuid4().hex
        return mid

    def _encode_request(self, msg: dict, target: str | None,
                        feats: frozenset) -> tuple[bytes, int]:
        """Serialize a request with the extensions the target can use:
        ``rr``/``accept`` (ignored by older workers), addressed delivery to a
        target that declares a route, compression to one that announced
        ``z``. Returns ``(payload, route)``: the target's route id when
        the request is to be ADDRESSED to it, else 0 (broadcast)."""
        base = json.dumps(msg).encode()
        rid = self.transport.route_id
        ext = dict(msg)
        if rid:
            ext["rr"] = rid
        ext["accept"] = CLIENT_ACCEPT
        payload = json.dumps(ext).encode()
        if not feats and len(base) <= _ONE_FRAGMENT < len(payload):
            payload = base  # never make a firmware-sized request multi-fragment
        if envelope.F_Z in feats:
            payload = envelope.compress(payload)
        # Address only a target that declared its route: a target that never
        # declared would miss a frame whose id another peer's name collides on
        # (and the TS/Rust example ports drop ADDRESSED frames outright).
        route = route_id_for(target) if target and envelope.F_ROUTE in feats and rid else 0
        return payload, route

    async def _pull_reply(self, reply: dict, remaining: float) -> dict:
        """Fetch a large reply served over Drop (``{"drop": {id, size,
        sha256}}``), verify it and return the reply it stands for, so callers
        never see the descriptor. Pulls from the replier (``from``), which for
        an open call is whichever worker answered first. ``remaining`` is what
        is left of the caller's timeout; the pull never runs past it."""
        from ..worker.ota_drop import pull_reply, valid_descriptor
        base = {k: reply[k] for k in ("id", "from") if k in reply}
        desc = valid_descriptor(reply.get("drop"), MAX_PULL_BYTES)
        if desc is None:
            return {**base, "ok": False, "error": "bad drop descriptor in reply"}
        did, size, _sha = desc
        replier = reply.get("from") if isinstance(reply.get("from"), str) else None
        feats = envelope.features_of(self.workers.get(replier) if replier else None)
        route = (route_id_for(replier) if replier and envelope.F_ROUTE in feats
                 and self.transport.route_id else 0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(remaining, 0.0)

        def timed_out() -> dict:
            # Rough guide for the caller: what a pull of this size needs.
            need = PULL_MIN_SECS + size / PULL_BYTES_PER_SEC
            return {**base, "ok": False,
                    "error": f"timeout: large reply ({size} bytes, over Drop) was not "
                             f"fetched within the call's timeout; allow about "
                             f"{need:.0f}s for a reply this size"}

        def register(drop_id: int, rx) -> None:
            if rx is None:
                self._drop_rx.pop(drop_id, None)
            else:
                self._drop_rx[drop_id] = rx

        # Two pulls of one drop_id at once would feed each other's receiver;
        # _new_id makes that rare, this makes it safe. The entry goes away
        # with its last user, so the map holds only drop ids being pulled.
        entry = self._drop_locks.get(did)
        if entry is None:
            entry = self._drop_locks[did] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            try:
                async with _timeout(max(deadline - loop.time(), 0.0)):
                    await entry[0].acquire()
            except TimeoutError:
                return timed_out()
            try:
                left = deadline - loop.time()
                blob = (await pull_reply(self.transport.band_id, desc, self._send,
                                         route=route, timeout=left, register=register)
                        if left > 0 else None)
            finally:
                entry[0].release()
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._drop_locks.get(did) is entry:
                del self._drop_locks[did]
        if blob is None:
            if loop.time() >= deadline:
                return timed_out()
            return {**base, "ok": False,
                    "error": f"large reply ({size} bytes) over Drop failed its sha256 check"}
        try:
            full = json.loads(envelope.decode(blob))
        except ValueError:
            full = None
        if not isinstance(full, dict) or full.get("id") != reply.get("id"):
            return {**base, "ok": False, "error": "large reply over Drop was not a reply"}
        from_id = full.get("from")
        if from_id and from_id in self.workers:
            self.workers[from_id]["last_seen"] = time.time()
        return full

    async def push_update(self, worker_id: str, bundle: bytes, manifest: dict,
                          *, drop_id: int = 0, name: str = "band-worker.pyz",
                          begin_timeout: float = 15.0,
                          transfer_timeout: float = 300.0) -> dict:
        """Push a worker bundle **in band** over telesthete Drop (§8), instead of
        making the worker fetch it over HTTP. Use when the target has no path to
        the installer/CDN (strict NAT, blocked egress).

        Flow: (1) call ``worker.ota_begin`` with the ed25519-signed ``manifest``
        so the worker verifies the signature and arms a Drop receiver; (2) offer
        the bytes and serve the chunks it requests; (3) return once the worker
        reports its sha-verified DONE (it then swaps + restarts on its own).

        ``manifest`` must be the same signed object the worker verifies (build,
        sha256, …). ``drop_id`` must be below 0x8000 (higher ids carry large
        replies). Returns a status dict.
        """
        from ..worker.ota_drop import REPLY_DROP_BASE, OtaDropSender, valid_ota_drop_id

        if not valid_ota_drop_id(drop_id):
            # 0x8000 and up are large-reply drop ids (core spec 14.2): a push
            # there would cross-feed with a reply transfer of the same id.
            return {"ok": False, "stage": "begin",
                    "error": f"drop_id must be 0-{REPLY_DROP_BASE - 1}, got {drop_id!r}"}
        begin = await self.call("worker.ota_begin",
                                args={"manifest": manifest, "drop_id": drop_id},
                                target=worker_id, timeout=begin_timeout)
        # The reply envelope's "ok" only says the capability ran; the worker's
        # own verdict is in result["ok"] (fails closed on a bad signature).
        if not begin.get("ok"):
            return {"ok": False, "stage": "begin", "reply": begin}
        result = begin.get("result", {}) or {}
        if not result.get("ok"):
            # Worker declined: bad manifest signature, already in progress, …
            return {"ok": False, "stage": "begin", "result": result}
        if result.get("action") != "receiving":
            # Up-to-date (or held) — nothing to transfer, but not a failure.
            return {"ok": True, "stage": "begin", "result": result}

        if self._ota_sender is not None:
            return {"ok": False, "error": "another in-band push is already active"}

        feats = envelope.features_of(self.workers.get(worker_id))
        # One native DROP frame per Drop packet when the worker takes them
        # (half the frames of the pass-through form; core spec 14.3),
        # otherwise pass-through, addressed when the worker declares a route.
        native = (self.transport.send_native
                  if envelope.F_DROP_NATIVE in feats and envelope.drop_native_enabled()
                  else None)
        route = (route_id_for(worker_id) if envelope.F_ROUTE in feats
                 and self.transport.route_id else 0)
        sender = OtaDropSender(self.transport.band_id, drop_id, name, bundle,
                               self._send, route=route, native=native)
        self._ota_sender = sender
        try:
            sender.offer()
            ok = await sender.wait(timeout=transfer_timeout)
            return {"ok": bool(ok), "stage": "transfer",
                    "sha256": sender.sha256, "chunks": sender.total_chunks,
                    "verified": bool(ok), "native": native is not None,
                    "frames": sender.frames_sent}
        finally:
            self._ota_sender = None

    async def _gc_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(15.0)
                cutoff = time.time() - WORKER_STALE_SECS
                local_id = self._local.worker_id if self._local is not None else None
                stale = [wid for wid, w in self.workers.items()
                         if w["last_seen"] < cutoff and wid != local_id]
                for wid in stale:
                    name = self.workers[wid].get("name", wid)
                    log.info("worker stale, evicting: id=%s name=%s", wid, name)
                    self.workers.pop(wid, None)
                # A delegate gone takes its devices with it: nothing answers
                # for them any more.
                for wid in [c for c, e in self.workers.items()
                            if e.get("delegate") and e["delegate"] not in self.workers]:
                    self.workers.pop(wid, None)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("gc loop failed")


class MultiBandClient:
    """Join several bands (one PSK each) over a single shared hub.

    The Telesthete hub relays by ``band_id`` and holds no PSK, so one hub
    carries many bands at once. This wraps one :class:`BandClient` per PSK and
    presents the same surface as a single client — a merged ``workers`` roster
    (each entry tagged with the ``band`` it was seen on) and a ``call()`` that
    routes to the band hosting the target worker. Used to run a new band
    alongside an old one during a PSK rotation, then drop the old PSK.

    ``build_server`` treats this interchangeably with :class:`BandClient`.
    """

    def __init__(self, psks, hub_host: str = "127.0.0.1",
                 hub_port: int = 7474, use_ws: bool = False) -> None:
        # Every band's client shares one route name (route ids are per band,
        # so this costs nothing and keeps replies addressable on all of them).
        self.route_name = f"client:{uuid.uuid4().hex}"
        deduped: list[str] = []
        for p in psks:
            p = (p or "").strip()
            if p and p not in deduped:
                deduped.append(p)
        # Empty is valid after the last band is revoked; the controller stays
        # available for enrollment management without retaining the old key.
        self._clients = [
            BandClient(psk=p, hub_host=hub_host, hub_port=hub_port, use_ws=use_ws,
                       route_name=self.route_name)
            for p in deduped
        ]
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.use_ws = use_ws
        self._membership_lock = asyncio.Lock()
        self._local = None
        self._authz = None

    @property
    def authz(self):
        return self._authz

    @authz.setter
    def authz(self, value) -> None:
        """One authorizer for every band (current and later-added)."""
        self._authz = value
        for c in self._clients:
            c.authz = value
        attach = getattr(value, "attach_client", None)
        if callable(attach):
            attach(self)  # the approval contact reaches the operator through us

    def attach_local(self, node) -> None:
        """Serve the hub node on every band (current and later-added)."""
        self._local = node
        for c in self._clients:
            c.attach_local(node)

    async def add_band(self, psk: str) -> None:
        from telesthete.protocol.crypto import derive_band_id
        band_id = derive_band_id(psk)
        async with self._membership_lock:
            if any(c.transport.band_id == band_id for c in self._clients):
                return
            client = BandClient(psk=psk, hub_host=self.hub_host,
                                hub_port=self.hub_port, use_ws=self.use_ws,
                                route_name=self.route_name)
            client.authz = self._authz
            if self._local is not None:
                client.attach_local(self._local)
            try:
                await client.start()
            except BaseException:
                await client.stop()
                raise
            client.label = band_id.hex()[:8]
            self._clients.append(client)

    async def remove_band(self, label: str) -> None:
        async with self._membership_lock:
            removed = [c for c in self._clients if c.transport.band_id.hex()[:8] == label]
            self._clients = [c for c in self._clients if c not in removed]
            for client in removed:
                await client.stop()

    async def sync_bands(self, psks: list[str]) -> None:
        from telesthete.protocol.crypto import derive_band_id
        wanted = {derive_band_id(p) for p in psks}
        # Leave retired bands before opening new ones. Never forward a new PSK
        # to devices over the old, possibly compromised band.
        for client in list(self._clients):
            if client.transport.band_id not in wanted:
                await self.remove_band(client.transport.band_id.hex()[:8])
        for psk in psks:
            await self.add_band(psk)

    async def start(self) -> None:
        for c in self._clients:
            await c.start()
            # Short band-id fingerprint, for tagging the merged roster.
            c.label = c.transport.band_id.hex()[:8]
        log.info("multi-band client up: %d band(s) [%s] on hub %s:%d",
                 len(self._clients),
                 ", ".join(getattr(c, "label", "?") for c in self._clients),
                 self.hub_host, self.hub_port)

    async def stop(self) -> None:
        for c in self._clients:
            try:
                await c.stop()
            except Exception:
                log.exception("band client stop failed")

    @property
    def workers(self) -> dict[str, WorkerEntry]:
        """Union of every band's roster. If a worker is briefly visible on two
        bands (mid-migration), the freshest sighting wins."""
        merged: dict[str, WorkerEntry] = {}
        for c in self._clients:
            label = getattr(c, "label", "?")
            for wid, w in c.workers.items():
                prev = merged.get(wid)
                if prev is None or w.get("last_seen", 0.0) >= prev.get("last_seen", 0.0):
                    entry = WorkerEntry(w)
                    # A hub node (verified is_hub grant) announces on every
                    # band it serves; it belongs to none of them in particular.
                    entry["band"] = "*" if "is_hub" in (w.get("roles") or ()) else label
                    merged[wid] = entry
        if self._local is not None:
            # The hub node is on every band at once (and listed with none).
            entry = WorkerEntry(self._local.entry())
            entry["band"] = "*"
            merged[self._local.worker_id] = entry
        return merged

    def _client_for(self, worker_id: str) -> "BandClient | None":
        """The band where ``worker_id`` was most recently seen."""
        best: BandClient | None = None
        best_seen = -1.0
        for c in self._clients:
            w = c.workers.get(worker_id)
            if w and w.get("last_seen", 0.0) > best_seen:
                best, best_seen = c, w.get("last_seen", 0.0)
        return best

    async def call(self, cap: str, args: dict | None = None,
                   target: str | None = None, timeout: float = 15.0,
                   identity: str | None = None, principal=None, _decision=None) -> dict:
        local = self._local is not None and target == self._local.worker_id
        entry = self._local.entry() if local else self.workers.get(target) if target else None
        decision = _decision
        if decision is None:
            decision = _authorize(self._authz, cap, target, entry, identity, principal,
                                  local=local, args=args)
        if decision is not None and decision.denied:
            return _denial(decision, target)
        decision, refused = await _settle(self._authz, decision, args, target)
        if refused is not None:
            return refused
        if local:
            return await _call_local(self._local, cap, args, identity, timeout)
        kw = {"timeout": timeout, "identity": identity, "_decision": decision}
        # Known target → send only on its band.
        if not self._clients:
            raise ConnectionError("no active bands")
        if target:
            c = self._client_for(target)
            if c is not None:
                return await c.call(cap=cap, args=args, target=target, **kw)
        # Otherwise race across all bands; first real reply wins.
        if len(self._clients) == 1:
            return await self._clients[0].call(cap=cap, args=args, target=target, **kw)
        tasks = [asyncio.create_task(c.call(cap=cap, args=args, target=target, **kw))
                 for c in self._clients]
        try:
            result: dict | None = None
            err: Exception | None = None
            pending = set(tasks)
            while pending and result is None:
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break  # overall timeout
                for t in done:
                    try:
                        result = t.result()
                        break
                    except Exception as e:
                        err = e
            if result is not None:
                return result
            if err is not None:
                raise err
            raise asyncio.TimeoutError
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
