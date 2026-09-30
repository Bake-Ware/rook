"""Hub-side enforcement point E1 (``docs/design/permissions.md`` 3.5).

One :class:`Authorizer` per hub process sits *inside* the band client
(``BandClient.authz`` / ``MultiBandClient.authz``), so every hub path that
emits a band call passes through it: ``rook_call``, the MCP tools that call
caps internally (consoles, chat wake, config apply), the dashboard
``/api/call`` and work pages, the migration controller, OTA pushes and hub
plugins. It

1. resolves the **principal** from verified credentials only: the MCP
   attribution wrapper or the dashboard's auth middleware put it in
   :data:`current_principal`; in-process hub code without one is a
   ``system:*`` principal. The envelope ``identity`` stays a display string;
2. evaluates the policy (:mod:`rook.hub.policy`) - in ``audit`` mode (the
   shipped default) nothing is denied, would-be denials are journaled;
3. signs a **call ticket** (3.6) for targeted calls when this hub holds the
   root key, so ticket-verifying workers can check the decision themselves;
4. journals non-allow decisions (coalesced) and exposes the last decision so
   ``rook_call`` can put it on its own journal row.

A crash while evaluating fails open for owner/operator/system principals and
closed for everyone else, and only in ``enforce`` mode (3.8).
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any, Callable

from ..core import authz as core_authz
from .policy import Decision, Principal, PolicyStore, Target

log = logging.getLogger("rook.hub.authz")

#: Principal of the request/tool call being handled (set by the MCP
#: attribution wrapper and the dashboard middleware).
current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "rook_principal", default=None)
#: The most recent decision in this context (``rook_call`` journals it).
last_decision: contextvars.ContextVar[Decision | None] = contextvars.ContextVar(
    "rook_last_decision", default=None)
#: Set by callers that journal the call themselves (``rook_call``), so the
#: authorizer doesn't write a second row.
caller_journals: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rook_caller_journals", default=False)

_COALESCE_SECS = 10.0


# -- principals from verified credentials -----------------------------------

def principal_for_token(att: Any, role: str | None = None) -> Principal:
    """From an MCP :class:`rook.band_mcp.attribution.Attribution`."""
    if att is None or not getattr(att, "verified", False) or att.kind == "unverified":
        return Principal("unverified", "unverified", verified=False,
                         label=getattr(att, "identity", "") or "")
    if att.kind == "shared":
        return Principal("token:static", "token", role or "operator", label="static")
    aid = att.agent_id or att.key_id or "unknown"
    return Principal(f"token:{aid}", "token", role or "agent", label=att.label or "")


def principal_for_user(user: dict | None, owner: bool) -> Principal:
    """From a dashboard account (``admin`` or a band owner -> owner)."""
    if not user:
        return Principal("human:dashboard", "human", "owner", ("human:owner",),
                         label="dashboard")
    role = "owner" if owner else "member"
    return Principal(f"human:{user.get('id')}", "human", role, (f"human:{role}",),
                     label=str(user.get("username") or user.get("name") or user.get("id")))


def system(component: str) -> Principal:
    comp = (component or "unattributed").split(":", 1)[-1] or "unattributed"
    return Principal(f"system:{comp}", "system", "system", label=comp)


BAND_UNAUTHENTICATED = Principal("band:unauthenticated", "band", "", verified=False)


def require_hub_admin(what: str) -> str | None:
    """Hard gate for hub administration caps (``policy.set``, band settings):
    ``None`` when the caller is a band owner, an operator-role token or
    in-process hub code, else the refusal text. Independent of the policy
    mode, so shipping in audit mode never opens these."""
    p = current_principal.get()
    if p is not None and p.verified and p.fail_open():
        return None
    who = p.id if p is not None else "an unauthenticated band caller"
    return f"denied: {what} is for band owners and operator tokens ({who} is neither)"


def target_from_entry(target_id: str | None, entry: dict | None, *,
                      local: bool = False, banned: bool = False) -> Target:
    e = entry or {}
    name = str(e.get("name") or "")
    roles = frozenset(e.get("roles") or ())
    return Target(
        id=target_id, name=name, device_id=str(e.get("device_id") or ""),
        facts=dict(e.get("facts") or {}), roles=roles,
        is_rook=bool(local or "is_hub" in roles),
        claims_rook=bool(e.get("claims_rook")) or name.lower() == core_authz.RESERVED_HUB_NAME,
        banned=banned, tiers=dict(e.get("tiers") or {}))


# -- the authorizer ----------------------------------------------------------

class Authorizer:
    def __init__(self, store: PolicyStore, signer: Any = None,
                 record: Callable[..., Any] | None = None,
                 banned: Callable[[str | None, str | None], bool] | None = None) -> None:
        self.store = store
        self.signer = signer
        self._record = record
        self._banned = banned
        self._recent: dict[tuple, list] = {}

    # -- principal ---------------------------------------------------------
    @staticmethod
    def principal(explicit: Principal | None = None, identity: str | None = None) -> Principal:
        if explicit is not None:
            return explicit
        cur = current_principal.get()
        if cur is not None:
            return cur
        # In-process hub code (timeouts discovery, watchdog, OTA, migration).
        # Its identity string is chosen by hub code, not by a remote caller.
        if isinstance(identity, str) and identity.startswith("system:"):
            return system(identity)
        return system("unattributed")

    # -- decisions ---------------------------------------------------------
    def check(self, cap: str, target_id: str | None, entry: dict | None, *,
              identity: str | None = None, principal: Principal | None = None,
              local: bool = False) -> Decision:
        p = self.principal(principal, identity)
        chain = [p]
        try:
            policy = self.store.current()
            banned = bool(self._banned and self._banned((entry or {}).get("name"), target_id))
            d = policy.evaluate(chain, cap, target_from_entry(target_id, entry, local=local,
                                                              banned=banned))
        except Exception as e:  # 3.8: owners never locked out; others fail closed
            policy = self.store.policy
            enforce = policy.mode_for(p) == "enforce"
            fail_open = p.fail_open() or not enforce
            d = Decision(decision="error_allow" if fail_open else "error_deny", cap=cap,
                         tier=core_authz.effective_tier(cap), principal=p.id, rev=policy.rev,
                         target=target_id, target_name=str((entry or {}).get("name") or ""),
                         reason=f"authorize failed: {type(e).__name__}: {e}")
            log.exception("ROOK AUTHZ ALERT: authorize() failed for %s %s", p.id, cap)
        last_decision.set(d)
        if d.decision not in ("allow", "off"):
            self._journal(d)
        return d

    def _journal(self, d: Decision) -> None:
        if d.decision == "would_deny":
            log.info("authz would_deny: %s %s on %s (%s)", d.principal, d.cap,
                     d.target_name or d.target, d.rule)
        elif d.denied:
            log.warning("authz %s: %s %s on %s (%s)", d.decision, d.principal, d.cap,
                        d.target_name or d.target, d.rule)
        if self._record is None or caller_journals.get():
            return
        key = (d.principal, d.cap, d.target, d.decision)
        now = time.monotonic()
        slot = self._recent.get(key)
        if slot is not None and now - slot[0] < _COALESCE_SECS:
            slot[1] += 1
            return
        count = slot[1] if slot else 0
        self._recent[key] = [now, 0]
        if len(self._recent) > 2048:
            self._recent = {k: v for k, v in self._recent.items() if now - v[0] < _COALESCE_SECS}
        try:
            reply = {"ok": not d.denied, "error": d.reason or d.decision, **d.explain()}
            if count:
                reply["coalesced"] = count
            self._record(cap=d.cap, worker=d.target_name or d.target,
                         identity=d.principal, args=None, reply=reply, authz=d.journal())
        except Exception:
            log.exception("journaling an authz decision failed")

    def record_event(self, cap: str, worker: str | None, detail: dict) -> None:
        """Journal a non-call audit event (``audit.policy``, ``audit.impostor``)."""
        log.warning("%s: %s %s", cap, worker, detail)
        if self._record is None:
            return
        try:
            self._record(cap=cap, worker=worker, identity="system:rook-authz", args=None,
                         reply={"ok": True, **detail})
        except Exception:
            log.exception("journaling %s failed", cap)

    # -- tickets -----------------------------------------------------------
    def ticket(self, d: Decision | None, *, band: str, target: str, msg_id: str,
               args: Any, entry: dict | None) -> dict | None:
        if self.signer is None or d is None or not self._signing():
            return None
        try:
            ready = (entry or {}).get("authz") or {}
            inline = self.signer.kid not in (ready.get("kids") or [])
            return self.signer.ticket(band=band, principal=d.principal, via=list(d.via),
                                      cap=d.cap, target=target, msg_id=msg_id, args=args,
                                      tier=d.tier, rev=d.rev, inline_grant=inline)
        except Exception:
            log.exception("ROOK AUTHZ ALERT: ticket signing failed; call sent without one")
            return None

    def _signing(self) -> bool:
        ready = getattr(self.signer, "ready", None)
        try:
            return bool(ready()) if callable(ready) else bool(getattr(self.signer, "enabled", False))
        except Exception:
            log.exception("hub signer check failed")
            return False

    def decorate_announce(self, msg: dict, band: str) -> dict:
        if self.signer is None or not self._signing():
            return msg
        try:
            return self.signer.decorate_announce(msg, band)
        except Exception:
            log.exception("signing the hub announce failed")
            return msg

    def anchors(self) -> list[str]:
        anchor = getattr(self.signer, "anchor", None)
        out = [anchor] if anchor else []
        for a in core_authz.default_anchors():
            if a not in out:
                out.append(a)
        return out


# MCP tools that act on the hub itself, authorized as caps on `rook`
# (Appendix A.2). Tools that forward to a worker cap (rook_call, consoles,
# chat wake, config) are authorized in the band client as that cap instead.
_TOOL_CAPS = {
    "rook_whoami": "identity.whoami", "rook_workers": "band.workers", "rook_caps": "band.caps",
    "rook_journal": "journal.read",
    "rook_handoff_get": "handoff.read", "rook_handoff_list": "handoff.read",
    "rook_handoff_save": "handoff.write",
    "rook_chat_rooms": "chat.read", "rook_chat_read": "chat.read",
    "rook_chat_start": "chat.write", "rook_chat_send": "chat.write",
    "rook_chat_delete": "chat.delete", "rook_presence": "chat.presence",
    "rook_console_list": "console.read", "rook_console_search": "console.read",
    "rook_console_read": "console.read",
}
_READ_ACTIONS = frozenset({"search", "get", "list", "context", "status", "deck"})


def hub_cap_for_tool(name: str, arguments: Any) -> str | None:
    """The hub cap an MCP tool call is authorized as, or None when the tool
    only forwards to worker caps (authorized in the band client)."""
    args = arguments if isinstance(arguments, dict) else {}
    if name in _TOOL_CAPS:
        return _TOOL_CAPS[name]
    action = str(args.get("action") or "").lower()
    if name == "rook_secret":
        action = action or "list"
        return {"list": "secret.list", "log": "secret.log", "get": "secret.get",
                "set": "secret.set", "delete": "secret.delete"}.get(action, "secret.get")
    if name == "rook_knowledge":
        return "knowledge.read" if (action or "search") in _READ_ACTIONS else "knowledge.write"
    if name in ("rook_task", "rook_project", "rook_concept"):
        default = "deck" if name == "rook_task" else "list"
        return "task.read" if (action or default) in _READ_ACTIONS else "task.write"
    return None


def build_authorizer(data_dir: str | None, *, record: Callable[..., Any] | None = None,
                     banned: Callable[[str | None, str | None], bool] | None = None,
                     signer: Any = None) -> Authorizer:
    """Policy store from ``data_dir`` plus a signer over the hub's keys.
    Never raises: a broken signer just means no tickets."""
    store = PolicyStore(PolicyStore.default_path(data_dir))
    if signer is None:
        try:
            from .keys import HubSigner
            signer = HubSigner()
        except Exception:
            log.exception("ROOK AUTHZ ALERT: hub signer unavailable; no tickets")
            signer = None
    return Authorizer(store, signer, record=record, banned=banned)
