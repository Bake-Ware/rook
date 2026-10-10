"""Who the identities a job can run as are, and whether they still work.

A :class:`Directory` answers from the hub's live credential stores:

* API keys: the MCP bridge's token store (``rook.band_mcp.tokens.TokenStore``,
  ``node.tokens``). A key's principal is ``token:<agent_id>`` (stable across
  rotation). A key that was revoked or has expired is *revoked*.
* Users: the dashboard account store (``node.accounts``, a callable or an
  ``AccountStore``). A user is ``human:<id>``; a deleted account is revoked.
* The vault (``node._vault``): a vault identity is a secret holding a Rook API
  key. Its value is read at run time to find the key's principal and is never
  stored or returned.

Anything the directory cannot check (no token store on this hub, a principal
kind it does not know) counts as active: the authorizer still checks every
call the run makes.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("rook.hub.plugins.jobs.principals")

SECRET_REF = re.compile(r"^\s*\{\{\s*secret:([A-Za-z0-9_.-]+)\s*\}\}\s*$")
ALWAYS_ACTIVE = ("token:static", "human:dashboard", "unverified")


@dataclass
class Found:
    """A resolved identity. ``info`` has the ``owner_info`` shape
    (id, kind, role, groups, label, verified); ``owner`` is who owns the key
    (when the store records it)."""

    info: dict | None
    active: bool = True
    reason: str = ""
    owner: str | None = None
    extra: dict = field(default_factory=dict)


def secret_name(ref: Any) -> str | None:
    """``name`` or ``{{secret:name}}`` -> ``name``."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    m = SECRET_REF.match(ref)
    if m:
        return m.group(1)
    return ref.strip() if re.fullmatch(r"[A-Za-z0-9_.-]+", ref.strip()) else None


def _token_info(meta: dict) -> dict:
    role = meta.get("role") or "agent"
    return {"id": f"token:{meta.get('agent_id')}", "kind": "token", "role": role, "groups": [],
            "label": str(meta.get("name") or meta.get("id") or "api"), "verified": True}


