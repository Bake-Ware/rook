"""Worker core — wires a transport, plugin loader, and dispatcher together."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from pathlib import Path

from ..core import authz
from ..core.facts import NodeFacts, local_facts, wire_facts
from ..core.host import PluginHost
from . import audit, context
from .authz_guard import Guard
from .plugin import Plugin
from .registry import CapabilityRegistry
from .transports.base import Transport

log = logging.getLogger("rook.worker.core")

_WORKER_ID_FILE = Path(os.path.expanduser("~")) / ".rook-band-worker" / "worker_id"


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
                 plugins_pkg: str = "rook.worker.plugins",
                 enabled: list[str] | None = None,
                 name: str | None = None,
                 announce_interval: float = 30.0) -> None:
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
        self.facts = NodeFacts(node_id=self.worker_id, name=self.name, hw=local_facts())
        self.host = PluginHost(self.registry, self.facts, build_version=VERSION,
                               data_root=str(_WORKER_ID_FILE.parent / "plugins"))
        # Persisted runtime disables (via worker.plugin.disable) are honoured at
        # boot so a disabled plugin is never loaded in the first place.
        from .admin import load_disabled, WorkerAdmin
        self.host.load(self.host.discover(plugins_pkg), enabled, disabled=load_disabled())
        self.plugins: list[Plugin] = self.host.plugins  # same list; admin mutates it
        # Introspection: lets the dashboard build accurate call forms.
        self.registry.register("caps.describe", self._caps_describe)
        # Runtime cap administration: worker.plugin.* + customcap.* + re-hydrate
        # persisted custom command-caps (cmd.*).
        self.admin = WorkerAdmin(self.registry, self.plugins, plugins_pkg)
        self.admin.register_caps()
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
        # Let plugins that need to send/receive raw band traffic (not just
        # answer capability calls) grab a handle to us. Opt-in via bind_worker.
        for p in self.plugins:
            bind = getattr(p, "bind_worker", None)
            if callable(bind):
                try:
                    bind(self)
                except Exception:
                    log.exception("plugin %s bind_worker failed", p.NAMESPACE)

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

    def register_binary_handler(self, handler) -> None:
        """Register a callable(payload: bytes, peer_id: tuple) -> bool that gets
        first crack at every inbound payload; returning True consumes it."""
        self._binary_handlers.append(handler)

    async def send_raw(self, packet_bytes: bytes) -> None:
        """Send an already-framed binary packet onto the band (used by binary
        sub-protocols like OTA-over-Drop)."""
        await self.transport.send(packet_bytes)

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

        cap = msg.get("cap")
        if not cap:
            # Hub announces carry the is_hub grant behind its call tickets;
            # learn the op key from them (signed announce = key possession).
            if msg.get("kind") == "announce" and "grants" in msg:
                try:
                    self.guard.on_announce(msg)
                except Exception:
                    log.debug("hub grant check failed", exc_info=True)
            return  # not a request

        target = msg.get("target")
        if target and target != self.worker_id:
            return  # addressed to another worker

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
        except TypeError as e:
            audit.record(cap, identity, args, ok=False, msg_id=msg_id,
                         target=target, error=f"bad args: {e}", ticket=ticket,
                         decision="allow")
            await self._reply(msg_id, {"ok": False, "error": f"bad args: {e}"})
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

    async def _reply(self, msg_id: str | None, body: dict) -> None:
        body = {"from": self.worker_id, **body}
        if msg_id is not None:
            body = {"id": msg_id, **body}
        try:
            await self.transport.send(json.dumps(body).encode())
        except Exception:
            log.exception("reply send failed")

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

    async def announce(self) -> None:
        from ._build_info import BUILD, VERSION
        msg = {
            "kind": "announce",
            "worker_id": self.worker_id,
            "name": self.name,
            "description": self.metadata.description,
            "caps": self.registry.list(),
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
        await self.transport.start(self._on_message, on_connect=self.announce)
        await self.host.start()
        try:
            await self.announce()
        except Exception:
            # A WS transport may still be connecting; on_connect and the
            # announce loop will register us as soon as the link is up.
            log.debug("initial announce deferred (transport not ready yet)")
        self._announce_task = asyncio.create_task(self._announce_loop())
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
        if self._announce_task is not None:
            self._announce_task.cancel()
            try:
                await self._announce_task
            except Exception:
                pass
        await self.host.stop()
        try:
            await self.transport.stop()
        except Exception:
            log.exception("transport stop failed")
