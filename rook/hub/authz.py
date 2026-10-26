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
3. adds **approval holds** (3.11, :mod:`rook.hub.approvals`): an agent's
   hard stop or a change on something the trust map protects waits for a
   person (operator ``hold`` rules are ignored). :meth:`Authorizer.check` only
   decides that a call is held (``decision == "hold"``); the band client then
   awaits :meth:`Authorizer.resolve` before it sends anything. Holds apply
   whatever the policy mode;
4. signs a **call ticket** (3.6) for targeted calls when this hub holds the
   root key, so ticket-verifying workers can check the decision themselves;
5. journals non-allow decisions (coalesced) and exposes the last decision so
   ``rook_call`` can put it on its own journal row.

A crash while evaluating fails open for owner/operator/system principals and
closed for everyone else, and only in ``enforce`` mode (3.8).
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import os
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

def principal_for_token(att: Any, access: Any = None,
                        is_admin: Callable[[str], bool] | None = None) -> Principal:
    """From an MCP :class:`rook.band_mcp.attribution.Attribution`.

    An API key only says which person is calling (access v2 item 7): its
    role and groups are its person's (``owner`` or ``member``, from
    ``access``, an :class:`rook.hub.access.AccessStore`; ``is_admin(user)``
    says whether an account is an operator, an owner whatever access.db
    says), and ``user`` is the account, so grants and the policy treat the
    key exactly as the person's dashboard session. The id stays
    ``token:<agent_id>`` for attribution and the agent hard stops. The
    static key, and any key on a hub with no access store or no owner yet,
    is the hub's own owner."""
    if att is None or not getattr(att, "verified", False) or att.kind == "unverified":
        return Principal("unverified", "unverified", verified=False,
                         label=getattr(att, "identity", "") or "")
    if att.kind == "shared":
        return Principal("token:static", "token", "owner", ("human:owner",), label="static")
    aid = att.agent_id or att.key_id or "unknown"
    return principal_for_agent(aid, att.label or "", access, is_admin)


