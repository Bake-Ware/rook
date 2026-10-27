"""People and what they may reach: hub roles and fine-grained grants (access v2).

Design: knowledge page ``users-access-trust-map-and-agent-sessions-bake-s-
design-decisions-2026-10-26-voi`` items 2, 3, 4, 6 and 7.

* Several people share one hub. **Owner** is a hub role and there may be
  several; everyone else is a **member**. Owners reach everything (they also
  pass :func:`rook.hub.authz.require_hub_admin`, which keeps hub settings,
  sign-in and policy owner-only whatever grants say).
* A member reaches only what a **grant** names: a band (every machine on it),
  one machine, the hub's own caps, a knowledge area, or anything; each grant
  may narrow to cap patterns and carries a tier ceiling (default ``exec``:
  ``admin`` caps need it said). A person with no grants reaches nothing.
* An API key only says which person is calling (item 7): each key belongs
  to one account (the ``tokens`` table), and a key reaches exactly what its
  person reaches, from any client. A key made before keys belonged to people
  is carried over to the hub's first owner the first time it is used.
* Every change is attributed and kept in an append-only history with the
  words of whoever asked for it.
* **Onboarding** (item 6): a new person asks for access in their own words
  (``requests``), and machines and bands that appear after the first look are
  **new resources** until an owner says who besides the owners gets them
  (``resources``). The house agent turns both into conversation
  (rook/hub/plugins/home/access_talk.py); owners also see them on the People
  page.
* Chat accounts (item 5): each person links their own Telegram / Discord
  account with a one-time code (:meth:`AccessStore.link_code`, redeemed by
  the bot with :meth:`AccessStore.redeem`); a linked account acts as its
  person and anyone else is ignored. The hub's bot is the default; a person
  may name their own bot (its token stays in the vault). Shared spaces (a
  household Discord server, a family Telegram group) are hub resources,
  granted like machines with a ``space`` grant (``ref`` = ``<platform>:<id>``).

The store is ``access.db`` beside ``policy.json`` (both hub processes open
it, as they share ``approvals.db``). :class:`rook.hub.authz.Authorizer`
calls :meth:`AccessStore.evaluate` for every band call a member makes, in
any policy mode. Accounts stay in ``enrollment.db``; :meth:`AccessStore.seed`
carries today's access over once (operators and band owners become owners,
band members get a grant on each band they were in).
"""

from __future__ import annotations

import contextvars
import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Any, Iterable

from ..core import authz as core_authz
from ..core.db import secure_file, transaction

log = logging.getLogger("rook.hub.access")

ROLES = ("owner", "member")
KINDS = ("band", "worker", "hub", "knowledge", "any", "space")
DEFAULT_TIER = "exec"
#: Hub caps a person may always call about themselves (no grant needed).
SELF_CAPS = frozenset({"users.me", "users.explain", "users.request", "users.link_code",
                       "users.links", "users.unlink", "users.chat_bot"})
