"""``llm.*``: the language models this hub can use (Manage > Model providers).

A *model provider* is an OpenAI-compatible endpoint (``/v1/models``,
``/v1/chat/completions``) with an optional API key (a vault reference,
``{{secret:name}}``, never the key) and the models it offers. Other hub code
picks one by id instead of each keeping its own URL and key:

* the home agent (``home.llm_provider``), and through it everything that uses
  the home agent's endpoint (its memory's chat and embeddings);
* agents and jobs on the band, read-only: ``llm.providers``, ``llm.models``,
  ``llm.check`` on worker ``rook``.

Providers come from two places:

* this plugin's ``llm.providers`` setting, edited on the Model providers page
  (any OpenAI-compatible server: llama.cpp, vLLM, LM Studio, Ollama, a paid API);
* other hub plugins that define ``llm_providers()`` (the llmanifold plugin
  contributes ``llmanifold``). They are read on every lookup, so a plugin
  that reloads or switches off simply appears or disappears.

See docs/design/model-providers.md.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from ....core.settings import SECRET_REF
from ..home.llm import ChatClient, LLMError, aiohttp_request, base_url, has_userinfo, url_host

log = logging.getLogger("rook.hub.plugins.llm")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KEY_REF = re.compile(r"^(\{\{secret:[a-z0-9][a-z0-9._-]{0,63}\}\})?$")
MAX_PROVIDERS = 32
MAX_MODELS = 200
#: llm.models / llm.check calls one band caller may make a minute, and how
#: many may run at once across all callers (each can be an outbound request
#: and a vault read).
CALLS_PER_MINUTE = 20
CALLS_AT_ONCE = 4


class ProviderError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    """One model provider. ``api_key`` is a vault reference or empty."""

    id: str
    base_url: str
    label: str = ""
    kind: str = "openai"            # openai | llmanifold | ... (who knows its extras)
    api_key: str = ""
    models: tuple = ()              # offered; empty = whatever /models lists
    default_model: str = ""
    source: str = "settings"        # settings | plugin:<namespace>
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def host(self) -> str:
        return url_host(self.base_url)

    def describe(self, *, full: bool = False) -> dict:
        """For pages and caps: never the key, and the URL only for ``full``
        (the operator's page); band readers see the host."""
        out = {"id": self.id, "label": self.label or self.id, "kind": self.kind,
               "host": self.host, "models": list(self.models),
               "default_model": self.default_model, "source": self.source,
               "key_set": bool(self.api_key)}
        if full:
            out["base_url"] = self.base_url
            out["api_key"] = self.api_key
        return out


def provider_from_row(row: Any) -> Provider:
    """Validate one ``llm.providers`` entry (a dict) into a Provider."""
    if not isinstance(row, dict):
        raise ProviderError("a provider is an object with id and base_url")
    pid = str(row.get("id") or "").strip().lower()
    if not ID_RE.match(pid):
        raise ProviderError("id: 1-32 lowercase letters, digits, - or _ (e.g. lab-gpu)")
    url = base_url(str(row.get("base_url") or ""))
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ProviderError(f"{pid}: base_url must be http(s)://host[:port]/v1")
    if has_userinfo(url):
        # Band readers see the host (llm.providers): keep credentials out of it.
        raise ProviderError(f"{pid}: base_url must not carry a user name or password; "
                            "put the key in the vault and name it as the API key")
    key = str(row.get("api_key") or "").strip()
    if not KEY_REF.match(key):
        raise ProviderError(f"{pid}: the API key must be a vault reference, {{{{secret:name}}}}")
    models = row.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",")]
    if not isinstance(models, list):
        raise ProviderError(f"{pid}: models must be a list of model ids")
    models = tuple(dict.fromkeys(str(m).strip() for m in models if str(m).strip()))[:MAX_MODELS]
    return Provider(id=pid, base_url=url, label=str(row.get("label") or "").strip()[:60],
                    kind="openai", api_key=key, models=models,
                    default_model=str(row.get("default_model") or "").strip()[:200])


