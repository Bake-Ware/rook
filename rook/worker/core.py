"""Worker core — wires a transport, plugin loader, and dispatcher together."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from ..core import authz
from ..core.facts import NodeFacts, clean_facts, local_facts, wire_facts
from ..core.host import PluginHost
from ..core.paths import worker_state_path
from . import audit, context
from .authz_guard import Guard
from .plugin import Plugin
from .registry import CapabilityRegistry
from ..core.pluginkit import CapError
from ..core.registry import CapTimeout
from .transports.base import Transport
from .wire import envelope

log = logging.getLogger("rook.worker.core")

_WORKER_ID_FILE = worker_state_path("worker_id")

# With ROOK_ANNOUNCE_CAPS_HASH=1, every this-many-th announce still carries the
# full cap list (~5 minutes at the default 30 s interval), so a client that
# missed a change or joined late converges even without fetching.
ANNOUNCE_FULL_EVERY = 10

# One id per process: a re-exec gets a new one, a plugin reload (which
# re-imports plugin modules, not this one) keeps it. The OTA health gate uses
# it to tell a real boot from a reload of the selfupdate plugin.
BOOT_ID = uuid.uuid4().hex


def stable_worker_id() -> str:
    """A worker_id that survives restarts, so a node keeps ONE identity on the
    band. Two reasons this matters: (1) each restart used to mint a fresh uuid,
    leaving ghost duplicate rows in the dashboard until the old one aged out;
    (2) durable bans (worker.deauth) need to name a target that a restart can't
    shed. Persisted under the worker state dir; falls back to an ephemeral id if
    that dir isn't writable (better a transient id than a crash)."""
    try:
        wid = _WORKER_ID_FILE.read_text(encoding="utf-8").strip()
        if wid:
            return wid
    except Exception:
        pass
    wid = uuid.uuid4().hex
    try:
        _WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WORKER_ID_FILE.write_text(wid + "\n", encoding="utf-8")
    except Exception:
        log.warning("could not persist worker_id; using an ephemeral one")
    return wid