KNOWLEDGE_CAPS = ("knowledge.read", "knowledge.write")
WORDS_MAX = 1000
REQUEST_STATES = ("open", "asked", "done", "declined")
RESOURCE_KINDS = ("worker", "band")
RESOURCE_STATES = ("new", "asked", "settled")
#: Who carried a person's change in (the house agent), for the history's actor.
via: contextvars.ContextVar[str] = contextvars.ContextVar("rook_access_via", default="")
#: Chat platforms a person can link an account on.
PLATFORMS = ("telegram", "discord")
#: How long a link code works, and its alphabet (no 0/O, 1/I: read aloud, typed on a phone).
CODE_TTL = 600.0
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SPACE_ID = re.compile(r"^-?[0-9]{1,24}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id TEXT PRIMARY KEY,
    role    TEXT NOT NULL CHECK(role IN ('owner','member')),
    label   TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    actor   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    seq     INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ref     TEXT NOT NULL DEFAULT '',
    doc     TEXT NOT NULL DEFAULT '{}',
    words   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS history_user ON history(user_id, seq);
CREATE TABLE IF NOT EXISTS bands (
    id       TEXT PRIMARY KEY,
    psk_hash TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tokens (
    agent_id TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    label    TEXT NOT NULL DEFAULT '',
    created  REAL NOT NULL,
    actor    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    words   TEXT NOT NULL,
    label   TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    state   TEXT NOT NULL DEFAULT 'open'
            CHECK(state IN ('open','asked','done','declined')),
    ask     TEXT NOT NULL DEFAULT '',
    decided_by TEXT,
    decided_words TEXT
);
CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id, state);
CREATE TABLE IF NOT EXISTS resources (
    kind    TEXT NOT NULL CHECK(kind IN ('worker','band')),
    ref     TEXT NOT NULL,
    label   TEXT NOT NULL DEFAULT '',
    seen    REAL NOT NULL,
    state   TEXT NOT NULL DEFAULT 'new' CHECK(state IN ('new','asked','settled')),
    ask     TEXT NOT NULL DEFAULT '',
    settled REAL,
    settled_by TEXT,
    words   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(kind, ref)
);
CREATE TABLE IF NOT EXISTS chat_links (
    platform      TEXT NOT NULL,
    platform_user TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    handle        TEXT NOT NULL DEFAULT '',
    created       REAL NOT NULL,
    PRIMARY KEY (platform, platform_user),
    UNIQUE (user_id, platform)
);
CREATE TABLE IF NOT EXISTS link_codes (
    code_hash TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL,
    platform  TEXT NOT NULL,
    expires   REAL NOT NULL,
    actor     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_bots (
    user_id  TEXT NOT NULL,
    platform TEXT NOT NULL,
    secret   TEXT NOT NULL,
    created  REAL NOT NULL,
    PRIMARY KEY (user_id, platform)
);
CREATE TABLE IF NOT EXISTS spaces (
    platform TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    created  REAL NOT NULL,
    actor    TEXT NOT NULL,
    PRIMARY KEY (platform, space_id)
);
"""

#: Words kept for a key carried over to the first owner.
CARRIED_OVER = ("carried over at the access v2 upgrade: this API key was made before keys "
                "belonged to a person, so it belongs to the hub's first owner")

#: The grants table; its kind check follows :data:`KINDS` (an older table is
#: rebuilt by :func:`_migrate_grants` when a kind is added).
_GRANTS = """
CREATE TABLE IF NOT EXISTS {name} (
    id       TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK(kind IN ({kinds})),
    ref      TEXT NOT NULL,
    caps     TEXT NOT NULL DEFAULT '["*"]',
    tier     TEXT NOT NULL DEFAULT 'exec',
    created  REAL NOT NULL,
    actor    TEXT NOT NULL,
    words    TEXT NOT NULL DEFAULT '',
    revoked  REAL,
    revoked_by TEXT,
    revoke_words TEXT
)"""


def _grants_sql(name: str = "grants") -> str:
    return _GRANTS.format(name=name, kinds=",".join(f"'{k}'" for k in KINDS))


def _migrate_grants(db) -> None:
    """Create the grants table, or rebuild one whose kind check predates a
    kind in :data:`KINDS` (SQLite cannot alter a CHECK in place)."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='grants'").fetchone()
    if row is None:
        db.execute(_grants_sql())
    elif any(f"'{k}'" not in row[0] for k in KINDS):
        db.execute("DROP TABLE IF EXISTS grants_rebuild")
        db.execute(_grants_sql("grants_rebuild"))
        new = {r[1] for r in db.execute("PRAGMA table_info(grants_rebuild)")}
        cols = ",".join(r[1] for r in db.execute("PRAGMA table_info(grants)") if r[1] in new)
        db.execute(f"INSERT INTO grants_rebuild({cols}) SELECT {cols} FROM grants")
        db.execute("DROP TABLE grants")
        db.execute("ALTER TABLE grants_rebuild RENAME TO grants")
    db.execute("CREATE INDEX IF NOT EXISTS grants_user ON grants(user_id, revoked)")


class AccessError(ValueError):
    pass


def user_of(principal: Any) -> str | None:
    """The account behind a principal, or None (the owner by password, the
    static key, system code): a signed-in dashboard account
    (``human:<user id>``) or the person an API key belongs to."""
    if getattr(principal, "kind", "") == "token":
        return str(getattr(principal, "user", "") or "") or None
    pid = str(getattr(principal, "id", "") or "")
    if getattr(principal, "kind", "") != "human" or not pid.startswith("human:"):
        return None
    uid = pid.split(":", 1)[1]
    return None if uid in ("", "dashboard") else uid


def user_of_id(pid: Any) -> str | None:
    """The account behind a principal id string, as an on-behalf-of chain
    (``Principal.via``) carries it: ``human:<id>`` -> ``<id>``, else None."""
    pid = str(pid or "")
    if not pid.startswith("human:"):
        return None
    uid = pid.split(":", 1)[1]
    return None if uid in ("", "dashboard") else uid


def restricted(principal: Any) -> bool:
    """Whether grants decide for this principal: a verified signed-in person
    who is not an owner, or a member's API key (a key whose person could not
    be read is a member with no account, and reaches nothing). Agents the hub
    runs itself (role ``agent``) are not people and are not limited here."""
    if principal is None or not getattr(principal, "verified", False) or principal.fail_open():
        return False
    if user_of(principal) is not None:
        return True
    return getattr(principal, "kind", "") == "token" and getattr(principal, "role", "") == "member"


def _norm_caps(caps: Any) -> list[str]:
    if caps in (None, "", []):
        return ["*"]
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(",")]
    out = sorted({str(c).strip() for c in caps if str(c).strip()})
    if not out:
        return ["*"]
    for c in out:
        if len(c) > 120 or any(ch.isspace() for ch in c):
            raise AccessError(f"caps: {c!r} is not a cap name or pattern")
    return out


def _words(words: Any) -> str:
    text = str(words or "").strip()
    if not text:
        raise AccessError("words: say who asked for this and in what words (it is kept)")
    return text[:WORDS_MAX]


def _grant_row(r: Any) -> dict:
    g = dict(r)
    g["caps"] = json.loads(g.get("caps") or '["*"]')
    return g


