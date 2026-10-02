"""Band client: joins a Telesthete band via the hub, tracks workers + caps,
fires capability calls, awaits matching replies via futures.

This is the read-side companion to :mod:`rook.worker.core`. It speaks the
same JSON wire format and reuses the worker's hub transport for I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from ..core.facts import clean_facts
from ..worker.transports.telesthete_hub import TelestheteHubTransport

log = logging.getLogger("rook.band_mcp.client")

# Idle-worker eviction: drop a worker we haven't heard from in this many
# seconds. Workers re-announce every 30s by default, so 90s tolerates a
# couple missed broadcasts before they disappear from `rook_workers()`.
WORKER_STALE_SECS = 90.0
# The hub's own node (hub-placed plugins, served as worker "rook") announces on
# the same cadence as workers.
LOCAL_ANNOUNCE_SECS = 30.0


def _denial(decision, target: str | None) -> dict:
    """A policy denial in the shape of a band reply (permissions 3.7)."""
    return {"id": uuid.uuid4().hex, "from": target or "rook", **decision.denial()}


def _authorize(authz, cap: str, target: str | None, entry: dict | None,
               identity: str | None, principal, local: bool = False):
    """Run E1 (permissions 3.5). Returns the Decision, or None when this
    client has no authorizer (tests, tools that never enforce)."""
    if authz is None:
        return None
    return authz.check(cap, target, entry, identity=identity, principal=principal, local=local)


async def _call_local(node, cap: str, args: dict | None, identity: str | None,
                      timeout: float) -> dict:
    """In-process call to the hub node: same reply shape as a band reply."""
    mid = uuid.uuid4().hex
    async with asyncio.timeout(timeout):
        body = await node.dispatch(cap, args or {}, identity, source="local")
    return {"id": mid, "from": node.worker_id, **body}


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
                 hub_port: int = 7474, use_ws: bool = False) -> None:
        self.transport = TelestheteHubTransport(
            psk=psk, hub_host=hub_host, hub_port=hub_port, use_ws=use_ws,
        )
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
                                  local=True)
        if decision is not None and decision.denied:
            body = decision.denial()
        else:
            body = await node.dispatch(msg.get("cap"), msg.get("args", {}),
                                       msg.get("identity"), source="band")
        reply = {"from": node.worker_id, **body}
        if msg.get("id") is not None:
            reply = {"id": msg["id"], **reply}
        try:
            await self.transport.send(json.dumps(reply).encode())
        except Exception:
            log.exception("hub node reply send failed")

    async def start(self) -> None:
        await self.transport.start(self._on_message, on_connect=self._on_connect)
        self._started = True
        self._gc_task = asyncio.create_task(self._gc_loop())
        self._start_local_announcer()
        log.info("band-mcp client up (band_id=%s, hub=%s:%d)",
                 self.transport.band_id.hex()[:16],
                 self.transport._hub[0], self.transport._hub[1])

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._gc_task, self._local_task, *self._local_calls):
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
        # In-band OTA (telesthete Drop) REQUEST/DONE come back as binary DROP
        # packets; route them to the active push before JSON parsing.
        if self._ota_sender is not None:
            from ..worker.ota_drop import is_drop_packet
            if is_drop_packet(payload, self.transport.band_id):
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
            "caps": list(msg.get("caps", [])),
            "plugins": list(msg.get("plugins", [])),
            "hb": dict(msg.get("hb") or {}),
            "version": msg.get("version"),
            "build": msg.get("build"),
            "app_release": msg.get("app_release") if isinstance(msg.get("app_release"), dict) else {},
            # Self-reported platform/hardware facts (absent on build-167 workers).
            "facts": clean_facts(facts),
            "last_seen": time.time(),
        })
        if wid not in self.workers:
            log.info("worker joined: id=%s name=%s caps=%d",
                     wid, entry["name"], len(entry["caps"]))
        self.workers[wid] = entry

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
        Raises ``asyncio.TimeoutError`` on no reply.
        """
        local = self._local is not None and target == self._local.worker_id
        decision = _decision
        if decision is None:
            entry = self._local.entry() if local else self.workers.get(target) if target else None
            decision = _authorize(self.authz, cap, target, entry, identity, principal, local=local)
        if decision is not None and decision.denied:
            return _denial(decision, target)
        if local:
            return await _call_local(self._local, cap, args, identity, timeout)
        if len(self._pending) >= 512:
            raise RuntimeError("band call capacity reached")
        mid = uuid.uuid4().hex
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
        try:
            async with asyncio.timeout(timeout):
                await self.transport.send(json.dumps(msg).encode())
                return await fut
        finally:
            self._pending.pop(mid, None)
            if not fut.done():
                fut.cancel()

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
        sha256, …). Returns a status dict.
        """
        from ..worker.ota_drop import OtaDropSender

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

        sender = OtaDropSender(self.transport.band_id, drop_id, name, bundle,
                               self.transport.send)
        self._ota_sender = sender
        try:
            sender.offer()
            ok = await sender.wait(timeout=transfer_timeout)
            return {"ok": bool(ok), "stage": "transfer",
                    "sha256": sender.sha256, "chunks": sender.total_chunks,
                    "verified": bool(ok)}
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
        deduped: list[str] = []
        for p in psks:
            p = (p or "").strip()
            if p and p not in deduped:
                deduped.append(p)
        # Empty is valid after the last band is revoked; the controller stays
        # available for enrollment management without retaining the old key.
        self._clients = [
            BandClient(psk=p, hub_host=hub_host, hub_port=hub_port, use_ws=use_ws)
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
                                hub_port=self.hub_port, use_ws=self.use_ws)
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
                   identity: str | None = None, principal=None) -> dict:
        local = self._local is not None and target == self._local.worker_id
        entry = self._local.entry() if local else self.workers.get(target) if target else None
        decision = _authorize(self._authz, cap, target, entry, identity, principal, local=local)
        if decision is not None and decision.denied:
            return _denial(decision, target)
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
