"""The hub transport job steps use: the band roster, cap calls, hub tools,
vault substitution and the call journal.

Nothing new on the wire. Calls go through the hub's band client
(``node.client.call``), the same path ``rook_call`` and the chat integrations
use, so the authorizer evaluates them for the run's principal and a call to
the hub itself short-circuits in process. Hub MCP tools (``tool`` steps) are
the hub plugins' own ``mcp_tools(invoke)`` functions, with ``invoke`` running
the cap in process as the run's identity. ``{{secret:name}}`` resolves through
the vault's ``substitute`` at the last moment; every call is journaled with
the placeholders, never the values.

Tests swap in a subclass that overrides :meth:`roster`, :meth:`call` and
:meth:`tool`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ....band_mcp import secret_mask

log = logging.getLogger("rook.hub.plugins.jobs.runtime")


class Runtime:
    def __init__(self, node: Any = None, *, clock=time.time, sleep=asyncio.sleep) -> None:
        self.node = node
        self.clock = clock
        self.sleep = sleep

    # -- roster ------------------------------------------------------------
    @property
    def hub_id(self) -> str:
        return getattr(self.node, "worker_id", "") or "rook"

    def roster(self) -> dict[str, dict]:
        """Live workers by id, plus the hub node itself (named ``rook``)."""
        out: dict[str, dict] = {}
        client = getattr(self.node, "client", None)
        try:
            out.update(dict(getattr(client, "workers", {}) or {}))
        except Exception:
            log.exception("jobs: reading the band roster failed")
        if self.node is not None and hasattr(self.node, "entry"):
            out[self.hub_id] = self.node.entry()
        return out

    # -- calls -------------------------------------------------------------
    async def call(self, cap: str, args: dict, target: str, timeout: float, identity) -> dict:
        """One cap call; returns the reply dict (``ok`` + ``result``/``error``),
        raises ``asyncio.TimeoutError`` on no reply."""
        from ...authz import current_principal
        node = self.node
        client = getattr(node, "client", None)
        # A call to the hub itself runs in process: the cap reads the run's
        # principal from context (caller(), require_hub_admin), not the loop's.
        tok = current_principal.set(identity.principal)
        try:
            if client is not None:
                return await client.call(cap=cap, args=args, target=target, timeout=timeout,
                                         identity=identity.display, principal=identity.principal)
            if node is not None and target == self.hub_id:
                return await asyncio.wait_for(node.dispatch(cap, args, identity.display), timeout)
        finally:
            current_principal.reset(tok)
        raise ConnectionError("the hub has no band client")

    async def tool(self, name: str, args: dict, identity) -> Any:
        """Call a hub MCP tool (``rook_task`` …) as ``identity``; returns its
        reply (the ``tool`` step parses JSON)."""
        node = self.node
        if node is None:
            raise LookupError("no hub node")
        from ...authz import current_principal

        async def invoke(cap: str, cargs: dict) -> Any:
            tok = current_principal.set(identity.principal)
            try:
                return await node.invoke(cap, cargs, identity.display)
            finally:
                current_principal.reset(tok)

        for plugin in node.host.plugins:
            hook = getattr(plugin, "mcp_tools", None)
            if not callable(hook):
                continue
            for fn in hook(invoke) or []:
                if getattr(fn, "__name__", "") == name:
                    return await fn(**args)
        raise LookupError(f"no hub tool named {name!r}")

    # -- secrets and the journal -----------------------------------------
    def substitute(self, obj: Any, actor: str, via: str) -> tuple[Any, dict]:
        """``{{secret:name}}`` -> values (vault.substitute). Raises KeyError
        for an unknown name, LookupError when there is no vault."""
        from ....band_mcp.vault import PLACEHOLDER
        try:
            if not PLACEHOLDER.search(json.dumps(obj, default=str)):
                return obj, {}
        except (TypeError, ValueError):
            return obj, {}
        vault = getattr(self.node, "_vault", None)
        if vault is None:
            raise LookupError("args use {{secret:…}} but the vault is unavailable on this hub")
        return vault.substitute(obj, actor, via=via)

    @staticmethod
    def mask(obj: Any, used: dict | None = None) -> Any:
        """Known vault values (and the values this step used) become
        ``{{secret:name}}`` stubs (band_mcp/secret_mask.py)."""
        return secret_mask.scrub(obj, extra=used or None)

    def journal(self, cap: str, worker: str, identity, args: dict, reply: dict) -> None:
        j = getattr(self.node, "journal", None)
        if j is None:
            return
        try:
            j.record(cap=cap, worker=worker, identity=identity.display, args=args, reply=reply)
        except Exception:
            log.exception("jobs: journaling %s failed", cap)
