"""``llmanifold.*``: llmanifold as a model provider for this hub.

llmanifold (https://github.com/Bake-Ware/llmanifold) is an LLM front door: one
OpenAI-compatible endpoint in front of local engines and paid APIs, with
lanes, queueing, priorities and fallbacks. Clients ask for a *flow* name or an
alias; llmanifold picks the engine.

This plugin, once its address is set:

* contributes the provider ``llmanifold`` to Model providers (the ``llm``
  plugin), so the home agent (``home.llm_provider = llmanifold``) and what
  uses the home agent's endpoint (its memory) talk to it by name;
* adds ``llmanifold.status`` on worker ``rook``: is the API answering, which
  flows it offers and, with the admin address, its lanes and queue (read only;
  nothing here drains, pauses or edits llmanifold).

The beta worker plugin in the llmanifold repo (``llmanifold_rook``) is the
other half: it runs on the machine llmanifold runs on and carries its admin
caps (drain, undrain, reload). This one is for hubs that only use it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...core.plugin import Plugin, capability, place, setting
from .home.llm import LLMError, aiohttp_request, base_url, has_userinfo, url_host

log = logging.getLogger("rook.hub.plugins.llmanifold")

KEY_REF = r"(\{\{secret:[a-z0-9][a-z0-9._-]{0,63}\}\})?"
#: Fields of one admin /api/status endpoint row worth showing (others vary by version).
#: Seconds llmanifold.status answers from its last check.
STATUS_TTL = 10
_ENDPOINT_FIELDS = ("name", "state", "healthy", "inflight", "lanes", "model", "paused",
                    "stalled", "metered")


class Llmanifold(Plugin):
    """llmanifold: your LLM front door (flows, lanes, fallbacks) as a model provider for this hub."""

    NAMESPACE = "llmanifold"
    NAME = "llmanifold"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("base_url", "url", "", apply="reload", group="llmanifold", order=1,
                label="llmanifold address",
                help="Its OpenAI-compatible API, e.g. http://llm.example.com:1234/v1."),
        setting("api_key", str, "", pattern=KEY_REF, group="llmanifold", order=2,
                label="Token (vault reference)",
                help="{{secret:<name>}} when your flows need a token; blank for open flows."),
        setting("models", list, [], group="llmanifold", order=3, label="Flows to offer",
                help="Flow names or aliases the home agent and others may pick. Blank: "
                     "everything its /v1/models lists."),
        setting("default_model", str, "", group="llmanifold", order=4, label="Default flow",
                help="Blank: llmanifold's own default (it routes a missing or unknown model "
                     "there)."),
        setting("admin_url", "url", "", group="llmanifold", order=5, advanced=True,
                label="Admin address",
                help="Its admin API (port 1240 by default), read only: adds lanes and the "
                     "queue to llmanifold.status. Leave blank if it is not reachable from "
                     "the hub."),
        setting("timeout_s", float, 8.0, min=1.0, max=60.0, group="llmanifold", order=6,
                advanced=True, label="Status timeout (s)"),
    )
    SKILL = ("### llmanifold\n"
             "`llmanifold.status` on worker `rook`: whether the hub's llmanifold (LLM front "
             "door) answers, its flows, and lanes/queue when the admin address is set. The "
             "hub uses it as the model provider `llmanifold`.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._http = aiohttp_request          # tests swap in a fake llmanifold
        self.last_status: dict | None = None
        self._status_for: tuple | None = None   # the settings last_status was taken with

    def bind_host(self, node) -> None:
        self._node = node

    def available(self) -> bool:
        url = str(self.settings.get("base_url") or "")
        return bool(url) and not has_userinfo(url)

    def unavailable_reason(self) -> str:
        if has_userinfo(str(self.settings.get("base_url") or "")):
            return ("The llmanifold address must not carry a user name or password (band "
                    "readers see its host): put the token in the vault and set Token.")
        return ("Set the llmanifold address (Settings > llmanifold, e.g. "
                "http://llm.example.com:1234/v1) to use it as a model provider.")

    # -- the provider ------------------------------------------------------------------
    def provider(self):
        from .llm import Provider
        return Provider(id="llmanifold", label="llmanifold", kind="llmanifold",
                        base_url=base_url(str(self.settings.get("base_url") or "")),
                        api_key=str(self.settings.get("api_key") or ""),
                        models=tuple(str(m) for m in (self.settings.get("models") or []) if m),
                        default_model=str(self.settings.get("default_model") or ""),
                        source="plugin:llmanifold",
                        extra={"admin": bool(self.settings.get("admin_url"))})

    def llm_providers(self) -> list:
        """Read by the model providers plugin on every lookup."""
        return [self.provider()]

    # -- status ---------------------------------------------------------------------------
    async def _admin_status(self) -> dict | None:
        admin = str(self.settings.get("admin_url") or "").rstrip("/")
        if not admin:
            return None
        if has_userinfo(admin):
            return {"ok": False, "error": "the admin address must not carry a user name or "
                                          "password"}
        timeout = float(self.settings.get("timeout_s") or 8.0)
        try:
            code, data = await self._http("GET", admin + "/api/status", None,
                                          {"Accept": "application/json"}, timeout)
        except Exception as e:  # noqa: BLE001 - one line on the page
            return {"ok": False, "error": f"cannot reach {url_host(admin)}: "
                                          f"{type(e).__name__}"}
        if code != 200 or not isinstance(data, dict):
            return {"ok": False, "error": f"admin API: HTTP {code}"}
        endpoints = []
        for e in data.get("endpoints") or []:
            if isinstance(e, dict):
                endpoints.append({k: e[k] for k in _ENDPOINT_FIELDS if k in e})
        queue = data.get("queue")
        return {"ok": True, "version": data.get("version"),
                "reload_error": data.get("reload_error") or None, "endpoints": endpoints,
                "inflight": sum(int(e.get("inflight") or 0) for e in endpoints),
                "queued": len(queue) if isinstance(queue, list) else queue}

    def _status_key(self) -> tuple:
        """What a status answer depends on: a change to any of these (a new
        address, say) is never answered from the cache."""
        return tuple(str(self.settings.get(k) or "") for k in
                     ("base_url", "admin_url", "api_key", "models"))

    async def status(self) -> dict:
        """API reachability and flows (through the model providers plugin's
        client, so the token is resolved the same way) plus admin state."""
        t0 = time.monotonic()
        out: dict = {"host": url_host(str(self.settings.get("base_url") or "")),
                     "checked_at": time.time()}
        llm = self._node.plugin("llm") if self._node is not None else None
        try:
            if llm is not None:
                flows = await llm.models("llmanifold", actor="system:llmanifold", live=True)
            else:
                from .home.llm import ChatClient
                flows = await ChatClient(self.provider().base_url, http=self._http,
                                         timeout=float(self.settings.get("timeout_s") or 8.0)
                                         ).models()
            out["api"] = {"ok": True, "flows": flows[:50], "flow_count": len(flows),
                          "latency_ms": round((time.monotonic() - t0) * 1000)}
        except (LLMError, ValueError) as e:
            out["api"] = {"ok": False, "error": str(e)[:300]}
        out["admin"] = await self._admin_status()
        out["ok"] = bool(out["api"].get("ok"))
        self.last_status, self._status_for = out, self._status_key()
        return out

    async def provider_detail(self) -> dict:
        """What Model providers shows under its check (llm.check)."""
        st = await self.status()
        return {"flows": st.get("api", {}).get("flows", []), "admin": st.get("admin")}

    @capability("status", risk="read", fields="*")
    async def status_cap(self) -> dict:
        """llmanifold.status — whether the hub's llmanifold answers, its flows, and lanes/queue when the admin address is set."""
        # Band readers share one answer for a few seconds: each live one is
        # outbound requests (and a vault read for the token).
        last = self.last_status
        if (last is not None and self._status_for == self._status_key()
                and time.time() - float(last.get("checked_at") or 0) < STATUS_TTL):
            return last
        return await self.status()


PLUGIN = Llmanifold
