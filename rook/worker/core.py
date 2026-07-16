"""Worker core — wires a transport, plugin loader, and dispatcher together."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid

from .plugin import Plugin, load_plugins
from .registry import CapabilityRegistry
from .transports.base import Transport

log = logging.getLogger("rook.worker.core")


class Worker:
    def __init__(self, transport: Transport,
                 plugins_pkg: str = "rook.worker.plugins",
                 enabled: list[str] | None = None,
                 name: str | None = None,
                 announce_interval: float = 30.0) -> None:
        self.transport = transport
        self.registry = CapabilityRegistry()
        self.plugins: list[Plugin] = load_plugins(plugins_pkg, self.registry, enabled)
        # Introspection: lets the dashboard build accurate call forms.
        self.registry.register("caps.describe", self._caps_describe)
        self.worker_id = uuid.uuid4().hex
        self.name = name or socket.gethostname()
        self._announce_interval = announce_interval
        self._stopping = False
        self._announce_task: asyncio.Task | None = None

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
        try:
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        cap = msg.get("cap")
        if not cap:
            return  # not a request

        target = msg.get("target")
        if target and target != self.worker_id:
            return  # addressed to another worker

        msg_id = msg.get("id")

        # If we don't own the cap and the request wasn't aimed at us, stay
        # silent so the band doesn't get spammed with one error per worker.
        if not self.registry.has(cap):
            if target == self.worker_id:
                await self._reply(msg_id, {"ok": False,
                                            "error": f"unknown capability: {cap}"})
            return

        args = msg.get("args", {}) or {}
        if not isinstance(args, dict):
            await self._reply(msg_id, {"ok": False, "error": "args must be an object"})
            return

        try:
            result = await self.registry.call(cap, **args)
            await self._reply(msg_id, {"ok": True, "result": result})
        except TypeError as e:
            await self._reply(msg_id, {"ok": False, "error": f"bad args: {e}"})
        except Exception as e:
            log.exception("capability %s raised", cap)
            await self._reply(msg_id, {"ok": False,
                                        "error": f"{type(e).__name__}: {e}"})

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

    def _caps_describe(self) -> dict:
        """Arg schema + docstring for every capability on this worker (for the UI)."""
        return self.registry.describe()

    async def announce(self) -> None:
        from ._build_info import BUILD, VERSION
        msg = {
            "kind": "announce",
            "worker_id": self.worker_id,
            "name": self.name,
            "caps": self.registry.list(),
            "plugins": [p.NAMESPACE for p in self.plugins],
            "version": VERSION,
            "build": BUILD,
        }
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
        for p in self.plugins:
            try:
                await p.start()
            except Exception:
                log.exception("plugin %s start failed", p.NAMESPACE)
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
        for p in self.plugins:
            try:
                await p.stop()
            except Exception:
                log.exception("plugin %s stop failed", p.NAMESPACE)
        try:
            await self.transport.stop()
        except Exception:
            log.exception("transport stop failed")
