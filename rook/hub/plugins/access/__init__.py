"""``users.*``: who uses this hub and what each person may reach (access v2).

The store and the rules are :mod:`rook.hub.access`; the band client's
authorizer enforces them on every call. These caps read and change it:

* ``users.me`` / ``users.explain`` / ``users.request``: anyone, about
  themselves (no grant needed); a request is a new person asking for access
  in their own words;
* ``users.list``, ``users.history``, ``users.role``, ``users.grant``,
  ``users.revoke``, ``users.settle``: owners (signed in or by their API keys,
  and in-process hub code) only, whatever the policy mode. Every change keeps
  the asker's words. ``users.list`` also says what waits on the owners:
  requests, people with no access yet, and new machines and bands nobody has
  said who gets.
* ``users.token``: whose an API key is. A key reaches exactly what its
  person reaches, from any client (item 7).
* Chat accounts (design item 5): ``users.link_code`` / ``users.links`` /
  ``users.unlink`` / ``users.chat_bot`` for your own Telegram and Discord
  accounts (owners may act for someone else); ``users.space`` /
  ``users.spaces`` make a shared Discord server or Telegram group a hub
  resource that ``users.grant kind=space`` gives people.
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
             "An API key reaches exactly what the person it belongs to reaches, from any "
             "client; owners move a key with `users.token`. "
             "Change access only when a person asks you to in conversation. A new person "
             "asks for access with `users.request` (their words); owners see what waits in "
             "`users.list` and close a new machine or band as owners-only with "
             "`users.settle`. "
             "Telegram/Discord: `users.link_code platform=telegram` gives a one-time code the "
             "person sends to the bot (`/link CODE`, Discord `!link CODE`); only linked accounts "
             "are heard, each as its person. `users.chat_bot` names a vault secret with the "
             "person's own bot token (never the token). Shared servers/groups: `users.space`, "
             "then `users.grant kind=space ref=<platform>:<id>`.\n")

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

    def _account(self, user: str | None, also: tuple = ()) -> str:
        """The account id ``user`` names: an id, or one account's name,
        username or email. An unknown user is refused rather than given a code
        or a grant nobody can use. ``also``: ids accepted besides accounts and
        members (a stale link can still be removed). A node that cannot see
        the accounts takes the id as given."""
        user = str(user or "").strip()
        accounts = self._accounts()
        if not user or not accounts:
            return user
        known = {a["id"] for a in accounts} | {m["user_id"] for m in self._store().members()}
        if user in known or user in also:
            return user
        key = user.casefold()
        hits = {a["id"] for a in accounts
                if key in {str(a.get(f) or "").casefold() for f in ("name", "username", "email")}}
        if len(hits) == 1:
            return hits.pop()
        if hits:
            raise ValueError(f"user: {len(hits)} accounts are called {user!r}; pass the "
                             "account id (users.list lists them)")
        raise ValueError(f"user: no account {user!r} (users.list lists them)")

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
        """Your own role and grants, in plain words. Through an API key: the
        person the key belongs to (a key reaches what they reach)."""
        p = current_principal.get()
        uid = _access.user_of(p)
        if uid is None:
            who = p.id if p is not None else "in-process hub code"
            if p is not None and p.fail_open():
                return {"user": None, "principal": who, "role": "owner",
                        "plain": "The hub's own owner: reaches everything on this hub."}
            return {"user": None, "principal": who,
                    "plain": "Not a person this hub knows: grants do not apply to this caller."}
        out = self._describe(self._store(), uid, admin=p.fail_open())
        if getattr(p, "kind", "") == "token":
            out["key"] = p.id
            out["plain"] = f"This API key belongs to {uid}. " + out["plain"]
        return out

    @capability("explain", risk="read", scope="multi-user")
    def explain(self, cap: str, worker: str | None = None, user: str | None = None) -> dict:
        """Whether a person (default: you) may call ``cap`` on ``worker``, and
        which grant decides. Asking about someone else is for owners."""
        store = self._store()
        uid = self._person(user, "users.explain") if user else self._me()
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
        keys = store.token_users()
        labels = {k.get("agent_id"): k.get("name") or "" for k in self._keys()}
        out = []
        for uid in list(dict.fromkeys([*members, *accounts])):
            a, m = accounts.get(uid, {}), members.get(uid, {})
            row = self._describe(store, uid, admin=bool(a.get("admin")))
            row["name"] = a.get("name") or a.get("username") or m.get("label") or uid
            if a.get("email"):
                row["email"] = a["email"]
            if a.get("admin"):
                row["operator"] = True
            mine = [k for k, u in keys.items() if u == uid]
            if mine:
                row["keys"] = [{"agent_id": k, "label": labels.get(k, "")} for k in mine]
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
        return self._store().set_role(self._account(user), role, actor=_actor(), words=words)

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
        return {"grant": self._store().grant(self._account(user), kind, ref, caps=caps, tier=tier,
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

    def _keys(self) -> list[dict]:
        tokens = getattr(self._node, "tokens", None)
        try:
            return list(tokens.list_api_tokens()) if tokens is not None else []
        except Exception:  # noqa: BLE001
            log.warning("could not list API keys", exc_info=True)
            return []

    @capability("token", risk="admin", scope="multi-user")
    def token(self, token: str, user: str, words: str) -> dict:
        """Say whose an API key is: it then reaches exactly what that person
        reaches, from any client. ``token``: the key's agent id
        (``agent_…``) or its label; ``user``: an account id; ``words``: the
        person's own words asking for it (kept). Owners only."""
        _gate("users.token")
        ref = str(token or "").strip()
        ref = ref[6:] if ref.startswith("token:") else ref
        hits = [k for k in self._keys() if ref and ref in (k.get("agent_id"), k.get("id"))]
        if not hits:
            hits = [k for k in self._keys() if ref and (k.get("name") or "").casefold() == ref.casefold()]
        if len({k.get("agent_id") for k in hits}) > 1:
            raise ValueError(f"token: {len(hits)} keys are labelled {ref!r}; pass the agent id")
        if not hits and not ref.startswith("agent_"):
            raise ValueError(f"token: no API key {ref!r}")
        aid = hits[0]["agent_id"] if hits else ref
        user = self._account(user)
        return self._store().set_token_user(aid, user, actor=_actor(), words=words,
                                            label=hits[0].get("name") or "" if hits else "")

    # -- chat accounts ---------------------------------------------------------
    def _person(self, user: str | None, what: str, also: tuple = ()) -> str:
        """``user`` when an owner names someone (an account id, or a unique
        name), else the caller (a person)."""
        me = self._me()
        if user and user != me:
            try:
                uid, bad = self._account(user, also), None
            except ValueError as e:
                uid, bad = None, e
            if uid != me:
                _gate(f"{what} for someone else")   # members learn nothing about others
            if bad is not None:
                raise bad
            return uid
        if me is None:
            raise ValueError("this is about a signed-in person: sign in on the dashboard, "
                             "or an owner passes user=")
        return me

    def _linked(self) -> tuple:
        return tuple({l["user_id"] for l in self._store().links()})

    def _chat_plugins(self) -> list:
        from ...integrations import ChatIntegration
        host = getattr(self._node, "host", None)
        return [p for p in (getattr(host, "plugins", None) or []) if isinstance(p, ChatIntegration)]

    def _bot_name(self, platform: str, user_id: str) -> str:
        for p in self._chat_plugins():
            if p.PLATFORM == platform:
                return p.bot_label(user_id)
        return ""

    @capability("link_code", risk="write", scope="multi-user")
    def link_code(self, platform: str, user: str | None = None) -> dict:
        """A one-time code that links your Telegram or Discord account.

        Send ``/link CODE`` (Discord: ``!link CODE``) to the hub's bot, or to
        your own bot if you set one (``users.chat_bot``), within ten minutes.
        Afterwards messages from that account act as you; unlinked accounts
        are ignored. ``user``: an owner making a code for someone else."""
        uid = self._person(user, "users.link_code")
        out = self._store().link_code(uid, platform, actor=_actor())
        prefix = "!" if out["platform"] == "discord" else "/"
        bot = self._bot_name(out["platform"], uid)
        out["send"] = f"{prefix}link {out['code']}"
        out["plain"] = (f"Send {out['send']} to {bot or 'the ' + out['platform'] + ' bot'} "
                        f"within 10 minutes (a direct message is best). It works once.")
        if not self._chat_plugins() or not any(p.PLATFORM == out["platform"]
                                               for p in self._chat_plugins()):
            out["note"] = f"the {out['platform']} integration is not running on this hub yet"
        return out

    @capability("links", risk="read", scope="multi-user")
    def links(self, user: str | None = None) -> dict:
        """Your linked chat accounts and which bot speaks for you on each
        platform (the hub's, or your own). Owners may ask about someone else
        (``user``) or everyone (``user="*"``)."""
        store = self._store()
        if user == "*":
            _gate("users.links for everyone")
            return {"links": store.links(), "bots": store.bots()}
        uid = self._person(user, "users.links", self._linked())
        return {"user": uid, "links": store.links(uid),
                "bots": {b["platform"]: b["secret"] for b in store.bots(user_id=uid)}}

    @capability("unlink", risk="write", scope="multi-user")
    def unlink(self, platform: str, words: str, user: str | None = None) -> dict:
        """Forget your linked Telegram or Discord account: its messages are
        ignored again. ``words``: who asked, in their words (kept)."""
        uid = self._person(user, "users.unlink", self._linked())
        return {"user": uid, "platform": platform,
                "unlinked": self._store().unlink(uid, platform, actor=_actor(), words=words)}

    @capability("chat_bot", risk="write", scope="self")
    def chat_bot(self, platform: str, words: str, secret: str = "") -> dict:
        """Use your own Telegram/Discord bot instead of the hub's.

        ``secret`` is the NAME of the vault secret holding the bot token
        (members: under ``user.<your id>.``); the token itself never goes
        through this cap. Empty ``secret``: back to the hub's bot. Your
        linked account then talks to your bot, and your notifications come
        from it. ``words``: your words asking for it (kept)."""
        uid = self._person(None, "users.chat_bot")
        p = current_principal.get()
        out = self._store().set_bot(uid, platform, secret, actor=_actor(), words=words,
                                    owner=bool(p is not None and p.fail_open()))
        for plugin in self._chat_plugins():
            if plugin.PLATFORM == out["platform"]:
                plugin.reload_bots()
        return out

    @capability("spaces", risk="read", scope="multi-user")
    def spaces(self) -> dict:
        """Shared chat spaces (Discord servers, Telegram groups) this hub
        treats as resources, and who holds a grant on each."""
        store = self._store()
        held: dict[str, list[str]] = {}
        for g in store.grants():
            if g["kind"] == "space":
                held.setdefault(g["ref"], []).append(g["user_id"])
        return {"spaces": [{**s, "ref": f"{s['platform']}:{s['space_id']}",
                            "granted": held.get(f"{s['platform']}:{s['space_id']}", [])}
                           for s in store.spaces()]}

    @capability("space", risk="admin", scope="multi-user")
    def space(self, platform: str, space: str, words: str, name: str = "",
              remove: bool = False) -> dict:
        """Add (or ``remove``) a shared space: a Discord server id or a
        Telegram group chat id. People then need ``users.grant kind=space
        ref=<platform>:<id>`` to be heard there; removing revokes those
        grants. Owners only; ``words`` kept."""
        _gate("users.space")
        store = self._store()
        if remove:
            return store.remove_space(platform, space, actor=_actor(), words=words)
        return store.add_space(platform, space, name, actor=_actor(), words=words)


def _plain(g: dict) -> str:
    caps = "" if g["caps"] == ["*"] else f" ({', '.join(g['caps'])})"
    what = {"band": f"band {g['ref']}", "worker": f"machine {g['ref']}", "hub": "the hub",
            "knowledge": "all knowledge" if g["ref"] == "*" else f"knowledge folder {g['ref']}",
            "any": "everything", "space": f"chat space {g['ref']}"}[g["kind"]]
    return f"{what}{caps} up to {g['tier']}"


PLUGIN = Access
