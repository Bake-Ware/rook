"""People and what they may reach: hub roles and fine-grained grants (access v2).

Design: knowledge page ``users-access-trust-map-and-agent-sessions-bake-s-
design-decisions-2026-10-26-voi`` items 2, 3, 4 and 6.

* Several people share one hub. **Owner** is a hub role and there may be
  several; everyone else is a **member**. Owners reach everything (they also
  pass :func:`rook.hub.authz.require_hub_admin`, which keeps hub settings,
  sign-in and policy owner-only whatever grants say).
* A member reaches only what a **grant** names: a band (every machine on it),
  one machine, the hub's own caps, a knowledge area, or anything; each grant
  may narrow to cap patterns and carries a tier ceiling (default ``exec``:
  ``admin`` caps need it said). A person with no grants reaches nothing.
* Every change is attributed and kept in an append-only history with the
  words of whoever asked for it.

The store is ``access.db`` beside ``policy.json`` (both hub processes open
it, as they share ``approvals.db``). :class:`rook.hub.authz.Authorizer`
calls :meth:`AccessStore.evaluate` for every band call a member makes, in
any policy mode. Accounts stay in ``enrollment.db``; :meth:`AccessStore.seed`
carries today's access over once (operators and band owners become owners,
band members get a grant on each band they were in).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import time
import uuid
from typing import Any, Iterable

from ..core import authz as core_authz
from ..core.db import secure_file, transaction

log = logging.getLogger("rook.hub.access")

ROLES = ("owner", "member")
KINDS = ("band", "worker", "hub", "knowledge", "any")
DEFAULT_TIER = "exec"
#: Hub caps a person may always call about themselves (no grant needed).
SELF_CAPS = frozenset({"users.me", "users.explain"})
KNOWLEDGE_CAPS = ("knowledge.read", "knowledge.write")
WORDS_MAX = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id TEXT PRIMARY KEY,
    role    TEXT NOT NULL CHECK(role IN ('owner','member')),
    label   TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    actor   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    id       TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK(kind IN ('band','worker','hub','knowledge','any')),
    ref      TEXT NOT NULL,
    caps     TEXT NOT NULL DEFAULT '["*"]',
    tier     TEXT NOT NULL DEFAULT 'exec',
    created  REAL NOT NULL,
    actor    TEXT NOT NULL,
    words    TEXT NOT NULL DEFAULT '',
    revoked  REAL,
    revoked_by TEXT,
    revoke_words TEXT
);
CREATE INDEX IF NOT EXISTS grants_user ON grants(user_id, revoked);
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class AccessError(ValueError):
    pass


def user_of(principal: Any) -> str | None:
    """The account behind a principal, or None (owners by password, tokens,
    system code). Tokens map to people in the tokens task; until then only
    signed-in dashboard accounts (``human:<user id>``) are people here."""
    pid = str(getattr(principal, "id", "") or "")
    if getattr(principal, "kind", "") != "human" or not pid.startswith("human:"):
        return None
    uid = pid.split(":", 1)[1]
    return None if uid in ("", "dashboard") else uid


def restricted(principal: Any) -> bool:
    """Whether grants decide for this principal: a verified signed-in person
    who is not an owner."""
    return (principal is not None and getattr(principal, "verified", False)
            and user_of(principal) is not None and not principal.fail_open())


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
    def evaluate(self, user_id: str, cap: str, tier: str, target: Any,
                 bands: Iterable[str] = ()) -> tuple[dict | None, str]:
        """``(grant, reason)``: the active grant that lets ``user_id`` make
        this call, or ``None`` and why not. ``bands`` are the key hashes of
        the bands the target was seen on; ``target`` a policy Target."""
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


def store_for(data_dir: str | None) -> AccessStore | None:
    if not data_dir:
        return None
    return AccessStore.for_dir(data_dir)