class AccessStore:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(path):
            open(path, "a").close()
        secure_file(path)
        with self.db() as db:
            db.executescript(_SCHEMA)
        with self.db() as db:
            _migrate_grants(db)
        self._bands_seen: dict[str, tuple] | None = None

    @classmethod
    def for_dir(cls, data_dir: str) -> "AccessStore":
        return cls(os.path.join(data_dir, "access.db"))

    def db(self, write: bool = True):
        return transaction(self.path, begin="IMMEDIATE" if write else None)

    @staticmethod
    def _log(db, actor: str, action: str, user_id: str, ref: str = "", doc: Any = None,
             words: str = "") -> None:
        db.execute("INSERT INTO history(ts,actor,action,user_id,ref,doc,words) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), actor, action, user_id, ref, json.dumps(doc or {}, sort_keys=True),
                    words))

    # -- people --------------------------------------------------------------
    def role(self, user_id: str) -> str | None:
        with self.db(False) as db:
            r = db.execute("SELECT role FROM members WHERE user_id=?", (user_id,)).fetchone()
        return r["role"] if r else None

    def is_owner(self, user_id: str) -> bool:
        return self.role(user_id) == "owner"

    def members(self) -> list[dict]:
        with self.db(False) as db:
            return [dict(r) for r in db.execute("SELECT * FROM members ORDER BY created")]

    def set_role(self, user_id: str, role: str, *, actor: str, words: str, label: str = "") -> dict:
        """Make someone an owner or a member. The last owner stays an owner."""
        if role not in ROLES:
            raise AccessError(f"role: one of {', '.join(ROLES)}")
        if not user_id:
            raise AccessError("user: an account id")
        words = _words(words)
        now = time.time()
        with self.db() as db:
            old = db.execute("SELECT * FROM members WHERE user_id=?", (user_id,)).fetchone()
            if old is not None and old["role"] == "owner" and role != "owner":
                owners = db.execute("SELECT count(*) FROM members WHERE role='owner'").fetchone()[0]
                if owners <= 1:
                    raise AccessError("this is the last owner: make someone else an owner first")
            if old is None:
                db.execute("INSERT INTO members(user_id,role,label,created,updated,actor) "
                           "VALUES(?,?,?,?,?,?)", (user_id, role, label[:120], now, now, actor))
            else:
                db.execute("UPDATE members SET role=?, label=CASE WHEN ?='' THEN label ELSE ? END, "
                           "updated=?, actor=? WHERE user_id=?",
                           (role, label, label[:120], now, actor, user_id))
            self._log(db, actor, "role", user_id, role,
                      {"from": old["role"] if old else None, "to": role}, words)
        return {"user": user_id, "role": role, "was": old["role"] if old else None}

    # -- grants --------------------------------------------------------------
    def grants(self, user_id: str | None = None, active: bool = True) -> list[dict]:
        q, params = "SELECT * FROM grants", []
        where = []
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if active:
            where.append("revoked IS NULL")
        if where:
            q += " WHERE " + " AND ".join(where)
        with self.db(False) as db:
            return [_grant_row(r) for r in db.execute(q + " ORDER BY created", params)]

    def grant(self, user_id: str, kind: str, ref: str = "", *, caps: Any = None,
              tier: str | None = None, actor: str, words: str, label: str = "") -> dict:
        if kind not in KINDS:
            raise AccessError(f"kind: one of {', '.join(KINDS)}")
        ref = str(ref or "").strip()
        if kind in ("hub", "any"):
            ref = ref or "*"
        elif not ref:
            raise AccessError(f"ref: which {kind} (a {'band id' if kind == 'band' else 'name'})")
        if kind == "knowledge" and ref != "*" and not ref.replace("-", "").replace("_", "").isalnum():
            raise AccessError("ref: a knowledge folder slug, or * for every area")
        if kind == "space":
            ref = self._space_ref(ref)
        t = core_authz.norm_tier(tier) if tier else DEFAULT_TIER
        if t is None:
            raise AccessError(f"tier: one of {', '.join(core_authz.TIERS)}")
        caps_l = _norm_caps(caps)
        words = _words(words)
        gid = "g_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self.db() as db:
            if db.execute("SELECT 1 FROM members WHERE user_id=?", (user_id,)).fetchone() is None:
                db.execute("INSERT INTO members(user_id,role,label,created,updated,actor) "
                           "VALUES(?,?,?,?,?,?)", (user_id, "member", label[:120], now, now, actor))
                self._log(db, actor, "role", user_id, "member", {"from": None, "to": "member"}, words)
            db.execute("INSERT INTO grants(id,user_id,kind,ref,caps,tier,created,actor,words) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (gid, user_id, kind, ref, json.dumps(caps_l), t, now, actor, words))
            row = _grant_row(db.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone())
            self._log(db, actor, "grant", user_id, gid, row, words)
            # Access given answers the person's request, and settles "who gets
            # this?" for the resource it names.
            db.execute("UPDATE requests SET state='done', decided_by=?, decided_words=?, "
                       "updated=? WHERE user_id=? AND state IN ('open','asked')",
                       (actor, words, now, user_id))
            if kind in RESOURCE_KINDS:
                db.execute("UPDATE resources SET state='settled', settled=?, settled_by=?, "
                           "words=? WHERE kind=? AND ref=? AND state!='settled'",
                           (now, actor, words, kind, ref))
        return row

    def revoke(self, grant_id: str, *, actor: str, words: str) -> dict:
        words = _words(words)
        with self.db() as db:
            r = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
            if r is None:
                raise AccessError(f"no grant {grant_id!r}")
            if r["revoked"] is not None:
                return _grant_row(r)
            db.execute("UPDATE grants SET revoked=?, revoked_by=?, revoke_words=? WHERE id=?",
                       (time.time(), actor, words, grant_id))
            row = _grant_row(db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone())
            self._log(db, actor, "revoke", r["user_id"], grant_id, row, words)
        return row

    def history(self, user_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        with self.db(False) as db:
            if user_id:
                rows = db.execute("SELECT * FROM history WHERE user_id=? ORDER BY seq DESC LIMIT ?",
                                  (user_id, limit))
            else:
                rows = db.execute("SELECT * FROM history ORDER BY seq DESC LIMIT ?", (limit,))
            return [{**dict(r), "doc": json.loads(r["doc"])} for r in rows]

    # -- API keys (item 7) -----------------------------------------------------
    def first_owner(self) -> str | None:
        with self.db(False) as db:
            r = db.execute("SELECT user_id FROM members WHERE role='owner' "
                           "ORDER BY created, rowid LIMIT 1").fetchone()
        return r["user_id"] if r else None

    def token_user(self, agent_id: str, *, label: str = "", adopt: bool = True) -> str | None:
        """The account an API key belongs to. A key with no row was made
        before keys belonged to people (minting records one): with ``adopt``
        it goes to the first owner, kept in the history. None when there is
        no owner yet (a hub signed into by password only: its key is that
        hub's owner)."""
        if not agent_id:
            return None
        with self.db(False) as db:
            r = db.execute("SELECT user_id FROM tokens WHERE agent_id=?", (agent_id,)).fetchone()
        if r is not None or not adopt:
            return r["user_id"] if r else None
        owner = self.first_owner()
        if owner is None:
            return None
        actor = "system:access-migration"
        with self.db() as db:
            r = db.execute("SELECT user_id FROM tokens WHERE agent_id=?", (agent_id,)).fetchone()
            if r is not None:  # the other hub process got there first
                return r["user_id"]
            db.execute("INSERT INTO tokens(agent_id,user_id,label,created,actor) VALUES(?,?,?,?,?)",
                       (agent_id, owner, label[:120], time.time(), actor))
            self._log(db, actor, "token", owner, agent_id, {"label": label[:120], "from": None},
                      CARRIED_OVER)
        log.info("API key %s (%s) carried over to the first owner %s", agent_id, label, owner)
        return owner

    def set_token_user(self, agent_id: str, user_id: str, *, actor: str, words: str,
                       label: str = "") -> dict:
        """Say whose an API key is (at mint, or moving it to someone else)."""
        if not agent_id:
            raise AccessError("token: a key's agent id")
        if not user_id:
            raise AccessError("user: an account id")
        words = _words(words)
        with self.db() as db:
            old = db.execute("SELECT user_id FROM tokens WHERE agent_id=?", (agent_id,)).fetchone()
            db.execute("INSERT INTO tokens(agent_id,user_id,label,created,actor) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(agent_id) DO UPDATE SET user_id=excluded.user_id, "
                       "label=CASE WHEN excluded.label='' THEN tokens.label ELSE excluded.label END, "
                       "actor=excluded.actor",
                       (agent_id, user_id, label[:120], time.time(), actor))
            self._log(db, actor, "token", user_id, agent_id,
                      {"label": label[:120], "from": old["user_id"] if old else None}, words)
        return {"token": agent_id, "user": user_id, "was": old["user_id"] if old else None}

    def token_users(self) -> dict[str, str]:
        """Every recorded key: ``{agent_id: user_id}``."""
        with self.db(False) as db:
            return {r["agent_id"]: r["user_id"] for r in db.execute("SELECT * FROM tokens")}

    # -- bands -----------------------------------------------------------------
    def sync_bands(self, bands: Iterable[dict]) -> None:
        """Record each enrolled band's current key hash (``id``, ``psk_hash``,
        ``name``), so a band grant follows its band across key changes. Cheap
        when nothing changed (the dashboard calls it every sync)."""
        seen = {str(b["id"]): (str(b.get("psk_hash") or ""), str(b.get("name") or ""))
                for b in bands if b.get("id") and b.get("psk_hash")}
        if seen == self._bands_seen:
            return
        with self.db() as db:
            for bid, (h, name) in seen.items():
                db.execute("INSERT INTO bands(id,psk_hash,name) VALUES(?,?,?) ON CONFLICT(id) "
                           "DO UPDATE SET psk_hash=excluded.psk_hash, name=excluded.name",
                           (bid, h, name))
        self._bands_seen = seen

    def band_hashes(self) -> dict[str, str]:
        with self.db(False) as db:
            return {r["id"]: r["psk_hash"] for r in db.execute("SELECT id, psk_hash FROM bands")}

    # -- onboarding: requests in a person's own words ---------------------------
    def request(self, user_id: str, words: Any, label: str = "") -> dict:
        """A person asks for access, in their own words. One open request per
        person: asking again replaces the words (and asks the owners again)."""
        if not user_id:
            raise AccessError("user: an account id")
        words = _words(words)
        now = time.time()
        with self.db() as db:
            old = db.execute("SELECT * FROM requests WHERE user_id=? AND state IN ('open','asked')",
                             (user_id,)).fetchone()
            if old is not None:
                db.execute("UPDATE requests SET words=?, label=CASE WHEN ?='' THEN label ELSE ? "
                           "END, updated=?, state='open', ask='' WHERE id=?",
                           (words, label, label[:120], now, old["id"]))
                rid = old["id"]
            else:
                rid = "r_" + uuid.uuid4().hex[:12]
                db.execute("INSERT INTO requests(id,user_id,words,label,created,updated) "
                           "VALUES(?,?,?,?,?,?)", (rid, user_id, words, label[:120], now, now))
            self._log(db, f"human:{user_id}", "request", user_id, rid, {}, words)
            return dict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())

    def requests(self, states: Iterable[str] = ("open", "asked"), user_id: str | None = None
                 ) -> list[dict]:
        states = [s for s in states if s in REQUEST_STATES]
        if not states:
            return []
        q = f"SELECT * FROM requests WHERE state IN ({','.join('?' * len(states))})"
        args: list = list(states)
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        with self.db(False) as db:
            return [dict(r) for r in db.execute(q + " ORDER BY created", args)]

    def get_request(self, rid: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def request_asked(self, rid: str, ask: str) -> None:
        """The house agent put this request to the owners as ``ask``."""
        with self.db() as db:
            db.execute("UPDATE requests SET state='asked', ask=?, updated=? WHERE id=? AND "
                       "state='open'", (ask, time.time(), rid))

    def decline(self, rid: str, *, actor: str, words: str) -> dict:
        """An owner says no to a request (kept, with their words)."""
        words = _words(words)
        with self.db() as db:
            r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if r is None:
                raise AccessError(f"no request {rid!r}")
            if r["state"] in ("open", "asked"):
                db.execute("UPDATE requests SET state='declined', decided_by=?, decided_words=?, "
                           "updated=? WHERE id=?", (actor, words, time.time(), rid))
                self._log(db, actor, "decline", r["user_id"], rid, {}, words)
            return dict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())

    # -- onboarding: "who gets this?" for new machines and bands ----------------
    def note_resources(self, found: Iterable[dict]) -> list[dict]:
        """Record the machines and bands there are now (``kind``, ``ref``,
        ``label``) and return the ones never seen before. The first look
        takes everything as it is (nobody is asked about what was already
        there); after that each new one waits for an owner's word."""
        rows = [(str(f["kind"]), str(f["ref"]), str(f.get("label") or "")[:120])
                for f in found if f.get("kind") in RESOURCE_KINDS and f.get("ref")]
        now = time.time()
        new: list[dict] = []
        with self.db() as db:
            first = db.execute("SELECT 1 FROM meta WHERE key='resources_seen'").fetchone() is None
            for kind, ref, label in rows:
                old = db.execute("SELECT label FROM resources WHERE kind=? AND ref=?",
                                 (kind, ref)).fetchone()
                if old is not None:
                    if label and old["label"] != label:
                        db.execute("UPDATE resources SET label=? WHERE kind=? AND ref=?",
                                   (label, kind, ref))
                    continue
                if first:
                    db.execute("INSERT INTO resources(kind,ref,label,seen,state,settled,settled_by,"
                               "words) VALUES(?,?,?,?,'settled',?,?,?)",
                               (kind, ref, label, now, now, "system:access",
                                "there before access prompts"))
                    continue
                db.execute("INSERT INTO resources(kind,ref,label,seen) VALUES(?,?,?,?)",
                           (kind, ref, label, now))
                new.append({"kind": kind, "ref": ref, "label": label, "seen": now,
                            "state": "new"})
            if first:
                db.execute("INSERT INTO meta(key,value) VALUES('resources_seen',?)", (str(now),))
        return new

    def resources(self, states: Iterable[str] = ("new", "asked")) -> list[dict]:
        states = [s for s in states if s in RESOURCE_STATES]
        if not states:
            return []
        with self.db(False) as db:
            return [dict(r) for r in db.execute(
                f"SELECT * FROM resources WHERE state IN ({','.join('?' * len(states))}) "
                "ORDER BY seen", states)]

    def resource(self, kind: str, ref: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM resources WHERE kind=? AND ref=?", (kind, ref)).fetchone()
        return dict(r) if r else None

    def resources_asked(self, keys: Iterable[tuple[str, str]], ask: str) -> None:
        with self.db() as db:
            for kind, ref in keys:
                db.execute("UPDATE resources SET state='asked', ask=? WHERE kind=? AND ref=? AND "
                           "state='new'", (ask, kind, ref))

    def settle(self, kind: str, ref: str, *, actor: str, words: str) -> dict:
        """An owner's word on a new resource: who gets it is decided (owners
        only, unless grants said otherwise). Kept in the history."""
        words = _words(words)
        now = time.time()
        with self.db() as db:
            r = db.execute("SELECT * FROM resources WHERE kind=? AND ref=?", (kind, ref)).fetchone()
            if r is None:
                raise AccessError(f"no new {kind} {ref!r}")
            if r["state"] != "settled":
                db.execute("UPDATE resources SET state='settled', settled=?, settled_by=?, words=? "
                           "WHERE kind=? AND ref=?", (now, actor, words, kind, ref))
                self._log(db, actor, "settle", "", f"{kind}:{ref}", {"label": r["label"]}, words)
            return dict(db.execute("SELECT * FROM resources WHERE kind=? AND ref=?",
                                   (kind, ref)).fetchone())

    def reaches(self, user_id: str, kind: str, ref: str, bands: Iterable[str] = (),
                name: str = "") -> dict | None:
        """The active grant that already gives ``user_id`` this machine
        (``bands``: the key hashes of the bands it is on) or band, whatever
        caps it narrows to; None when nothing does."""
        hashes = self.band_hashes()
        band_set = set(bands or ())
        for g in self.grants(user_id):
            if g["kind"] == "any":
                return g
            if kind == "worker":
                if g["kind"] == "worker" and (g["ref"] == ref or (
                        name and g["ref"].casefold() == name.casefold())):
                    return g
                if g["kind"] == "band" and hashes.get(g["ref"]) in band_set:
                    return g
            elif kind == "band" and g["kind"] == "band" and g["ref"] == ref:
                return g
        return None

    # -- migration -------------------------------------------------------------
    def seed(self, accounts: Any) -> bool:
        """Carry today's access over, once: operator accounts and band owners
        become owners; band members get a grant on each band they belong to.
        Accounts made afterwards start with nothing. Returns whether it ran."""
        with self.db(False) as db:
            if db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
                return False
        with accounts.db() as adb:
            users = [dict(r) for r in adb.execute("SELECT id, name, username, admin FROM users")]
            mships = [dict(r) for r in adb.execute("SELECT band_id, user_id, role FROM memberships")]
        actor, words = "system:access-migration", "carried over from accounts at the access v2 upgrade"
        now = time.time()
        with self.db() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
                return False
            for u in users:
                mine = [m for m in mships if m["user_id"] == u["id"]]
                owner = bool(u["admin"]) or any(m["role"] == "owner" for m in mine)
                label = str(u.get("name") or u.get("username") or "")[:120]
                if not owner and not mine:
                    continue  # nothing to carry over: they wait for onboarding
                if db.execute("SELECT 1 FROM members WHERE user_id=?", (u["id"],)).fetchone():
                    continue
                role = "owner" if owner else "member"
                db.execute("INSERT INTO members(user_id,role,label,created,updated,actor) "
                           "VALUES(?,?,?,?,?,?)", (u["id"], role, label, now, now, actor))
                self._log(db, actor, "role", u["id"], role, {"from": None, "to": role}, words)
                if owner:
                    continue
                for m in mine:
                    gid = "g_" + uuid.uuid4().hex[:12]
                    db.execute("INSERT INTO grants(id,user_id,kind,ref,caps,tier,created,actor,words) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (gid, u["id"], "band", m["band_id"],
                                                            '["*"]', DEFAULT_TIER, now, actor, words))
                    self._log(db, actor, "grant", u["id"], gid,
                              {"kind": "band", "ref": m["band_id"]}, words)
            db.execute("INSERT INTO meta(key,value) VALUES('seeded',?)", (str(now),))
        return True

    # -- decisions -------------------------------------------------------------
    def evaluate(self, user_id: str | None, cap: str, tier: str, target: Any,
                 bands: Iterable[str] = ()) -> tuple[dict | None, str]:
        """``(grant, reason)``: the active grant that lets ``user_id`` make
        this call, or ``None`` and why not. ``bands`` are the key hashes of
        the bands the target was seen on; ``target`` a policy Target."""
        if not user_id:
            return None, "this API key belongs to no one this hub can read: an owner checks it on Tokens"
        is_hub = bool(getattr(target, "is_rook", False))
        if is_hub and cap in SELF_CAPS:
            return {"id": "self", "kind": "hub", "ref": "*"}, "about yourself"
        grants = self.grants(user_id)
        if not grants:
            return None, "no access yet: an owner gives it (Account & access, or ask the house agent)"
        rank = core_authz.tier_rank(tier)
        tid = str(getattr(target, "id", "") or "")
        tname = str(getattr(target, "name", "") or "").casefold()
        band_set = set(bands or ())
        hashes = self.band_hashes() if any(g["kind"] == "band" for g in grants) else {}
        over_tier = False
        for g in grants:
            if not any(fnmatch.fnmatchcase(cap, p) for p in g["caps"]):
                continue
            kind, ref = g["kind"], g["ref"]
            if kind == "any":
                hit = True
            elif kind == "hub":
                hit = is_hub
            elif kind == "knowledge":
                hit = is_hub and cap in KNOWLEDGE_CAPS
            elif kind == "space":
                hit = False  # a chat space, not a machine: see may_use_space
            elif kind == "worker":
                hit = bool(tid) and not is_hub and (ref == tid or ref.casefold() == tname)
            else:  # band
                h = hashes.get(ref)
                hit = bool(tid) and not is_hub and bool(h) and h in band_set
            if not hit:
                continue
            if rank > core_authz.tier_rank(g["tier"]):
                over_tier = True
                continue
            return g, f"grant {g['id']}"
        if over_tier:
            return None, f"{cap} is a {tier} cap, above what your grant allows"
        where = getattr(target, "name", "") or tid or "every machine"
        return None, f"no grant covers {cap} on {'the hub' if is_hub else where}"

    # -- chat accounts (item 5) ------------------------------------------------
    def link_code(self, user_id: str, platform: str, *, actor: str) -> dict:
        """A fresh one-time code that links ``user_id``'s account on
        ``platform`` when sent to the bot. It replaces any earlier unused code
        for the same person and platform, works once and expires after
        :data:`CODE_TTL` seconds. Only its hash is stored."""
        platform = _platform(platform)
        if not user_id:
            raise AccessError("user: an account id")
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        code = f"{raw[:4]}-{raw[4:]}"
        now = time.time()
        with self.db() as db:
            db.execute("DELETE FROM link_codes WHERE expires<? OR (user_id=? AND platform=?)",
                       (now, user_id, platform))
            db.execute("INSERT INTO link_codes(code_hash,user_id,platform,expires,actor) "
                       "VALUES(?,?,?,?,?)", (_code_hash(code), user_id, platform,
                                            now + CODE_TTL, actor))
            self._log(db, actor, "link_code", user_id, platform, {"expires": now + CODE_TTL})
        return {"code": code, "platform": platform, "expires": now + CODE_TTL}

    def redeem(self, platform: str, code: str, platform_user: str, handle: str = "", *,
               bot_owner: str | None = None) -> str | None:
        """Link ``platform_user`` to the person whose code this is; returns
        their account id, or None for an unknown, used or expired code. On a
        person's own bot (``bot_owner``) only that person's codes work. The
        account replaces whatever that person had linked on the platform
        before, and a platform account belongs to one person at a time."""
        platform = _platform(platform)
        platform_user = str(platform_user or "").strip()
        if not platform_user:
            return None
        h, now = _code_hash(code), time.time()
        with self.db() as db:
            r = db.execute("SELECT * FROM link_codes WHERE code_hash=?", (h,)).fetchone()
            if r is None or r["platform"] != platform or r["expires"] < now:
                return None
            uid = r["user_id"]
            if bot_owner is not None and uid != bot_owner:
                return None
            db.execute("DELETE FROM link_codes WHERE code_hash=?", (h,))
            old = db.execute("SELECT user_id FROM chat_links WHERE platform=? AND platform_user=?",
                             (platform, platform_user)).fetchone()
            db.execute("DELETE FROM chat_links WHERE platform=? AND (user_id=? OR platform_user=?)",
                       (platform, uid, platform_user))
            db.execute("INSERT INTO chat_links(platform,platform_user,user_id,handle,created) "
                       "VALUES(?,?,?,?,?)", (platform, platform_user, uid, str(handle)[:120], now))
            doc = {"platform_user": platform_user, "handle": str(handle)[:120]}
            if old is not None and old["user_id"] != uid:
                doc["was"] = old["user_id"]
            self._log(db, f"{platform}:{platform_user}", "link", uid, platform, doc,
                      "sent the one-time link code to the bot")
        return uid

    def linked_user(self, platform: str, platform_user: str) -> str | None:
        """The account a platform user id is linked to, or None (a stranger)."""
        with self.db(False) as db:
            r = db.execute("SELECT user_id FROM chat_links WHERE platform=? AND platform_user=?",
                           (platform, str(platform_user))).fetchone()
        return r["user_id"] if r else None

    def links(self, user_id: str | None = None, platform: str | None = None) -> list[dict]:
        q, params, where = "SELECT * FROM chat_links", [], []
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if platform:
            where.append("platform=?")
            params.append(platform)
        if where:
            q += " WHERE " + " AND ".join(where)
        with self.db(False) as db:
            return [dict(r) for r in db.execute(q + " ORDER BY created", params)]

    def unlink(self, user_id: str, platform: str, *, actor: str, words: str) -> bool:
        platform = _platform(platform)
        words = _words(words)
        with self.db() as db:
            gone = db.execute("DELETE FROM chat_links WHERE user_id=? AND platform=?",
                              (user_id, platform)).rowcount
            db.execute("DELETE FROM link_codes WHERE user_id=? AND platform=?", (user_id, platform))
            if gone:
                self._log(db, actor, "unlink", user_id, platform, {}, words)
        return bool(gone)

    def set_bot(self, user_id: str, platform: str, secret: str, *, actor: str,
                words: str, owner: bool = False) -> dict:
        """Use the person's own bot on ``platform`` instead of the hub's.
        ``secret`` names the vault secret holding its token (never the token);
        members name one under ``user.<their id>.``. An empty ``secret`` goes
        back to the hub's bot."""
        platform = _platform(platform)
        words = _words(words)
        secret = str(secret or "").strip()
        if secret.startswith("{{secret:"):
            secret = secret[len("{{secret:"):].split("|", 1)[0].rstrip("}")
        with self.db() as db:
            if not secret:
                gone = db.execute("DELETE FROM chat_bots WHERE user_id=? AND platform=?",
                                  (user_id, platform)).rowcount
                if gone:
                    self._log(db, actor, "bot", user_id, platform, {"secret": None}, words)
                return {"user": user_id, "platform": platform, "bot": "hub"}
            if not _SECRET_NAME.match(secret):
                raise AccessError("secret: the vault secret's name (lowercase, digits, . _ -), "
                                  "never the token itself")
            if not owner and not secret.startswith(f"user.{user_id.lower()}."):
                raise AccessError(f"secret: keep your bot's token in the vault under "
                                  f"user.{user_id.lower()}.<name> (yours alone)")
            db.execute("INSERT INTO chat_bots(user_id,platform,secret,created) VALUES(?,?,?,?) "
                       "ON CONFLICT(user_id,platform) DO UPDATE SET secret=excluded.secret, "
                       "created=excluded.created", (user_id, platform, secret, time.time()))
            self._log(db, actor, "bot", user_id, platform, {"secret": secret}, words)
        return {"user": user_id, "platform": platform, "bot": "own", "secret": secret}

    def bots(self, platform: str | None = None, user_id: str | None = None) -> list[dict]:
        q, params, where = "SELECT * FROM chat_bots", [], []
        if platform:
            where.append("platform=?")
            params.append(platform)
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        with self.db(False) as db:
            return [dict(r) for r in db.execute(q + " ORDER BY created", params)]

    # -- shared chat spaces ------------------------------------------------------
    def _space_ref(self, ref: str) -> str:
        platform, _, sid = str(ref or "").partition(":")
        platform = platform.strip().lower()
        with self.db(False) as db:
            r = db.execute("SELECT 1 FROM spaces WHERE platform=? AND space_id=?",
                           (platform, sid.strip())).fetchone()
        if r is None:
            raise AccessError("ref: a shared space as <platform>:<id>, added first with "
                              "users.space (users.spaces lists them)")
        return f"{platform}:{sid.strip()}"

    def add_space(self, platform: str, space_id: str, name: str, *, actor: str,
                  words: str) -> dict:
        """Make a Discord server or a Telegram group a hub resource people can
        be granted (``kind=space``)."""
        platform = _platform(platform)
        space_id = str(space_id or "").strip()
        if not _SPACE_ID.match(space_id):
            raise AccessError("space: the Discord server id or the Telegram group's chat id")
        words = _words(words)
        name = str(name or "").strip()[:120]
        with self.db() as db:
            db.execute("INSERT INTO spaces(platform,space_id,name,created,actor) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(platform,space_id) DO UPDATE SET name=excluded.name",
                       (platform, space_id, name, time.time(), actor))
            self._log(db, actor, "space", "*", f"{platform}:{space_id}", {"name": name}, words)
        return {"space": f"{platform}:{space_id}", "name": name}

    def remove_space(self, platform: str, space_id: str, *, actor: str, words: str) -> dict:
        """Stop treating a space as a hub resource; its grants are revoked."""
        platform = _platform(platform)
        ref = f"{platform}:{str(space_id).strip()}"
        words = _words(words)
        with self.db() as db:
            gone = db.execute("DELETE FROM spaces WHERE platform=? AND space_id=?",
                              (platform, str(space_id).strip())).rowcount
            ids = [r["id"] for r in db.execute("SELECT id FROM grants WHERE kind='space' AND "
                                               "ref=? AND revoked IS NULL", (ref,))]
            db.execute("UPDATE grants SET revoked=?, revoked_by=?, revoke_words=? WHERE kind='space' "
                       "AND ref=? AND revoked IS NULL", (time.time(), actor, words, ref))
            if gone:
                self._log(db, actor, "space_removed", "*", ref, {"revoked": ids}, words)
        return {"space": ref, "removed": bool(gone), "revoked": ids}

    def spaces(self, platform: str | None = None) -> list[dict]:
        with self.db(False) as db:
            if platform:
                rows = db.execute("SELECT * FROM spaces WHERE platform=? ORDER BY created",
                                  (platform,))
            else:
                rows = db.execute("SELECT * FROM spaces ORDER BY created")
            return [dict(r) for r in rows]

    def space(self, platform: str, space_id: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM spaces WHERE platform=? AND space_id=?",
                           (platform, str(space_id))).fetchone()
        return dict(r) if r else None

    def may_use_space(self, user_id: str, platform: str, space_id: str) -> bool:
        """Whether a person may speak through the hub in a shared space:
        owners always, members with a ``space`` grant on it."""
        if self.is_owner(user_id):
            return True
        ref = f"{platform}:{space_id}"
        return any(g["kind"] == "space" and g["ref"] == ref for g in self.grants(user_id))

    def knowledge_areas(self, user_id: str, write: bool = False) -> set[str] | None:
        """Folder slugs a member may read (or write) in knowledge; ``{"*"}``
        for every area; ``None`` when nothing limits them (hub or any grant
        covering the knowledge caps)."""
        need = core_authz.tier_rank("write" if write else "read")
        cap = KNOWLEDGE_CAPS[1] if write else KNOWLEDGE_CAPS[0]
        areas: set[str] = set()
        for g in self.grants(user_id):
            if core_authz.tier_rank(g["tier"]) < need:
                continue
            if not any(fnmatch.fnmatchcase(cap, p) for p in g["caps"]):
                continue
            if g["kind"] in ("hub", "any"):
                return None
            if g["kind"] == "knowledge":
                areas.add(g["ref"])
        return areas


def _platform(platform: str) -> str:
    p = str(platform or "").strip().lower()
    if p not in PLATFORMS:
        raise AccessError(f"platform: one of {', '.join(PLATFORMS)}")
    return p


def _code_hash(code: str) -> str:
    norm = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return hashlib.sha256(("rook-link:" + norm).encode()).hexdigest()


def store_for(data_dir: str | None) -> AccessStore | None:
    if not data_dir:
        return None
    return AccessStore.for_dir(data_dir)