def principal_for_agent(agent_id: str, label: str = "", access: Any = None,
                        is_admin: Callable[[str], bool] | None = None) -> Principal:
    """The principal of the API key ``agent_id`` (see :func:`principal_for_token`).
    A store that cannot be read makes a member with no account: grants then
    deny everything (members fail closed); owners by account are kept by
    ``is_admin``."""
    pid = f"token:{agent_id}"
    if access is None:
        return Principal(pid, "token", "owner", ("human:owner",), label=label)
    try:
        uid = access.token_user(agent_id, label=label)
    except Exception:  # noqa: BLE001 - unreadable: not an owner by this route
        log.exception("ROOK AUTHZ ALERT: whose key %s is could not be read", agent_id)
        return Principal(pid, "token", "member", ("human:member",), label=label, user="")
    if uid is None:
        return Principal(pid, "token", "owner", ("human:owner",), label=label)
    owner = False
    try:
        owner = access.is_owner(uid)
    except Exception:  # noqa: BLE001 - a member until it can be read
        log.exception("ROOK AUTHZ ALERT: access store unreadable for %s", uid)
    if not owner and is_admin is not None:
        try:
            owner = bool(is_admin(uid))
        except Exception:  # noqa: BLE001
            log.exception("operator check for %s failed", uid)
    role = "owner" if owner else "member"
    return Principal(pid, "token", role, (f"human:{role}",), label=label, user=uid)


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
    ``None`` when the caller is a hub owner (signed in, or an API key that
    belongs to one) or in-process hub code, else the refusal text.
    Independent of the policy mode, so shipping in audit mode never opens
    these."""
    p = current_principal.get()
    if p is not None and p.verified and p.fail_open():
        return None
    who = p.id if p is not None else "an unauthenticated band caller"
    return f"denied: {what} is for hub owners ({who} is not one, or acts for someone who is not)"


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
                 banned: Callable[[str | None, str | None], bool] | None = None,
                 approvals: Any = None, access: Any = None) -> None:
        self.store = store
        self.signer = signer
        self._record = record
        self._banned = banned
        self._recent: dict[tuple, list] = {}
        #: :class:`rook.hub.approvals.Approvals`, or None (no data dir): then
        #: an operator hold rule refuses the call, since nobody could approve it.
        self.approvals = approvals
        #: :class:`rook.hub.access.AccessStore`: what members may reach. None
        #: (no data dir: tests, tools) means nobody is limited by grants.
        self.access = access
        self._client: Any = None

    def attach_client(self, client: Any) -> None:
        """The band client this authorizer guards (the approval contact
        reaches the operator through it; grants ask it which bands a worker
        is on)."""
        self._client = client
        if self.approvals is not None:
            self.approvals.attach_client(client)

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
              local: bool = False, args: Any = None) -> Decision:
        """The policy's decision for one call, with the approval layer
        (holds, hard stops: rook/hub/hardstops.py, which read ``args``)."""
        p = self.principal(principal, identity)
        # An API key is its person to the policy (access v2 item 7); the
        # decision still names the key, which made the call.
        person = p.as_person()
        target = None
        try:
            policy = self.store.current()
            banned = bool(self._banned and self._banned((entry or {}).get("name"), target_id))
            target = target_from_entry(target_id, entry, local=local, banned=banned)
            d = policy.evaluate([person], cap, target)
            if person is not p:
                d.principal, d.via = p.id, tuple(p.via)
            d = self._grants(d, p, target)
            d = self._approvals(d, p, policy, target, args)
        except Exception as e:  # 3.8: owners never locked out; others fail closed
            policy = self.store.policy
            enforce = policy.mode_for(person) == "enforce"
            fail_open = p.fail_open() or not enforce
            if self.access is not None:
                from . import access as _access
                if _access.restricted(p):
                    fail_open = False  # a member's reach is never decided by a crash
            d = Decision(decision="error_allow" if fail_open else "error_deny", cap=cap,
                         tier=core_authz.effective_tier(cap), principal=p.id, rev=policy.rev,
                         target=target_id, target_name=str((entry or {}).get("name") or ""),
                         reason=f"authorize failed: {type(e).__name__}: {e}")
            log.exception("ROOK AUTHZ ALERT: authorize() failed for %s %s", p.id, cap)
            if d.decision == "error_allow" and target is not None:
                # Failing open for an operator token must not skip the holds.
                d = self._approvals(d, p, policy, target, args)
        d.label = p.label
        last_decision.set(d)
        if d.decision not in ("allow", "off", "hold"):
            self._journal(d)  # holds are journaled when the hold is created
        return d

    def bands_of(self, target_id: str | None) -> set[str]:
        """Key hashes of the bands ``target_id`` was seen on."""
        c = self._client
        if c is None or not target_id:
            return set()
        clients = getattr(c, "_clients", None)
        if clients is None:
            clients = [c]
        return {h for x in clients if target_id in (getattr(x, "workers", None) or {})
                for h in [getattr(x, "psk_hash", "")] if h}

    def _grants(self, d: Decision, p: Principal, target: Target) -> Decision:
        """Access v2: a signed-in member reaches only what a grant names,
        in every policy mode; an error denies (owners are never limited).
        A call made on someone's behalf (a job run, an agent inside one)
        never exceeds any member in its on-behalf-of chain either: each
        needs a grant for it too."""
        if self.access is None or d.denied:
            return d
        try:
            people = self._limited(p)
        except Exception as e:  # noqa: BLE001 - fail closed for members
            log.exception("ROOK AUTHZ ALERT: access check failed for %s %s", p.id, d.cap)
            return dataclasses.replace(d, decision="error_deny", rule="access",
                                       reason=f"access check failed: {type(e).__name__}")
        if not people:
            return d
        tags = list(d.tags)
        for uid, who in people:
            try:
                grant, why = self.access.evaluate(uid, d.cap, d.tier, target, self.bands_of(target.id))
            except Exception as e:  # noqa: BLE001 - fail closed for members
                log.exception("ROOK AUTHZ ALERT: access check failed for %s %s", who, d.cap)
                return dataclasses.replace(d, decision="error_deny", rule="access",
                                           reason=f"access check failed: {type(e).__name__}")
            if grant is None:
                if who != p.id:
                    why = f"acting for {who}: {why}"
                return dataclasses.replace(d, decision="deny", rule="access:no-grant", reason=why)
            tags.append(f"grant:{grant['id']}")
        return dataclasses.replace(d, tags=tuple(tags))

    def _limited(self, p: Principal) -> list[tuple[str, str]]:
        """``(user id, principal id)`` of every person whose grants bound
        this call: the caller when :func:`access.restricted`, and each person
        in ``p.via`` whom the access store records as a member (owners, and
        accounts it does not know, are not limited here; the jobs plugin
        refuses to run as a person with no access)."""
        from . import access as _access
        out: list[tuple[str, str]] = []
        me = _access.user_of(p)
        if _access.restricted(p):
            out.append((me, p.id))
        seen = {me}
        for v in p.via or ():
            uid = _access.user_of_id(v)
            if uid is None or uid in seen:
                continue
            seen.add(uid)
            if self.access.role(uid) == "member":
                out.append((uid, str(v)))
        return out

    def _approvals(self, d: Decision, p: Principal, policy: Any, target: Any,
                   args: Any = None) -> Decision:
        if self.approvals is None:
            if d.held:
                return dataclasses.replace(d, decision="deny", rule=d.rule,
                                           reason="held, but this hub has no approval queue")
            return d
        try:
            return self.approvals.apply(d, p, policy, target, args)
        except Exception as e:  # noqa: BLE001
            log.exception("ROOK AUTHZ ALERT: approval check failed for %s %s", p.id, d.cap)
            if p.kind in ("human", "system"):
                return d
            return dataclasses.replace(d, decision="error_deny",
                                       reason=f"approval check failed: {type(e).__name__}")

    async def resolve(self, d: Decision, args: Any) -> "Decision | dict":
        """Settle a held decision before the call is sent (3.11): an allow
        (approved), a deny (a person denied it) or the ``held`` reply dict.
        Anything else is returned unchanged."""
        if d is None or not d.held:
            return d
        if self.approvals is None:
            out: Any = dataclasses.replace(d, decision="deny",
                                           reason="held, but this hub has no approval queue")
        else:
            try:
                out = await self.approvals.resolve(d, args, label=d.label)
            except Exception as e:  # noqa: BLE001 - a broken queue holds nothing open
                log.exception("ROOK AUTHZ ALERT: resolving hold for %s %s failed",
                              d.principal, d.cap)
                out = dataclasses.replace(d, decision="error_deny",
                                          reason=f"approval queue failed: {type(e).__name__}")
        last_decision.set(out if isinstance(out, Decision) else d)
        if isinstance(out, Decision) and out.denied and not out.hold_id:
            self._journal(out)
        return out

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

    def signing_ready(self) -> bool:
        """True when targeted calls will carry a signed ticket."""
        return self.signer is not None and self._signing()

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
    "rook_install_claude_code": "hub.info",
    "rook_home_ask": "home.ask",
}
_READ_ACTIONS = frozenset({"search", "get", "list", "context", "status", "deck", "board"})


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
    if name == "rook_memory":
        return {"add": "memory.remember", "forget": "memory.forget",
                "status": "memory.status"}.get(action or "search", "memory.recall")
    if name == "rook_huddle":
        return "huddle.read" if (action or "read") in ("read", "list", "mine") else "huddle.write"
    if name == "rook_jobs":
        from .plugins.jobs.service import route
        return route(action or "list")
    return None


def build_authorizer(data_dir: str | None, *, record: Callable[..., Any] | None = None,
                     banned: Callable[[str | None, str | None], bool] | None = None,
                     signer: Any = None, settings_store: Any = None) -> Authorizer:
    """Policy store from ``data_dir`` plus a signer over the hub's keys, and
    the approval queue (``approvals.db``) in the same directory, so the two
    hub processes share it as they share ``policy.json``. Never raises: a
    broken signer just means no tickets."""
    store = PolicyStore(PolicyStore.default_path(data_dir))
    approvals = None
    if data_dir:
        try:
            from .approvals import Approvals, ConfigSource
            from .trust import store_for
            approvals = Approvals(os.path.join(data_dir, "approvals.db"),
                                  ConfigSource(store=settings_store), record=record,
                                  trust=store_for(os.path.join(data_dir, "trust.json")))
        except Exception:
            log.exception("ROOK AUTHZ ALERT: approval queue unavailable; hold rules refuse")
    if signer is None:
        try:
            from .keys import HubSigner
            signer = HubSigner()
        except Exception:
            log.exception("ROOK AUTHZ ALERT: hub signer unavailable; no tickets")
            signer = None
    access = None
    if data_dir:
        try:
            from .access import AccessStore
            access = AccessStore.for_dir(data_dir)
        except Exception:
            log.exception("ROOK AUTHZ ALERT: access store unavailable; members reach nothing")
            access = _NoAccess()
    return Authorizer(store, signer, record=record, banned=banned, approvals=approvals,
                      access=access)


class _NoAccess:
    """Stands in for an access store that would not open: every member call
    is denied (owners are not affected)."""

    def evaluate(self, *a: Any, **kw: Any):
        raise RuntimeError("the access store is unavailable")

    def __getattr__(self, name: str):
        raise RuntimeError("the access store is unavailable")