class Directory:
    def __init__(self, tokens: Any = None, accounts: Any = None, vault: Any = None,
                 clock=time.time) -> None:
        self.tokens = tokens
        self._accounts = accounts
        self.vault = vault
        self.clock = clock

    @classmethod
    def for_node(cls, node: Any) -> "Directory":
        if node is None:
            return cls()
        return cls(getattr(node, "tokens", None), getattr(node, "accounts", None),
                   getattr(node, "_vault", None))

    @property
    def accounts(self) -> Any:
        a = self._accounts
        if a is not None and not hasattr(a, "user") and callable(a):
            try:
                self._accounts = a = a()
            except Exception:  # noqa: BLE001
                log.exception("jobs: opening the account store failed")
                self._accounts = a = None
        return a

    # -- keys ------------------------------------------------------------------
    def _token_list(self) -> list[dict] | None:
        if self.tokens is None or not hasattr(self.tokens, "list_api_tokens"):
            return None
        try:
            return list(self.tokens.list_api_tokens())
        except Exception:  # noqa: BLE001
            log.exception("jobs: listing API tokens failed")
            return None

    def _expired(self, meta: dict) -> bool:
        exp = meta.get("expires_at")
        return exp is not None and exp <= self.clock()

    def lookup(self, ref: Any) -> Found | None:
        """A key or user by reference: ``token:<agent_id>``, ``human:<id>``,
        a key id, an agent id or a key's (unique) name. ``None`` when the
        reference matches nothing this directory can see."""
        if not isinstance(ref, str) or not ref.strip():
            return None
        ref = ref.strip()
        if ref in ALWAYS_ACTIVE:
            kind = ref.split(":", 1)[0]
            return Found({"id": ref, "kind": kind, "role": "operator" if ref == "token:static" else "owner",
                          "groups": ["human:owner"] if kind == "human" else [], "label": ref,
                          "verified": ref != "unverified"})
        if ref.startswith("human:"):
            return self._user(ref[6:])
        tokens = self._token_list()
        if tokens is None:
            return None
        want = ref[6:] if ref.startswith("token:") else ref
        hits = [t for t in tokens if want in (t.get("agent_id"), t.get("id"))]
        if not hits and not ref.startswith("token:"):
            hits = [t for t in tokens if t.get("name") == want]
            if len(hits) > 1:
                raise ValueError(f"{len(hits)} API keys are named {want!r}; use its token:<agent_id>")
        if not hits:
            if ref.startswith("token:"):
                return Found({"id": ref, "kind": "token", "role": "agent", "groups": [], "label": ref},
                             active=False, reason=f"API key {ref} was revoked")
            return None
        live = [t for t in hits if not self._expired(t)]
        meta = (live or hits)[0]
        info = _token_info(meta)
        if not live:
            return Found(info, active=False, reason=f"API key {info['label']!r} expired")
        return Found(info, owner=meta.get("owner"), extra={"key_id": meta.get("id")})

    def _user(self, uid: str) -> Found | None:
        acc = self.accounts
        if acc is None:
            return None
        try:
            user = acc.user(uid)
        except Exception:  # noqa: BLE001
            log.exception("jobs: reading account %s failed", uid)
            return None
        if not user:
            return Found({"id": f"human:{uid}", "kind": "human", "role": "member", "groups": [],
                          "label": uid}, active=False, reason=f"user human:{uid} no longer exists")
        if user.get("disabled"):
            return Found({"id": f"human:{uid}", "kind": "human", "role": "member", "groups": [],
                          "label": uid}, active=False, reason=f"user human:{uid} is disabled")
        role = "owner" if user.get("admin") else "member"
        return Found({"id": f"human:{uid}", "kind": "human", "role": role, "groups": [f"human:{role}"],
                      "label": str(user.get("username") or user.get("name") or uid), "verified": True},
                     owner=f"human:{uid}")

    def status(self, info: dict | None) -> Found:
        """Whether a stored principal (a job's ``owner_info``) still works."""
        if not info:
            return Found(info)
        pid = str(info.get("id") or "")
        kind = str(info.get("kind") or "")
        if kind in ("system", "unknown", "job") or pid in ALWAYS_ACTIVE:
            return Found(info)
        if kind not in ("token", "human"):
            return Found(info)
        try:
            found = self.lookup(pid)
        except ValueError:
            found = None
        if found is None:
            return Found(info)  # nothing to check against here
        if not found.active:
            return Found(info, active=False, reason=found.reason)
        return Found(info)

    # -- vault -------------------------------------------------------------------
    def from_vault(self, ref: Any, actor: str) -> Found:
        """The principal of the API key held in vault secret ``ref``. The value
        is used for the lookup only."""
        name = secret_name(ref)
        if name is None:
            raise ValueError(f"vault identity needs a secret name, not {ref!r}")
        if self.vault is None:
            raise LookupError("the vault is unavailable on this hub")
        if self.tokens is None or not hasattr(self.tokens, "principal_for"):
            raise LookupError("this hub cannot check API keys (no token store)")
        try:
            raw = self.vault.get(name, actor, via="job identity")
        except KeyError:
            return Found({"id": f"vault:{name}", "kind": "vault", "role": "", "groups": [], "label": name},
                         active=False, reason=f"vault secret {name!r} no longer exists")
        p = self.tokens.principal_for(raw)
        del raw
        if p is None:
            return Found({"id": f"vault:{name}", "kind": "vault", "role": "", "groups": [], "label": name},
                         active=False, reason=f"the API key in vault secret {name!r} was revoked or expired")
        if p.get("kind") == "shared":
            info = {"id": "token:static", "kind": "token", "role": "operator", "groups": [],
                    "label": "static", "verified": True}
        else:
            info = {"id": f"token:{p.get('agent_id')}", "kind": "token", "role": p.get("role") or "agent",
                    "groups": [], "label": str(p.get("label") or "api"), "verified": True}
        return Found(info, extra={"secret": name})
