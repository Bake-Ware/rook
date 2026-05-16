"""Worker core — wires a transport, plugin loader, and dispatcher together."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from .plugin import Plugin, load_plugins
from .registry import CapabilityRegistry
from .transports.base import Transport

log = logging.getLogger("rook.worker.core")


class Worker:
    def __init__(self, transport: Transport,
                 plugins_pkg: str = "rook.worker.plugins",
                 enabled: list[str] | None = None) -> None:
        self.transport = transport
        self.registry = CapabilityRegistry()
        self.plugins: list[Plugin] = load_plugins(plugins_pkg, self.registry, enabled)
        self._stopping = False

    async def _on_message(self, payload: bytes, peer_id: tuple) -> None:
        """Top-level dispatch. Expects a JSON object with at minimum ``cap``.

        Message shape:
            {"id": "<uuid>", "cap": "shell.exec", "args": {"cmd": "ls"}}

        Reply shape:
            {"id": "<uuid>", "ok": true, "result": ...}
            {"id": "<uuid>", "ok": false, "error": "..."}
        """
        try:
            msg = json.loads(payload)
        except Exception as e:
            log.debug("bad json: %s", e)
            return
        if not isinstance(msg, dict):
            return

        # Plain announcements / responses — ignore (not capability calls).
        cap = msg.get("cap")
        msg_id = msg.get("id")
        if not cap:
            return

        if not self.registry.has(cap):
            await self._reply(msg_id, {"ok": False, "error": f"unknown capability: {cap}"})
            return

        args = msg.get("args", {}) or {}
        if not isinstance(args, dict):
            await self._reply(msg_id, {"ok": False, "error": "args must be an object"})
            return

        try:
            result = await self.registry.call(cap, **args)
            await self._reply(msg_id, {"ok": True, "result": result})
        except KeyError as e:
            await self._reply(msg_id, {"ok": False, "error": f"unknown capability: {e}"})
        except TypeError as e:
            await self._reply(msg_id, {"ok": False, "error": f"bad args: {e}"})
        except Exception as e:
            log.exception("capability %s raised", cap)
            await self._reply(msg_id, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    async def _reply(self, msg_id: str | None, body: dict) -> None:
        if msg_id is not None:
            body = {"id": msg_id, **body}
        try:
            await self.transport.send(json.dumps(body).encode())
        except Exception:
            log.exception("reply send failed")

    async def call(self, cap: str, **kwargs) -> str:
        """Send an outbound capability call to whoever is in the band.

        Returns the generated message id so the caller can correlate replies
        (handled by user code; the worker itself does not block).
        """
        msg_id = uuid.uuid4().hex
        msg = {"id": msg_id, "cap": cap, "args": kwargs}
        await self.transport.send(json.dumps(msg).encode())
        return msg_id

    async def announce(self) -> None:
        """One-shot non-request announcement so other peers can see us."""
        msg = {
            "kind": "announce",
            "caps": self.registry.list(),
            "plugins": [p.NAMESPACE for p in self.plugins],
        }
        await self.transport.send(json.dumps(msg).encode())

    async def run(self) -> None:
        await self.transport.start(self._on_message)
        for p in self.plugins:
            try:
                await p.start()
            except Exception:
                log.exception("plugin %s start failed", p.NAMESPACE)
        await self.announce()
        log.info("worker up: caps=%s", self.registry.list())
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for p in self.plugins:
            try:
                await p.stop()
            except Exception:
                log.exception("plugin %s stop failed", p.NAMESPACE)
        try:
            await self.transport.stop()
        except Exception:
            log.exception("transport stop failed")