class ModelProviders(Plugin):
    """Model providers: the language models this hub can use, by name, for the home agent and the band."""

    NAMESPACE = "llm"
    NAME = "llm"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("providers", list, [], group="Providers", order=1,
                label="OpenAI-compatible providers",
                help='[{"id": "lab-gpu", "label": "Lab GPU", "base_url": "http://llm.example:8080/v1", '
                     '"api_key": "{{secret:lab-key}}", "models": [], "default_model": ""}]. '
                     "Easier on Manage > Model providers."),
        setting("check_timeout_s", float, 10.0, min=1.0, max=60.0, group="Providers", order=2,
                advanced=True, label="Check timeout (s)"),
    )
    SKILL = ("### model providers\n"
             "The language models this hub can reach: `llm.providers` (ids, hosts, models), "
             "`llm.models(provider=…)`, `llm.check(provider=…)` on worker `rook`. Read only; "
             "the home agent uses one of them.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self.checks: dict[str, dict] = {}
        self._http = aiohttp_request            # tests swap in a fake endpoint
        self._calls: dict[str, collections.deque] = {}
        self._running: asyncio.Semaphore | None = None

    def bind_host(self, node) -> None:
        self._node = node

    # -- the registry ------------------------------------------------------------
    def configured(self) -> list[Provider]:
        out = []
        for row in (self.settings.get("providers") or [])[:MAX_PROVIDERS]:
            try:
                out.append(provider_from_row(row))
            except ProviderError as e:
                log.warning("llm.providers: skipping an entry: %s", e)
        return out

    def contributed(self) -> list[Provider]:
        """Providers other loaded hub plugins offer (``llm_providers()``)."""
        out = []
        host = getattr(self._node, "host", None)
        for p in list(getattr(host, "plugins", []) or []):
            fn = getattr(p, "llm_providers", None)
            if p is self or not callable(fn):
                continue
            try:
                out.extend(x for x in (fn() or []) if isinstance(x, Provider))
            except Exception:
                log.exception("llm: %s.llm_providers() failed", p.NAMESPACE)
        return out

    def providers(self) -> list[Provider]:
        seen: dict[str, Provider] = {}
        # A plugin's provider wins over a hand-made one with the same id: the
        # plugin owns that name (the page refuses to save such an id anyway).
        for p in self.configured() + self.contributed():
            seen[p.id] = p
        return sorted(seen.values(), key=lambda p: (p.source != "settings", p.id))

    def get(self, provider_id: str) -> Provider | None:
        pid = str(provider_id or "").strip().lower()
        return next((p for p in self.providers() if p.id == pid), None)

    def require(self, provider_id: str) -> Provider:
        p = self.get(provider_id)
        if p is None:
            known = ", ".join(x.id for x in self.providers()) or "none yet"
            raise ProviderError(f"no model provider named {provider_id!r} (known: {known})")
        return p

    # -- using one -----------------------------------------------------------------
    def api_key(self, provider: Provider, actor: str, via: str = "model providers") -> str | None:
        ref = (provider.api_key or "").strip()
        if not ref:
            return None
        m = SECRET_REF.match(ref)
        vault = getattr(self._node, "_vault", None)
        if not m or vault is None:
            raise ProviderError("the provider's API key is unavailable (no vault)")
        try:
            return vault.get(m.group(1), actor, via=via)
        except KeyError:
            raise ProviderError(f"no vault secret named {m.group(1)!r}") from None

    def client(self, provider_id: str, model: str | None = None, *, actor: str = "system:rook-hub",
               timeout: float = 60.0, via: str = "model providers") -> ChatClient:
        """A chat client for one provider (its default model unless one is named)."""
        p = self.require(provider_id)
        model = model or p.default_model or (p.models[0] if len(p.models) == 1 else "")
        return ChatClient(p.base_url, model, self.api_key(p, actor, via), timeout, self._http)

    async def models(self, provider_id: str, *, actor: str = "system:rook-hub",
                     live: bool = False) -> list[str]:
        """The models a provider offers: its configured list, else (or with
        ``live``) what its ``/models`` lists."""
        p = self.require(provider_id)
        if p.models and not live:
            return list(p.models)
        cli = self.client(p.id, actor=actor, timeout=self._timeout())
        return await cli.models(timeout=self._timeout())

    def _timeout(self) -> float:
        return float(self.settings.get("check_timeout_s") or 10.0)

    async def check(self, provider_id: str, *, actor: str = "system:rook-hub") -> dict:
        """Is it answering? ``{ok, latency_ms, models, error, checked_at}``,
        remembered for the page and ``llm.providers``."""
        t0 = time.monotonic()
        out: dict = {"provider": provider_id, "checked_at": time.time()}
        try:
            got = await self.models(provider_id, actor=actor, live=True)
            out.update(ok=True, models=len(got), sample=got[:8])
        except (LLMError, ProviderError) as e:
            out.update(ok=False, error=str(e)[:300])
        out["latency_ms"] = round((time.monotonic() - t0) * 1000)
        p = self.get(provider_id)
        if p is not None and p.source.startswith("plugin:") and self._node is not None:
            # The plugin behind it may know more (llmanifold: lanes, queue).
            owner = self._node.plugin(p.source.split(":", 1)[1])
            fn = getattr(owner, "provider_detail", None)
            if callable(fn):
                try:
                    out["detail"] = await fn()
                except Exception:  # noqa: BLE001 - the basic check stands
                    log.exception("llm: %s detail failed", p.source)
        if p is not None:
            self.checks[p.id] = out
        return out

    # -- caps (read only) ------------------------------------------------------------
    @staticmethod
    def _caller() -> str:
        ident = context.caller_identity.get()
        return str(ident) if ident else "system:rook-hub"

    def _admit(self, who: str) -> None:
        """Count one cap call for ``who``; refuse past CALLS_PER_MINUTE."""
        now = time.monotonic()
        for key in [k for k, q in self._calls.items() if not q or q[-1] < now - 60]:
            del self._calls[key]
        q = self._calls.setdefault(who, collections.deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= CALLS_PER_MINUTE:
            raise ProviderError(f"too many model provider calls (at most {CALLS_PER_MINUTE} "
                                "a minute); try again shortly")
        q.append(now)

    def _slot(self) -> asyncio.Semaphore:
        if self._running is None:
            self._running = asyncio.Semaphore(CALLS_AT_ONCE)
        return self._running

    @capability("providers", risk="read", fields="*")
    def providers_cap(self) -> dict:
        """llm.providers — the model providers this hub can use: id, kind, host, offered models, last check."""
        rows = []
        for p in self.providers():
            row = p.describe()
            c = self.checks.get(p.id)
            if c:
                row["last_check"] = {k: c.get(k) for k in ("ok", "latency_ms", "models",
                                                           "checked_at")}
            rows.append(row)
        return {"providers": rows}

    @capability("models", risk="read")
    async def models_cap(self, provider: str, live: bool = False) -> dict:
        """llm.models — model ids one provider offers (live=true asks its /models)."""
        who = self._caller()
        try:
            self._admit(who)
            async with self._slot():
                return {"provider": provider,
                        "models": await self.models(provider, actor=who, live=live)}
        except (LLMError, ProviderError) as e:
            return {"provider": provider, "error": str(e)[:300]}

    @capability("check", risk="read")
    async def check_cap(self, provider: str = "") -> dict:
        """llm.check — whether a provider (or every one) answers, with latency."""
        who = self._caller()
        try:
            self._admit(who)
        except ProviderError as e:
            return {"checks": [], "error": str(e)}
        ids = [provider] if provider else [p.id for p in self.providers()]
        out = []
        for pid in ids:
            async with self._slot():
                out.append(await self.check(pid, actor=who))
        return {"checks": out}

    # -- Manage > Model providers ------------------------------------------------------
    async def page(self, svc) -> dict:
        secrets = []
        vault = getattr(self._node, "_vault", None)
        if vault is not None:
            with contextlib.suppress(Exception):
                secrets = [s["name"] for s in vault.list()]
        r = svc.resolve("llm.providers")
        users = []
        home = self._node.plugin("home") if self._node is not None else None
        if home is not None:
            with contextlib.suppress(Exception):
                pid = str(home.settings.get("llm_provider") or "")
                if pid:
                    users.append({"provider": pid, "who": "Home agent"})
        rows = []
        for p in self.providers():
            row = p.describe(full=p.source == "settings")
            row["last_check"] = self.checks.get(p.id)
            row["extra"] = p.extra
            rows.append(row)
        return {"view": "llm", "providers": rows, "secrets": secrets, "users": users,
                "locked": bool(r.get("locked")), "env": r.get("env"),
                "plugins": self._plugin_rows(),
                "history": svc.store.history(key="llm.", limit=20)}

    def _plugin_rows(self) -> list[dict]:
        """Provider plugins that are not contributing (so the page can say why)."""
        host = getattr(self._node, "host", None)
        st = (getattr(host, "status", {}) or {}).get("llmanifold")
        if not st or st.get("state") == "loaded":
            return []
        return [{"module": "llmanifold", "state": st.get("state"), "reason": st.get("reason", "")}]

    async def page_action(self, svc, data: dict, actor: str) -> dict:
        action = data.get("action")
        if action in ("llm_check", "llm_models"):
            pid = str(data.get("provider") or "")
            if action == "llm_check":
                return {"ok": True, "check": await self.check(pid, actor=actor)}
            try:
                return {"ok": True, "models": await self.models(pid, actor=actor, live=True)}
            except (LLMError, ProviderError) as e:
                return {"ok": False, "error": str(e)}
        rows = list(self.settings.get("providers") or [])
        if action == "llm_save":
            new = provider_from_row(data.get("provider") or {})
            owned = {p.id for p in self.contributed()}
            if new.id in owned:
                raise ProviderError(f"{new.id!r} belongs to a plugin; pick another id")
            entry = {"id": new.id, "label": new.label, "base_url": new.base_url,
                     "api_key": new.api_key, "models": list(new.models),
                     "default_model": new.default_model}
            replaced = False
            for i, row in enumerate(rows):
                if isinstance(row, dict) and str(row.get("id") or "").lower() == new.id:
                    rows[i], replaced = entry, True
            if not replaced:
                if len(rows) >= MAX_PROVIDERS:
                    raise ProviderError(f"at most {MAX_PROVIDERS} providers")
                rows.append(entry)
        elif action == "llm_delete":
            pid = str(data.get("provider") or "").lower()
            rows = [r for r in rows if not (isinstance(r, dict)
                                            and str(r.get("id") or "").lower() == pid)]
            self.checks.pop(pid, None)
        else:
            raise ProviderError("use llm_save, llm_delete, llm_check or llm_models")
        svc.set("llm.providers", rows, actor=actor, source="ui", note="Model providers page")
        return {"ok": True, **(await self.page(svc))}


PLUGIN = ModelProviders