class Worker:
    def __init__(self, transport: Transport,
                 plugins_pkg: "str | list[str]" = "rook.worker.plugins",
                 enabled: list[str] | None = None,
                 name: str | None = None,
                 announce_interval: float = 30.0,
                 facts: dict | None = None) -> None:
        self.app_release: dict = {}
        self.transport = transport
        self.registry = CapabilityRegistry()
        self.plugins_pkg = plugins_pkg
        self.worker_id = stable_worker_id()
        self.name = name or socket.gethostname()
        # One plugin host for hub and workers (rook.core.host): placement over
        # this node's facts, manifest checks, failure isolation. Workers hold no
        # roles, so plugins placed on the hub (is_hub) never load here.
        from ._build_info import VERSION
        # ``facts``: extra self-reported facts from the embedding app (the
        # Android APK adds app="android" so its native plugins place there).
        hw = {**local_facts(), **clean_facts(facts or {})}
        self.facts = NodeFacts(node_id=self.worker_id, name=self.name, hw=hw)
        self.host = PluginHost(self.registry, self.facts, build_version=VERSION,
                               data_root=str(_WORKER_ID_FILE.parent / "plugins"))
        # Persisted runtime disables (via worker.plugin.disable) are honoured at
        # boot so a disabled plugin is never loaded in the first place.
        from .admin import apply_enrollment_profile, load_disabled, load_enabled, WorkerAdmin
        # First start after enrollment: the plugin profile the pairing code
        # carried (the band's choice of plugins) becomes this worker's
        # persisted state before anything loads.
        try:
            apply_enrollment_profile()
        except Exception:
            log.exception("applying the enrollment plugin profile failed; loading the defaults")
        # Plugins enabled at runtime (worker.plugin.enable) survive a restart
        # even when --enable leaves them out (settings P14).
        self.enable_arg = list(enabled) if enabled is not None else None
        if enabled is not None:
            enabled = list(dict.fromkeys(list(enabled) + sorted(load_enabled())))
        self.host.load(self.host.discover(plugins_pkg), enabled, disabled=load_disabled())
        self.plugins: list[Plugin] = self.host.plugins  # same list; the host mutates it
        # Introspection: lets the dashboard build accurate call forms.
        self.registry.register("caps.describe", self._caps_describe)
        # Runtime cap administration: worker.plugin.* + customcap.* + re-hydrate
        # persisted custom command-caps (cmd.*).
        self.admin = WorkerAdmin(self.registry, self.plugins, plugins_pkg,
                                 enable_arg=self.enable_arg, host=self.host)
        self.admin.register_caps()
        # Typed settings: what this worker's plugins declare, which env the
        # pushed config set, and secrets fetched from the hub at use. Its
        # presence tells the hub this build resolves {{secret:…}} references.
        self.registry.register("worker.settings_report", self._settings_report)
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._secret_task: asyncio.Task | None = None
        from .metadata import WorkerMetadata
        self.metadata = WorkerMetadata(_WORKER_ID_FILE.with_name('metadata.json'))
        self.registry.register('worker.description_get', self._description_get)
        self.registry.register('worker.description_set', self._description_set)
        self._announce_interval = announce_interval
        band_id = getattr(transport, "band_id", None)
        self.guard = Guard(self.worker_id,
                           band_id.hex() if isinstance(band_id, (bytes, bytearray)) else None)
        self._stopping = False
        self._announce_task: asyncio.Task | None = None
        # Binary sub-protocol handlers (e.g. in-band OTA over telesthete Drop).
        # The band carries JSON capability messages by default, but some
        # features ride binary telesthete packets on other channel types; a
        # plugin registers a handler that claims those before JSON parsing.
        # Each returns True if it consumed the payload.
        self._binary_handlers: list = []
        # Wire-level extensions (compression, Drop) need a transport that
        # frames for the band (its codec inflates and re-wraps on the far
        # side); in-process transports carry plain JSON and never get them.
        self._wire = getattr(transport, "codec", None) is not None
        # Large replies served over telesthete Drop (requests that accept
        # "drop"); needs a real band id to frame Drop packets.
        self._reply_drops = None
        if self._wire and isinstance(band_id, (bytes, bytearray)) and len(band_id) == 16:
            from .ota_drop import DropReplyServer
            self._reply_drops = DropReplyServer(bytes(band_id), self.send_raw,
                                                worker_id=self.worker_id)
        # A request the transport's dispatcher sheds under a burst gets a
        # "busy" error reply when it was aimed at us, instead of a silent
        # caller timeout.
        if hasattr(transport, "on_shed"):
            transport.on_shed = self._on_shed
        self._busy_tasks: set[asyncio.Task] = set()
        # Addressed routing (telesthete SPEC §10.1): declare our worker_id's
        # route id so replies and targeted calls can reach us alone.
        set_route = getattr(transport, "set_route", None)
        if callable(set_route) and envelope.routing_enabled():
            set_route(self.worker_id)
        # Band-contact evidence for the post-update health gate (selfupdate):
        # monotonic times of the last authenticated message from another peer
        # and of the last reply to one of our own requests (a full round trip:
        # our sealed send was opened, answered and routed back), plus who
        # announced lately, as targets for a probe. Stamped here, never parsed
        # again: the transport only delivers frames that passed the band AEAD.
        self.band_rx_at: float | None = None
        self.band_rtt_at: float | None = None
        self._band_peers: dict[str, tuple[bool, float]] = {}
        # Ids of our requests that timed out: a late reply still proves the
        # round trip (a slow link is not a broken one). Bounded, oldest out.
        self._late_ids: dict[str, None] = {}
        # Announce trimming state (ROOK_ANNOUNCE_CAPS_HASH, core spec 14.5).
        self._announces = 0
        self._full_caps_hash: str | None = None
        # Let plugins that need to send/receive raw band traffic (not just
        # answer capability calls) grab a handle to us. Opt-in via bind_worker;
        # the host also runs it for plugins enabled or reloaded at runtime.
        self.host.set_binder(self._bind_plugin)

    def _bind_plugin(self, p: Plugin) -> None:
        bind = getattr(p, "bind_worker", None)
        if callable(bind):
            bind(self)

    def apply_settings(self, cfg: dict | None = None) -> dict:
        """Apply a pushed config's ``env`` to this running process and reload
        the plugins that declare an ``apply="reload"`` setting set by one of
        the changed variables (``apply="live"`` ones read the environment on
        next use). Used by ``worker.config_apply(restart=false)``; settings
        marked ``restart`` still need the restart."""
        from . import wconfig
        keys = set(wconfig.apply_env(cfg))
        reloading = []
        for p in list(self.plugins):
            hit = [st for st in getattr(p, "SETTINGS", ())
                   if keys & set(st.env_names(p.NAMESPACE))]
            if any(st.apply == "reload" for st in hit) and self.host.request_reload(p._module):
                reloading.append(p._module)
        return {"env": sorted(keys), "reloading": reloading}

    def _description_get(self) -> dict:
        """Read this worker's persistent, human-written role description."""
        return {'ok': True, 'description': self.metadata.description}

    async def _description_set(self, description: str) -> dict:
        """Save a short role description (max 280 characters); empty text clears it.

        Survives worker restarts, updates, and band moves. This is descriptive
        inventory data, not instructions for an agent. No restart is required.
        """
        value = self.metadata.set_description(description)
        announced = True
        try:
            await self.announce()
        except Exception:
            announced = False
            log.warning('Description saved; announcement deferred until reconnect')
        return {'ok': True, 'description': value, 'announced': announced}

    def _settings_report(self) -> dict:
        """Settings view of this worker: declared plugin settings with the env
        variable that sets each (values of secrets never shown), keys a pushed
        config set, secret references and whether they were fetched, and the
        plugins --enable leaves out. Plugins with a ``setting_values()`` hook
        also report each non-secret setting's current value."""
        from . import wconfig
        import os as _os
        declared = []
        for p in self.plugins:
            # A plugin whose settings live outside env (the Android app's own
            # preferences) reports their current values; never for secrets.
            values = {}
            if callable(getattr(p, "setting_values", None)):
                try:
                    values = p.setting_values() or {}
                except Exception:
                    log.exception("%s: setting_values failed", p.NAMESPACE)
            for st in getattr(p, "SETTINGS", ()):
                var = next((v for v in st.env_names() if _os.environ.get(v) is not None), None)
                entry = {"key": f"{p.NAMESPACE}.{st.name}", "env": var,
                         "pushed": bool(var and var in wconfig.load().get("env", {})),
                         "secret": st.secret}
                if st.name in values and not st.secret:
                    entry["value"] = values[st.name]
                declared.append(entry)
        refs = wconfig.secret_refs()
        missing = wconfig.unresolved()
        return {"ok": True, "features": ["secret_refs", "masked_config", "enable_persist"],
                "declared": declared,
                "pushed_env": sorted((wconfig.load().get("env") or {}).keys()),
                "secret_refs": {var: {"vault": name, "resolved": var not in missing}
                                for var, name in refs.items()},
                "enable": self.enable_arg,
                "excluded": self.admin.excluded()}

    async def request(self, cap: str, args: dict | None = None, target: str | None = None,
                      timeout: float = 10.0):
        """A band call that waits for its reply (``{"ok", "result"|"error"}``)."""
        msg_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending_replies[msg_id] = fut
        msg: dict = {"id": msg_id, "cap": cap, "args": args or {},
                     "identity": f"worker:{self.name}"}
        if target:
            msg["target"] = target
        rid = getattr(self.transport, "route_id", None)
        if rid:
            msg["rr"] = rid  # reply addressed to us (old repliers ignore it)
        try:
            await self.transport.send(json.dumps(msg).encode())
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending_replies.pop(msg_id, None)
            if not fut.done() or fut.cancelled():
                self._late_ids[msg_id] = None
                while len(self._late_ids) > self.BAND_PEERS_MAX:
                    self._late_ids.pop(next(iter(self._late_ids)))

    async def resolve_secret_refs(self) -> list[str]:
        """Fetch pushed ``{{secret:…}}`` values from the hub (worker ``rook``,
        cap ``settings.worker_secret``) into this process's environment. The
        values stay in memory; nothing is written to disk."""
        from . import wconfig
        missing = wconfig.unresolved()
        if not missing:
            return []
        try:
            reply = await self.request("settings.worker_secret",
                                       {"worker_id": self.worker_id,
                                        "names": sorted(set(missing.values()))}, timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("hub did not answer settings.worker_secret; %d secret(s) unresolved",
                        len(missing))
            return []
        if not reply.get("ok"):
            log.warning("fetching pushed secrets failed: %s", reply.get("error"))
            return []
        got = (reply.get("result") or {}).get("secrets") or {}
        done = wconfig.set_resolved({var: got.get(name) for var, name in missing.items()
                                     if got.get(name) is not None})
        still = wconfig.unresolved()
        if still:
            log.warning("pushed secret(s) for %s not available from the hub", sorted(still))
        return done

    async def _on_connect(self) -> None:
        await self.announce(full=True)
        from . import wconfig
        if wconfig.unresolved() and (self._secret_task is None or self._secret_task.done()):
            self._secret_task = asyncio.ensure_future(self._resolve_later())

    async def _resolve_later(self) -> None:
        for delay in (2.0, 10.0, 30.0, 60.0):
            await asyncio.sleep(delay)
            try:
                await self.resolve_secret_refs()
            except Exception:
                log.exception("resolving pushed secrets failed")
            from . import wconfig
            if not wconfig.unresolved():
                return

    def register_binary_handler(self, handler) -> None:
        """Register a callable(payload: bytes, peer_id: tuple) -> bool that gets
        first crack at every inbound payload; returning True consumes it."""
        if handler not in self._binary_handlers:
            self._binary_handlers.append(handler)

    def unregister_binary_handler(self, handler) -> None:
        """Drop a handler added by :meth:`register_binary_handler` (a plugin
        that is stopped or reloaded must not keep receiving packets)."""
        try:
            self._binary_handlers.remove(handler)
        except ValueError:
            pass

    async def send_raw(self, packet_bytes: bytes, route: int = 0) -> None:
        """Send an already-framed binary packet onto the band (used by binary
        sub-protocols like OTA-over-Drop). ``route`` addresses it to a peer's
        declared route id (0 = broadcast)."""
        await self._send(packet_bytes, route)

    async def _send(self, payload: bytes, route: int = 0) -> None:
        # Address only through a transport that routes (it declared a route
        # id): others — older transports, embedders' and tests' doubles —
        # take send(payload) alone, and get the broadcast they always did.
        if route and getattr(self.transport, "route_id", None) is not None:
            await self.transport.send(payload, route=route)
        else:
            await self.transport.send(payload)

    # At most this many "busy" replies in flight; beyond it shed requests
    # time out at their caller as before (the link is saturated anyway).
    BUSY_REPLIES_MAX = 64

    def _on_shed(self, payload: bytes, peer_id: tuple) -> None:
        """The dispatcher shed a request (its backlog was full). Answer it
        ``busy`` if it was a targeted call to us, so the caller learns now
        rather than at its timeout. Open calls stay silent: another worker
        may answer them."""
        if len(self._busy_tasks) >= self.BUSY_REPLIES_MAX:
            return
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("target") != self.worker_id:
            return
        msg_id = msg.get("id")
        if msg_id is None:
            return
        body = {"id": msg_id, "from": self.worker_id, "ok": False,
                "error": "busy: worker dispatch backlog full; retry later"}
        route = envelope.reply_opts(msg).route
        task = asyncio.get_running_loop().create_task(
            self._send_quietly(json.dumps(body).encode(), route))
        self._busy_tasks.add(task)
        task.add_done_callback(self._busy_tasks.discard)

    async def _send_quietly(self, payload: bytes, route: int) -> None:
        try:
            await self._send(payload, route)
        except Exception as e:
            log.debug("busy reply not sent: %s", e)

    def features(self) -> list[str]:
        """Envelope extensions this worker speaks (announce ``features``,
        core spec 14). Callers use one only toward a worker that lists it."""
        t = self.transport
        out = []
        if getattr(t, "route_id", None) is not None:
            out.append(envelope.F_ROUTE)
        if self._reply_drops is not None:
            out.append(envelope.F_DROP)
        if self._wire:
            out.append(envelope.F_Z)            # its codec inflates requests
        if callable(getattr(t, "send_native", None)):
            out.append(envelope.F_DROP_NATIVE)  # its codec opens native DROP
        return sorted(out)

    async def _on_message(self, payload: bytes, peer_id: tuple) -> None:
        """Top-level dispatch. Capability requests look like:

            {"id": "...", "cap": "shell.exec", "args": {...}, "target"?: "<worker_id>"}

        ``target`` is optional. If present and not equal to ``self.worker_id``
        this worker ignores the request. ``target`` absent = open call.

        Replies:
            {"id": "...", "from": worker_id, "ok": true,  "result": ...}
            {"id": "...", "from": worker_id, "ok": false, "error": "..."}

        Anything without a ``cap`` field (announces, replies, foreign chatter)
        is dropped silently — we are not a sink.
        """
        # Binary sub-protocols (OTA-over-Drop, …) get first crack — they ride
        # non-JSON telesthete packets that json.loads would just reject.
        # Pulls of our own large replies (Drop REQUEST/DONE) come first.
        if self._reply_drops is not None and self._reply_drops.feed(payload):
            return
        for handler in self._binary_handlers:
            try:
                if handler(payload, peer_id):
                    return
            except Exception:
                log.exception("binary handler raised")

        try:
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        self._note_band_rx(msg)
        cap = msg.get("cap")
        if not cap:
            # Hub announces carry the is_hub grant behind its call tickets;
            # learn the op key from them (signed announce = key possession).
            if msg.get("kind") == "announce" and "grants" in msg:
                try:
                    self.guard.on_announce(msg)
                except Exception:
                    log.debug("hub grant check failed", exc_info=True)
            # A reply to one of our own requests (Worker.request)?
            mid = msg.get("id")
            mid = mid if isinstance(mid, str) else None
            fut = self._pending_replies.get(mid) if mid else None
            if fut is not None and not fut.done():
                self.band_rtt_at = time.monotonic()
                fut.set_result(msg)
            elif mid and "ok" in msg and mid in self._late_ids:
                del self._late_ids[mid]
                self.band_rtt_at = time.monotonic()   # late, but a round trip
            return  # not a request

        target = msg.get("target")
        if target and target != self.worker_id:
            return  # addressed to another worker
        # How the caller wants its reply (rr / accept, core spec 14); read by
        # _reply and by caps that answer over a side channel. Set for every
        # request so nothing carries over from a previous one.
        envelope.current_reply_opts.set(envelope.reply_opts(msg))

        msg_id = msg.get("id")
        # Caller identity carried in the envelope (band client stamps it from
        # the bearer-token name; see rook.band_mcp). Audit-first: we log who
        # called what regardless of whether any cap gates on it yet.
        identity = msg.get("identity")

        # If we don't own the cap and the request wasn't aimed at us, stay
        # silent so the band doesn't get spammed with one error per worker.
        if not self.registry.has(cap):
            if target == self.worker_id:
                audit.record(cap, identity, None, ok=False, msg_id=msg_id,
                             target=target, error="unknown capability")
                await self._reply(msg_id, {"ok": False,
                                            "error": f"unknown capability: {cap}"})
            return

        args = msg.get("args", {}) or {}
        if not isinstance(args, dict):
            audit.record(cap, identity, None, ok=False, msg_id=msg_id,
                         target=target, error="args must be an object")
            await self._reply(msg_id, {"ok": False, "error": "args must be an object"})
            return

        # Defense in depth (permissions 3.5 E2): check the hub's call ticket
        # against this worker's own tier table. In the default ``audit`` mode
        # nothing is refused; the result is recorded in audit.jsonl.
        try:
            tier = authz.effective_tier(cap, getattr(self.registry.meta(cap), "risk", None))
            ticket = self.guard.check(msg, cap, tier, args)
        except Exception as e:  # a broken checker must not brick the worker
            log.exception("ticket check failed")
            ticket = {"verified": False, "reason": f"check failed: {type(e).__name__}"}
            if self.guard.mode not in ("off", "audit"):
                ticket["refuse"] = "denied by worker: ticket check failed"
        if ticket.get("refuse"):
            audit.record(cap, identity, args, ok=False, msg_id=msg_id, target=target,
                         error=ticket["refuse"], ticket=ticket, decision="deny")
            await self._reply(msg_id, {"ok": False, "error": ticket["refuse"]})
            return

        # Expose the caller identity to the handler for its duration (memory
        # write-ownership, chat attribution, later ACLs). Reset after so it
        # never leaks into an unrelated dispatch.
        tok = context.caller_identity.set(identity)
        ttok = context.call_ticket.set(ticket)
        try:
            result = await self.registry.call(cap, **args)
            audit.record(cap, identity, args, ok=True, msg_id=msg_id, target=target,
                         ticket=ticket, decision="allow")
            await self._reply(msg_id, {"ok": True, "result": result})
        except CapTimeout as e:
            audit.record(cap, identity, args, ok=False, msg_id=msg_id,
                         target=target, error=f"timeout: {e}", ticket=ticket,
                         decision="allow")
            await self._reply(msg_id, {"ok": False, "error": f"timeout: {e}"})
        except TypeError as e:
            audit.record(cap, identity, args, ok=False, msg_id=msg_id,
                         target=target, error=f"bad args: {e}", ticket=ticket,
                         decision="allow")
            await self._reply(msg_id, {"ok": False, "error": f"bad args: {e}"})
        except CapError as e:
            # A cap-authored failure (pluginkit.fail and friends): the message
            # is the error as-is, no type-name prefix and no traceback.
            audit.record(cap, identity, args, ok=False, msg_id=msg_id,
                         target=target, error=str(e), ticket=ticket,
                         decision="allow")
            await self._reply(msg_id, {"ok": False, "error": str(e)})
        except Exception as e:
            log.exception("capability %s raised", cap)
            audit.record(cap, identity, args, ok=False, msg_id=msg_id,
                         target=target, error=f"{type(e).__name__}: {e}",
                         ticket=ticket, decision="allow")
            await self._reply(msg_id, {"ok": False,
                                        "error": f"{type(e).__name__}: {e}"})
        finally:
            context.call_ticket.reset(ttok)
            context.caller_identity.reset(tok)

    # Most recent announcers remembered as probe targets (bounded: a big band
    # must not grow this without limit).
    BAND_PEERS_MAX = 32

    def _note_band_rx(self, msg: dict) -> None:
        """Record band contact for an inbound message from another peer (our
        own traffic never counts, should a relay ever echo it)."""
        if msg.get("from") == self.worker_id or msg.get("worker_id") == self.worker_id:
            return
        now = time.monotonic()
        self.band_rx_at = now
        wid = msg.get("worker_id")
        if msg.get("kind") == "announce" and isinstance(wid, str) and wid:
            peers = self._band_peers
            peers.pop(wid, None)
            # "grants" marks the hub node (unverified here; any peer that
            # answers a probe proves the round trip just as well).
            peers[wid] = ("grants" in msg, now)
            while len(peers) > self.BAND_PEERS_MAX:
                peers.pop(next(iter(peers)))

    def band_peers(self) -> list[str]:
        """Recently announced peers, the hub node first, then newest first:
        targets for a band round-trip probe."""
        ranked = sorted(self._band_peers.items(), key=lambda kv: (not kv[1][0], -kv[1][1]))
        return [wid for wid, _ in ranked]

    async def _reply(self, msg_id: str | None, body: dict) -> None:
        body = {"from": self.worker_id, **body}
        if msg_id is not None:
            body = {"id": msg_id, **body}
        opts = envelope.current_reply_opts.get()
        try:
            payload = self._encode_reply(msg_id, body, opts)
            await self._send(payload, opts.route)
        except Exception:
            log.exception("reply send failed")

    def _encode_reply(self, msg_id: str | None, body: dict,
                      opts: "envelope.ReplyOpts") -> bytes:
        """Serialize a reply, applying only what the request negotiated: the
        size cap (always), Drop for a large reply (``accept: drop``), and
        compression (``accept: z``). A request without ``accept`` gets the
        plain JSON reply it always got."""
        payload = json.dumps(body).encode()
        limit = envelope.max_reply_bytes()
        if len(payload) > limit:
            log.warning("reply to %s is %d bytes, over the %d-byte cap; sending an error",
                        msg_id, len(payload), limit)
            err = {k: body[k] for k in ("id", "from") if k in body}
            err.update(ok=False, error=(f"reply too large: {len(payload)} bytes exceeds "
                                        f"ROOK_BAND_MAX_REPLY_BYTES ({limit})"))
            payload = json.dumps(err).encode()
        else:
            threshold = envelope.drop_threshold()
            if (opts.drop and msg_id is not None and self._reply_drops is not None
                    and threshold and len(payload) > threshold):
                blob = envelope.compress(payload) if opts.z else payload  # _wire implied
                # Routed pass-through when the caller has a route (only it gets
                # the chunks); native DROP frames when it can't be addressed.
                native = (getattr(self.transport, "send_native", None)
                          if opts.drop_native and not opts.route else None)
                desc = self._reply_drops.serve(msg_id, blob, route=opts.route, native=native)
                if desc is not None:
                    payload = json.dumps({"id": msg_id, "from": self.worker_id,
                                          "ok": body.get("ok"), "drop": desc}).encode()
        return envelope.compress(payload) if opts.z and self._wire else payload

    async def call(self, cap: str, target: str | None = None, **kwargs) -> str:
        """Send an outbound capability call to the band."""
        msg_id = uuid.uuid4().hex
        msg: dict = {"id": msg_id, "cap": cap, "args": kwargs}
        if target:
            msg["target"] = target
        await self.transport.send(json.dumps(msg).encode())
        return msg_id

    def _caps_describe(self, prefix: str = "") -> dict:
        """Arg schema + docstring for every capability on this worker (for the UI).

        ``prefix`` (e.g. ``"shell."``) limits it to matching caps. Workers
        before this arg reject it, so the hub filters on its side instead of
        sending it."""
        return self.registry.describe(prefix)

    async def announce(self, full: bool = False) -> None:
        from ._build_info import BUILD, VERSION
        caps = self.registry.list()
        chash = envelope.caps_hash(caps)
        msg = {
            "kind": "announce",
            "worker_id": self.worker_id,
            "name": self.name,
            "description": self.metadata.description,
            "caps": caps,
            # Additive keys (core spec 14): older receivers ignore them.
            "caps_hash": chash,
            "features": self.features(),
            "plugins": [p.NAMESPACE for p in self.plugins],
            "version": VERSION,
            "build": BUILD,
            "app_release": self.app_release,
            # Self-reported platform/hardware facts for plugin placement.
            # Additive: build-167 receivers ignore unknown announce keys.
            "facts": wire_facts(self.facts.hw),
        }
        tiers = self.host.tiers()  # declared risk tiers only; omitted when none
        if tiers:
            msg["tiers"] = tiers
        # Ticket-verification readiness (permissions 5.1); additive key.
        msg["authz"] = self.guard.readiness()
        # Optional per-plugin live status (battery, etc.) rides the heartbeat.
        hb = self.host.heartbeats()
        if hb:
            msg["hb"] = hb
        # Hash-only announces (opt-in): full caps on the first announce, on a
        # change, after a reconnect and every ANNOUNCE_FULL_EVERY-th one.
        if (envelope.caps_hash_announces() and not full
                and chash == self._full_caps_hash
                and self._announces % ANNOUNCE_FULL_EVERY != 0):
            del msg["caps"]
        else:
            self._full_caps_hash = chash
        self._announces += 1
        await self.transport.send(json.dumps(msg).encode())

    async def _announce_loop(self) -> None:
        import random
        while not self._stopping:
            try:
                # Jitter each interval (±20%) so a fleet that (re)started together
                # de-phases instead of announcing in a synchronized burst.
                await asyncio.sleep(self._announce_interval * (0.8 + random.random() * 0.4))
                if not self._stopping:
                    await self.announce()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("announce failed")

    async def run(self) -> None:
        # on_connect=self.announce: re-announce every time the transport's link
        # (re)establishes, so a dropped+restored band connection re-registers
        # us instead of leaving us silently off the band.
        await self.transport.start(self._on_message, on_connect=self._on_connect)
        await self.host.start()
        try:
            await self.announce()
        except Exception:
            # A WS transport may still be connecting; on_connect and the
            # announce loop will register us as soon as the link is up.
            log.debug("initial announce deferred (transport not ready yet)")
        self._announce_task = asyncio.create_task(self._announce_loop())
        from . import wconfig
        if wconfig.unresolved() and self._secret_task is None:
            self._secret_task = asyncio.ensure_future(self._resolve_later())
        log.info("worker up: id=%s name=%s caps=%s",
                 self.worker_id, self.name, self.registry.list())
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._secret_task is not None and not self._secret_task.done():
            self._secret_task.cancel()
        if self._announce_task is not None:
            self._announce_task.cancel()
            try:
                await self._announce_task
            except Exception:
                pass
        await self.host.stop()
        if self._reply_drops is not None:
            self._reply_drops.close()
        try:
            await self.transport.stop()
        except Exception:
            log.exception("transport stop failed")
