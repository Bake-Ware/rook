"""``users.*``: who uses this hub and what each person may reach (access v2).

The store and the rules are :mod:`rook.hub.access`; the band client's
authorizer enforces them on every call. These caps read and change it:

* ``users.me`` / ``users.explain`` / ``users.request``: anyone, about
  themselves (no grant needed); a request is a new person asking for access
  in their own words;
* ``users.list``, ``users.history``, ``users.role``, ``users.grant``,
  ``users.revoke``, ``users.settle``: owners (and operator tokens, in-process
  hub code) only, whatever the policy mode. Every change keeps the asker's
  words. ``users.list`` also says what waits on the owners: requests, people
  with no access yet, and new machines and bands nobody has said who gets.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from ....core.plugin import Plugin, capability, place
from ... import access as _access
from ...authz import current_principal, require_hub_admin

log = logging.getLogger("rook.hub.plugins.access")


def _actor() -> str:
    p = current_principal.get()
    if p is None:
        who = "system:hub"
    else:
        who = f"{p.id} ({p.label})" if p.label and p.label != p.id else p.id
    carried = _access.via.get()
    return f"{who} via {carried}" if carried else who


def _gate(what: str) -> None:
    refusal = require_hub_admin(what)
    if refusal:
        raise PermissionError(refusal.replace("band owners", "hub owners"))


class Access(Plugin):
    NAMESPACE = "users"
    NAME = "access"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### users\n"
             "People on this hub are owners (reach everything) or members (reach only what a "
             "grant names: a band, a machine, the hub's caps, a knowledge folder, or anything; "
             "optional cap patterns and a tier ceiling, default exec). New people reach "
             "nothing. `users.me` shows your own access; `users.explain` whether a call would "
             "be allowed. Owners change access with `users.grant` / `users.revoke` / "
             "`users.role`, always passing `words`: the person's own words asking for it. "
             "Change access only when a person asks you to in conversation. A new person "
             "asks for access with `users.request` (their words); owners see what waits in "
             "`users.list` and close a new machine or band as owners-only with "
             "`users.settle`.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None

    def bind_host(self, node) -> None:
        self._node = node

    # -- plumbing ------------------------------------------------------------
    def _authz(self):
        return getattr(getattr(self._node, "client", None), "authz", None)

    def _store(self) -> _access.AccessStore:
        store = getattr(self._authz(), "access", None)
        if not isinstance(store, _access.AccessStore):
            raise RuntimeError("access is not configured on this hub (no data directory)")
        return store

    def _accounts(self) -> list[dict]:
        """Accounts from enrollment.db, read-only, when this node can see it."""
        path = os.environ.get("ROOK_ENROLLMENT_DB")
        if not path:
            try:
                from ....remote import setup_store
                path = str(setup_store.setup_path().with_name("enrollment.db"))
            except Exception:  # noqa: BLE001
                return []
        if not os.path.exists(path):
            return []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in con.execute(
                    "SELECT id, name, username, email, admin FROM users")]
            finally:
                con.close()
        except sqlite3.Error:
            log.warning("could not read accounts from %s", path, exc_info=True)
            return []

    def _target(self, worker: str | None):
        from ...authz import target_from_entry
        node = self._node
        if not worker:
            return None, target_from_entry(None, None)
        if worker.lower() == "rook" and node is not None:
            return node.worker_id, target_from_entry(node.worker_id, node.entry(), local=True)
        roster = getattr(getattr(node, "client", None), "workers", {}) or {}
        if worker in roster:
            return worker, target_from_entry(worker, roster[worker])
        named = [w for w, e in roster.items() if (e.get("name") or "").lower() == worker.lower()]
        if len(named) == 1:
            return named[0], target_from_entry(named[0], roster[named[0]])
        return worker, target_from_entry(worker, {"name": worker})

    def _me(self) -> str | None:
        return _access.user_of(current_principal.get())

    def _describe(self, store: _access.AccessStore, user_id: str, admin: bool = False) -> dict:
        role = "owner" if admin else (store.role(user_id) or "new")
        grants = [] if role == "owner" else store.grants(user_id)
        if role == "owner":
            plain = "Owner: reaches everything on this hub, including hub settings."
        elif not grants:
            plain = "Reaches nothing yet: an owner gives access."
        else:
            plain = "Reaches only: " + "; ".join(_plain(g) for g in grants) + "."
        return {"user": user_id, "role": role, "grants": grants, "plain": plain}

    # -- caps ----------------------------------------------------------------
    @capability("me", risk="read", scope="self")
    def me(self) -> dict:
        """Your own role and grants, in plain words.

        For a signed-in person; an agent token is not a person here yet."""
        p = current_principal.get()
        uid = _access.user_of(p)
        if uid is None:
            who = p.id if p is not None else "in-process hub code"
            return {"user": None, "principal": who,
                    "plain": "Not a signed-in person: grants do not apply to this caller."}
        return self._describe(self._store(), uid, admin=p.fail_open())

    @capability("explain", risk="read", scope="multi-user")
    def explain(self, cap: str, worker: str | None = None, user: str | None = None) -> dict:
        """Whether a person (default: you) may call ``cap`` on ``worker``, and
        which grant decides. Asking about someone else is for owners."""
        store = self._store()
        uid = user or self._me()
        if not uid:
            raise ValueError("user: an account id (users.list lists them)")
        if uid != self._me():
            _gate("users.explain for someone else")
        if store.is_owner(uid):
            return {"user": uid, "allowed": True, "why": "owner"}
        tid, target = self._target(worker)
        from ....core.authz import effective_tier
        tier = effective_tier(cap, (target.tiers or {}).get(cap))
        authz = self._authz()
        bands = authz.bands_of(tid) if authz is not None else set()
        grant, why = store.evaluate(uid, cap, tier, target, bands)
        return {"user": uid, "cap": cap, "worker": worker or None, "tier": tier,
                "allowed": grant is not None, "why": why,
                "grant": grant if grant and grant.get("id") != "self" else None}

    @capability("list", risk="read", scope="multi-user")
    def list(self) -> dict:
        """Everyone with an account or access: role, grants, in plain words.
        People with no row yet are ``new`` (they reach nothing). Owners only."""
        _gate("users.list")
        store = self._store()
        accounts = {a["id"]: a for a in self._accounts()}
        members = {m["user_id"]: m for m in store.members()}
        out = []
        for uid in list(dict.fromkeys([*members, *accounts])):
            a, m = accounts.get(uid, {}), members.get(uid, {})
            row = self._describe(store, uid, admin=bool(a.get("admin")))
            row["name"] = a.get("name") or a.get("username") or m.get("label") or uid
            if a.get("email"):
                row["email"] = a["email"]
            if a.get("admin"):
                row["operator"] = True
            out.append(row)
        return {"users": out, "owners": sum(1 for r in out if r["role"] == "owner"),
                "waiting": self._waiting(store, {r["user"]: r for r in out})}

    def _waiting(self, store: _access.AccessStore, people: dict) -> dict:
        """What waits on the owners, in plain words."""
        def name(uid: str, label: str = "") -> str:
            return str((people.get(uid) or {}).get("name") or label or uid)
        reqs = [{"id": r["id"], "user": r["user_id"], "name": name(r["user_id"], r["label"]),
                 "words": r["words"], "state": r["state"], "ask": r["ask"],
                 "created": r["created"]} for r in store.requests()]
        asked = {r["user"] for r in reqs}
        new = [{"user": uid, "name": p["name"]} for uid, p in people.items()
               if p["role"] in ("new", "member") and not p["grants"] and uid not in asked]
        res = [{"key": f"{r['kind']}:{r['ref']}", "kind": r["kind"], "ref": r["ref"],
                "label": r["label"] or r["ref"], "state": r["state"], "ask": r["ask"],
                "seen": r["seen"]} for r in store.resources()]
        return {"requests": reqs, "no_access": new, "resources": res}

    @capability("history", risk="read", scope="multi-user")
    def history(self, user: str | None = None, limit: int = 50) -> dict:
        """Who changed whose access, when, and in whose words (newest first). Owners only."""
        _gate("users.history")
        return {"history": self._store().history(user, limit)}

    @capability("role", risk="admin", scope="multi-user")
    def role(self, user: str, role: str, words: str) -> dict:
        """Make someone an ``owner`` or a ``member``. ``words``: the person's
        own words asking for it (kept). The last owner stays an owner. Owners only."""
        _gate("users.role")
        return self._store().set_role(user, role, actor=_actor(), words=words)

    @capability("grant", risk="admin", scope="multi-user")
    def grant(self, user: str, kind: str, words: str, ref: str = "", caps: Any = None,
              tier: str | None = None) -> dict:
        """Give a member access. ``kind`` band (``ref`` = band id) | worker
        (name or id) | hub (the hub's own caps) | knowledge (``ref`` = folder
        slug or ``*``) | any; ``caps``: patterns to narrow it (default all);
        ``tier``: highest risk allowed (read|write|exec|admin, default exec).
        ``words``: the person's own words asking for it. Owners only; hub
        settings, sign-in and policy stay owner-only whatever a grant says."""
        _gate("users.grant")
        return {"grant": self._store().grant(user, kind, ref, caps=caps, tier=tier,
                                             actor=_actor(), words=words)}

    @capability("revoke", risk="admin", scope="multi-user")
    def revoke(self, grant: str, words: str) -> dict:
        """Take a grant away (kept in the history with ``words``). Owners only."""
        _gate("users.revoke")
        return {"grant": self._store().revoke(grant, actor=_actor(), words=words)}

    @capability("request", risk="write", scope="self")
    def request(self, words: str) -> dict:
        """Ask the hub's owners for access, in your own words ("the living
        room PC and the recipes folder"). New people reach nothing until an
        owner says what they get; the house agent puts your words to them."""
        uid = self._me()
        if uid is None:
            raise PermissionError("users.request is for a signed-in person asking for themselves")
        store = self._store()
        if store.is_owner(uid) or (current_principal.get() is not None
                                   and current_principal.get().fail_open()):
            return {"user": uid, "plain": "You are an owner: you already reach everything."}
        p = current_principal.get()
        row = store.request(uid, words, label=str(getattr(p, "label", "") or ""))
        return {"request": row["id"], "user": uid, "state": row["state"],
                "plain": "Asked. An owner decides what you get; until then you reach "
                         + ("only what you have now." if store.grants(uid) else "nothing.")}

    @capability("settle", risk="admin", scope="multi-user")
    def settle(self, what: str, words: str) -> dict:
        """Close what waits on you, with your words: a new machine or band
        (``worker:<id>`` / ``band:<id>``, from ``users.list`` waiting) stays
        with the owners (and whoever grants already reach); a request
        (``r_...``) is declined. Owners only."""
        _gate("users.settle")
        store = self._store()
        what = str(what or "").strip()
        if what.startswith("r_"):
            return {"request": store.decline(what, actor=_actor(), words=words)}
        kind, _, ref = what.partition(":")
        if kind not in _access.RESOURCE_KINDS or not ref:
            raise ValueError("what: worker:<id>, band:<id> or a request id (users.list waiting)")
        return {"resource": store.settle(kind, ref, actor=_actor(), words=words)}


def _plain(g: dict) -> str:
    caps = "" if g["caps"] == ["*"] else f" ({', '.join(g['caps'])})"
    what = {"band": f"band {g['ref']}", "worker": f"machine {g['ref']}", "hub": "the hub",
            "knowledge": "all knowledge" if g["ref"] == "*" else f"knowledge folder {g['ref']}",
            "any": "everything"}[g["kind"]]
    return f"{what}{caps} up to {g['tier']}"


PLUGIN = Access
