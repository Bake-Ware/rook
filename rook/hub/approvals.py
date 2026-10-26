"""Approval holds (``docs/design/permissions.md`` 3.11).

A *held* call waits until a person approves or denies it. The authorizer
(:mod:`rook.hub.authz`) decides that a call is held; this module keeps the
queue and waits for the decision.

Why a call is held, for agent-like principals only (``approvals.principals``:
API tokens, unverified callers, integrations, the house agent; never people,
never hub code):

* a hard stop (rook/hub/hardstops.py): erasing data for good, revealing a
  secret's value, sending as the person to someone else;
* a change on something the trust map calls sacred (rook/hub/trust.py), and,
  with the advanced security plugin on, something it calls guarded.

That is all. There are no blanket holds (operator hold rules in the policy are
ignored), no spread limit and no hub-host holds. An agent changing the
guardrails themselves is refused outright (no ratchet). A deny always wins: a
denied call is never held.

Nothing here expires on a clock. A pending hold waits until a person decides
it (the dashboard, the phone's card, clear-my-cards by voice, or an agent
relaying the person's own words, rook/hub/relay.py); an approval waits until
the call it approved is repeated, and is then used up.

Waiting: the band client calls :meth:`Approvals.resolve` before it sends a
held call. An unused approval for an identical call (same principal, cap,
target and canonical args) lets it through at once and is used up. Otherwise
the hold is created (or joined, when an identical one is pending) and the call
waits up to ``approvals.wait_secs`` for a decision. Approved: it proceeds.
Denied: a denial reply. Still pending: an ``ok: false`` reply with
``held: true`` and the hold id; the agent asks the user and repeats the call.
Nothing is ever run later on the hub's own initiative.

Storage: one SQLite file, ``approvals.db``, in the hub data directory beside
``policy.json`` (WAL, short transactions). Both hub layouts share it: the MCP
process and the dashboard open the same file, and ``rook hub serve`` is one
process.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..core import authz as core_authz
from ..core import names
from ..core.db import secure_file, transaction
from . import hardstops

log = logging.getLogger("rook.hub.approvals")

#: ``expired`` only on holds recorded before approvals stopped expiring.
STATES = ("pending", "approved", "denied", "expired", "consumed")
#: Decided holds kept for the "recent decisions" list.
HISTORY_KEEP = 500
POLL_SECS = 0.25
CONFIG_TTL = 3.0
_DB_TIMEOUT = 3.0
#: Longest string shown per argument, and the whole shown args as JSON.
SHOW_STR = 400
SHOW_TOTAL = 4000
#: Argument names whose values are never shown on the Approvals page.
_SECRET_KEYS = re.compile(r"(?i)(pass(word|wd|phrase)?|secret|token|psk|api_?key|private_?key|"
                          r"credential)")

#: The args as the caller gave them (``{{secret:name}}`` placeholders, not
#: values). ``rook_call`` and console writes set it before the band call,
#: because what reaches the band client has the secrets filled in.
shown_args: contextvars.ContextVar[Any] = contextvars.ContextVar("rook_hold_shown_args",
                                                                 default=None)
#: Hub caps already checked for holds in this MCP tool call (the tool's own
#: cap), so a plugin tool's in-process call to the same cap is not held twice.
gated_caps: contextvars.ContextVar[frozenset] = contextvars.ContextVar("rook_hold_gated",
                                                                       default=frozenset())
#: Set by a tool acting on a resource (a console room, a chat message) the
#: caller owns, so tidying up after itself is never held.
own_resource: contextvars.ContextVar[bool] = contextvars.ContextVar("rook_hold_own_resource",
                                                                    default=False)
#: What a hold made in this call is for, in the caller's words and records
#: (``{task, task_title, project, why, room, room_title, huddle, job}``, or a
#: callable returning that, called only when a hold is made): ``rook_call``
#: sets the caller's claimed task and its ``why``, a console write its room,
#: a job run its job, a huddle card its huddle. Kept with the hold so the
#: person sees "For:" and "Why:" (rook/hub/plain.py).
hold_context: contextvars.ContextVar[Any] = contextvars.ContextVar("rook_hold_context",
                                                                  default=None)
CONTEXT_KEYS = ("task", "task_title", "project", "why", "room", "room_title", "huddle", "job")
CONTEXT_MAX = 300


def clean_context(ctx: Any) -> dict:
    """The known keys of a hold context, as short strings (``{}`` for none)."""
    if not isinstance(ctx, dict):
        return {}
    out = {}
    for k in CONTEXT_KEYS:
        v = ctx.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            out[k] = " ".join(str(v).split())[:CONTEXT_MAX]
    return out


class HoldError(ValueError):
    pass


# -- settings ------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    principals: tuple = ("token:*", "unverified", "integration:*", "agent:*", "role:agent",
                         "role:integration")
    wait_secs: float = 45.0
    dashboard_url: str = ""
    #: The advanced security plugin (rook/hub/plugins/security) is on.
    advanced: bool = False
    relay_dashboard_only: tuple = ("tier:admin", "tag:physical", "worker.*",
                                   "band.*", "hub.plugin.*", "customcap.*", "role.set",
                                   "role.delete", "role.move", "job.write:delete")


def _settings() -> dict:
    from .settings_schema import _CORE
    return {s.name[len("approvals."):]: s for s, _owner, _extra in _CORE
            if s.name.startswith("approvals.")}



def _dashboard_url(store: Any, environ: Any) -> str:
    """The dashboard's public base URL, when the hub knows one."""
    domain = (environ.get("ROOK_DOMAIN") or "").strip()
    try:
        if not domain and store is not None:
            row = store.get("core.hub.domain", "hub", "")
            domain = str((row or {}).get("value") or "").strip()
        if not domain:
            from ..remote import setup_store
            domain = str((setup_store.load() or {}).get("pyz_domain") or "").strip()
    except Exception:
        log.debug("dashboard URL unavailable", exc_info=True)
    if not domain or "example.com" in domain:
        return ""
    try:
        from ..remote.public_url import public_base
        return public_base(domain)
    except Exception:
        return ""


def dashboard_url(store: Any = None, environ: Any = None) -> str:
    """The dashboard's public base URL (``http(s)://host[:port]``), or ""."""
    return _dashboard_url(store, os.environ if environ is None else environ)


def _advanced(store: Any, environ: Any) -> bool:
    from .plugins.security import enabled
    return enabled(store, environ)


def load_config(store: Any = None, environ: Any = None) -> Config:
    """Approval settings: environment > Settings page (hub scope) > default.
    A bad value is logged and skipped, never fatal."""
    environ = environ if environ is not None else os.environ
    values: dict = {}
    for name, s in _settings().items():
        val = s.default
        row = None
        try:
            row = store.get(f"core.{s.name}", "hub", "") if store is not None else None
        except Exception:
            log.debug("approvals: settings store unreadable", exc_info=True)
        if row is not None and row.get("value") is not None:
            try:
                val = s.coerce(row["value"])
            except (TypeError, ValueError) as e:
                log.warning("approvals: stored %s ignored: %s", s.name, e)
        for var in s.env_names():
            if environ.get(var) is not None:
                try:
                    val = s.coerce(environ[var])
                except (TypeError, ValueError) as e:
                    log.warning("approvals: %s ignored: %s", var, e)
                break
        values[name] = val
    return Config(
        principals=tuple(str(x).strip() for x in values.get("principals") or ()
                         if str(x).strip()),
        wait_secs=float(values.get("wait_secs", 45)),
        dashboard_url=_dashboard_url(store, environ),
        advanced=_advanced(store, environ),
        relay_dashboard_only=tuple(str(x).strip() for x in values.get("relay_dashboard_only")
                                   or () if str(x).strip()))


class ConfigSource:
    """:func:`load_config`, re-read at most every :data:`CONFIG_TTL` seconds
    (so the call path does no settings I/O otherwise). ``fixed`` pins a
    config (tests)."""

    def __init__(self, store: Any = None, fixed: Config | None = None,
                 open_store: bool = True) -> None:
        self._store = store
        self._open_store = open_store and store is None
        self.fixed = fixed
        self._cached: Config | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def _get_store(self) -> Any:
        if self._open_store:
            self._open_store = False
            try:
                from .settings_store import SettingsStore
                self._store = SettingsStore()
            except Exception:
                log.debug("approvals: no settings store", exc_info=True)
        return self._store

    def get(self) -> Config:
        if self.fixed is not None:
            return self.fixed
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._at < CONFIG_TTL:
                return self._cached
        try:
            cfg = load_config(self._get_store())
        except Exception:
            log.exception("approvals: settings unreadable; using the defaults")
            cfg = Config()
        with self._lock:
            self._cached, self._at = cfg, now
        return cfg


# -- args ----------------------------------------------------------------------

def args_hash(args: Any) -> str:
    """sha256 over canonical JSON of the args as given (placeholders, not
    secret values): the identity of "the same call"."""
    try:
        blob = core_authz.canonical(args if args is not None else {})
    except (TypeError, ValueError):
        blob = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _redact(obj: Any, cap: str, depth: int = 0) -> Any:
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if isinstance(v, str) and (_SECRET_KEYS.search(ks) or
                                       (ks == "value" and cap.startswith(("secret.", "settings.")))):
                out[ks] = v if v.startswith("{{secret:") else "[hidden]"
            else:
                out[ks] = _redact(v, cap, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v, cap, depth + 1) for v in obj[:50]] + (["…"] if len(obj) > 50 else [])
    if isinstance(obj, str) and len(obj) > SHOW_STR:
        return obj[:SHOW_STR] + f"… ({len(obj)} chars)"
    return obj


def display_args(args: Any, cap: str = "") -> Any:
    """The args as shown on the Approvals page: known vault values masked,
    secret-looking fields hidden, long values cut. Never resolves a secret."""
    shown = args if args is not None else {}
    try:
        from ..band_mcp.secret_mask import scrub
        shown = scrub(shown)
    except Exception:
        pass
    shown = _redact(shown, cap)
    try:
        text = json.dumps(shown, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_unprintable": True}
    if len(text) > SHOW_TOTAL:
        return {"_truncated": text[:SHOW_TOTAL] + "…"}
    return shown


# -- principals --------------------------------------------------------------

_NEVER = ("human", "system")


def _sel_match(sel: str, pid: str, kind: str, role: str | None) -> bool:
    if sel == "*":
        return True
    if sel.startswith("role:"):
        return role is not None and role == sel[5:]
    if sel.endswith(":*"):
        return kind == sel[:-2]
    return pid == sel


def _kind_of(pid: str) -> str:
    return pid.split(":", 1)[0] if ":" in pid else pid


def holdable(p: Any, selectors: tuple) -> bool:
    """Whether calls by principal ``p`` can be held: the principal, or any
    principal it acts for (its on-behalf-of chain), matches a selector. A
    person or hub code at the head of the chain is held only when an agent is
    in the chain (a job an agent set up, the house agent)."""
    include = [s for s in selectors if not s.startswith("!")]
    exclude = [s[1:] for s in selectors if s.startswith("!")]
    if any(_sel_match(s, p.id, p.kind, p.role) for s in exclude):
        return False
    via = [str(v) for v in (p.via or ())]
    if any(_sel_match(s, v, _kind_of(v), None) for s in include for v in via
           if _kind_of(v) not in _NEVER):
        return True
    if p.kind in _NEVER or _kind_of(p.id) in _NEVER:
        return False
    return any(_sel_match(s, p.id, p.kind, p.role) for s in include)


# -- the queue ----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS holds(
    id TEXT PRIMARY KEY, state TEXT NOT NULL, kind TEXT NOT NULL,
    principal TEXT NOT NULL, principal_label TEXT NOT NULL DEFAULT '',
    via_json TEXT NOT NULL DEFAULT '[]', cap TEXT NOT NULL, tier TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]', target_id TEXT, target_name TEXT NOT NULL DEFAULT '',
    is_hub INTEGER NOT NULL DEFAULT 0, args_json TEXT, args_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', rule TEXT, created REAL NOT NULL, expires REAL NOT NULL,
    decided_by TEXT, decided_at REAL, scope TEXT, valid_until REAL, consumed_at REAL,
    note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS holds_state ON holds(state, created);
CREATE INDEX IF NOT EXISTS holds_match ON holds(principal, cap, target_id, args_hash, state);
-- Relayed decisions (rook/hub/relay.py): an agent approved or denied an item
-- on a person's behalf, with the person's own words. Append-only: never
-- updated, never deleted (the triggers refuse), kept beyond the hold history.
CREATE TABLE IF NOT EXISTS relays(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, item TEXT NOT NULL, decision TEXT NOT NULL,
    relayer TEXT NOT NULL, relayer_label TEXT NOT NULL DEFAULT '', who TEXT NOT NULL,
    quote TEXT NOT NULL, session TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS relays_item ON relays(kind, item);
CREATE TRIGGER IF NOT EXISTS relays_no_update BEFORE UPDATE ON relays
    BEGIN SELECT RAISE(ABORT, 'relays are append-only'); END;
CREATE TRIGGER IF NOT EXISTS relays_no_delete BEFORE DELETE ON relays
    BEGIN SELECT RAISE(ABORT, 'relays are append-only'); END;
-- Who may relay the person's approvals: set by a person on the dashboard
-- only (the Tokens page); no cap, tool or setting writes it.
CREATE TABLE IF NOT EXISTS relayers(
    principal TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', enabled_by TEXT NOT NULL,
    enabled_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS relayer_log(
    principal TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL,
    by TEXT NOT NULL, at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS relayer_log_no_update BEFORE UPDATE ON relayer_log
    BEGIN SELECT RAISE(ABORT, 'the relayer log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS relayer_log_no_delete BEFORE DELETE ON relayer_log
    BEGIN SELECT RAISE(ABORT, 'the relayer log is append-only'); END;
"""

#: How long a person's denial answers an identical repeat of the call at once
#: (the agent hears the denial and the reason instead of a new hold).
DENIAL_ECHO_SECS = 300.0
RELAY_FIELDS = ("who", "quote", "session")


def _relay_row(r: Any) -> dict:
    return {k: r[k] for k in ("id", "kind", "item", "decision", "relayer", "relayer_label", "who",
                              "quote", "session", "reason", "summary", "created")}


def relay_text(rel: dict) -> str:
    """``approved by Bake via Claude web: "do it"`` (the dashboard and the
    requester see the same line)."""
    verb = {"approve": "approved", "deny": "denied", "answer": "answered"}.get(
        rel.get("decision"), str(rel.get("decision") or "decided"))
    via = rel.get("relayer_label") or rel.get("relayer") or "an agent"
    return f'{verb} by {rel.get("who")} via {via}: "{rel.get("quote")}"'



def _row(r: Any) -> dict:
    d = dict(r)
    d["via"] = json.loads(d.pop("via_json") or "[]")
    d["tags"] = json.loads(d.pop("tags_json") or "[]")
    raw = d.pop("args_json")
    d["args"] = json.loads(raw) if raw else {}
    d["is_hub"] = bool(d.get("is_hub"))
    raw = d.pop("context_json", None)
    try:
        d["context"] = clean_context(json.loads(raw)) if raw else {}
    except ValueError:
        d["context"] = {}
    return d


class ApprovalQueue:
    """The holds file. Every method is one short transaction, so two hub
    processes can share it."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._ready = False
        self._lock = threading.Lock()
        self._swept = 0.0

    def _db(self, write: bool = False):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                    secure_file(self.path)
                    with transaction(self.path, begin=None, timeout=_DB_TIMEOUT, wal=True) as db:
                        db.executescript(_SCHEMA)
                        cols = {r[1] for r in db.execute("PRAGMA table_info(holds)")}
                        if "context_json" not in cols:
                            db.execute("ALTER TABLE holds ADD COLUMN context_json TEXT")
                        names.ensure_table(db)
                    self._ready = True
        return transaction(self.path, begin="IMMEDIATE" if write else None,
                           timeout=_DB_TIMEOUT, wal=True)

    # -- upkeep ------------------------------------------------------------
    def sweep(self, force: bool = False) -> None:
        """Keep a bounded history of decided holds. Nothing made now expires:
        a pending hold waits for a person, an approval for the call it
        approved. Rows written before that (``expires`` > 0, a ``valid_until``)
        keep the deadline they were given, so a stale approval or ask from
        then never comes back."""
        now = self.clock()
        if not force and now - self._swept < 2.0:
            return
        self._swept = now
        with self._db(write=True) as db:
            db.execute("UPDATE holds SET state='expired' WHERE state='pending' AND expires>0 "
                       "AND expires<?", (now,))
            db.execute("UPDATE holds SET state='expired' WHERE state='approved' "
                       "AND valid_until IS NOT NULL AND valid_until<?", (now,))
            db.execute("DELETE FROM holds WHERE state!='pending' AND id NOT IN (SELECT id FROM "
                       "holds WHERE state!='pending' ORDER BY created DESC LIMIT ?)",
                       (HISTORY_KEEP,))

    # -- writes ------------------------------------------------------------
    def create(self, *, principal: str, cap: str, tier: str, args_hash: str, label: str = "",
               via: tuple = (), tags: tuple = (), target_id: str | None = None,
               target_name: str = "", is_hub: bool = False, args: Any = None,
               reason: str = "", rule: str | None = None, kind: str = "default",
               context: dict | None = None) -> tuple[dict, bool]:
        """A new pending hold, or the pending one for the identical call.
        Returns ``(hold, created)``."""
        self.sweep()
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE state='pending' AND principal=? AND cap=? "
                           "AND target_id IS ? AND args_hash=? AND (expires=0 OR expires>=?) "
                           "ORDER BY created LIMIT 1",
                           (principal, cap, target_id, args_hash, now)).fetchone()
            if r is not None:
                found = _row(r)
                found["name"] = names.name_of(db, "hold", found["id"])
                return found, False
            hid = "h" + secrets.token_hex(12)
            db.execute(
                "INSERT INTO holds(id, state, kind, principal, principal_label, via_json, cap, "
                "tier, tags_json, target_id, target_name, is_hub, args_json, args_hash, reason, "
                "rule, created, expires, context_json) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, kind, principal, label or "", json.dumps(list(via)), cap, tier,
                 json.dumps(list(tags)), target_id, target_name or "", int(bool(is_hub)),
                 json.dumps(args, ensure_ascii=False, default=str), args_hash, reason, rule,
                 now, 0.0,
                 json.dumps(clean_context(context), ensure_ascii=False) if context else None))
            names.assign(db, "hold", hid)
            r = db.execute("SELECT * FROM holds WHERE id=?", (hid,)).fetchone()
        return self._named([_row(r)])[0], True

    def decide(self, hold_id: str, approve: bool, by: str, *, scope: str = "call",
               note: str = "", relay: dict | None = None) -> dict:
        """Approve or deny a pending hold; an approval stays until the call
        it approved uses it. Raises :class:`HoldError` unless the hold is
        pending. ``relay`` (an agent relaying the person's decision,
        rook/hub/relay.py): the audit row is written in the same transaction."""
        if scope != "call":
            raise HoldError("scope must be call")
        self.sweep(force=True)
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE id=?", (str(hold_id),)).fetchone()
            if r is None:
                raise HoldError("no such hold")
            if r["state"] != "pending":
                raise HoldError(f"hold {hold_id} is {r['state']}, not pending")
            db.execute("UPDATE holds SET state=?, decided_by=?, decided_at=?, scope=?, "
                       "valid_until=NULL, note=? WHERE id=? AND state='pending'",
                       ("approved" if approve else "denied", by, now, scope,
                        str(note or "")[:500], r["id"]))
            if relay is not None:
                self._relay_insert(db, {**relay, "kind": relay.get("kind") or "hold",
                                        "item": r["id"],
                                        "decision": "approve" if approve else "deny"})
            r = db.execute("SELECT * FROM holds WHERE id=?", (r["id"],)).fetchone()
        return self._named([_row(r)])[0]

    def consume_match(self, principal: str, cap: str, target_id: str | None,
                      args_hash: str) -> dict | None:
        """Use up an approval for this identical call, if one is unused."""
        self.sweep()
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE state='approved' AND principal=? AND cap=? "
                           "AND target_id IS ? AND args_hash=? AND (valid_until IS NULL OR "
                           "valid_until>=?) ORDER BY decided_at LIMIT 1",
                           (principal, cap, target_id, args_hash, now)).fetchone()
            if r is None:
                return None
            db.execute("UPDATE holds SET state='consumed', consumed_at=? WHERE id=?",
                       (now, r["id"]))
            r = db.execute("SELECT * FROM holds WHERE id=?", (r["id"],)).fetchone()
        return _row(r)

    def recent_denial(self, principal: str, cap: str, target_id: str | None, args_hash: str,
                      within: float = DENIAL_ECHO_SECS) -> dict | None:
        """A person's denial of this identical call in the last ``within``
        seconds: a repeat gets the denial (and its reason), not a new hold."""
        since = self.clock() - within
        with self._db() as db:
            r = db.execute("SELECT * FROM holds WHERE state='denied' AND principal=? AND cap=? "
                           "AND target_id IS ? AND args_hash=? AND decided_at>=? "
                           "ORDER BY decided_at DESC LIMIT 1",
                           (principal, cap, target_id, args_hash, since)).fetchone()
        return _row(r) if r is not None else None

    def consume_id(self, hold_id: str) -> bool:
        now = self.clock()
        with self._db(write=True) as db:
            cur = db.execute("UPDATE holds SET state='consumed', consumed_at=? WHERE id=? "
                             "AND state='approved' AND (valid_until IS NULL OR valid_until>=?)",
                             (now, hold_id, now))
            return cur.rowcount == 1

    # -- reads -------------------------------------------------------------
    def get(self, hold_id: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM holds WHERE id=?", (str(hold_id),)).fetchone()
            if r is None and names.looks_like_name(hold_id):
                hit = names.find(db, "hold", hold_id)
                if len(hit) == 1:
                    r = db.execute("SELECT * FROM holds WHERE id=?", (hit[0],)).fetchone()
        if r is None:
            return None
        return self._named([_row(r)])[0]

    def pending(self, limit: int = 200) -> list[dict]:
        self.sweep()
        with self._db() as db:
            rows = db.execute("SELECT * FROM holds WHERE state='pending' ORDER BY created "
                              "LIMIT ?", (int(limit),)).fetchall()
        return self._named([_row(r) for r in rows])

    def recent(self, limit: int = 50) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM holds WHERE state!='pending' "
                              "ORDER BY COALESCE(decided_at, created) DESC LIMIT ?",
                              (int(limit),)).fetchall()
        return self._named([_row(r) for r in rows])

    def count_pending(self) -> int:
        self.sweep()
        with self._db() as db:
            return int(db.execute("SELECT count(*) FROM holds WHERE state='pending'")
                       .fetchone()[0])

    # -- speakable names ---------------------------------------------------
    def _named(self, rows: list[dict]) -> list[dict]:
        """Holds with their speakable name (``hold`` kind), given one when
        they have none yet (holds made before names)."""
        if not rows:
            return rows
        with self._db() as db:
            have = names.names_of(db, "hold", [r["id"] for r in rows])
        missing = [r["id"] for r in rows if r["id"] not in have]
        if missing:
            with self._db(write=True) as db:
                for hid in missing:
                    have[hid] = names.assign(db, "hold", hid)
        for r in rows:
            r["name"] = have.get(r["id"])
        return rows

    # -- relayed decisions (append-only audit) -----------------------------
    def _relay_insert(self, db: Any, rel: dict) -> dict:
        for k in RELAY_FIELDS + ("relayer",):
            if not str(rel.get(k) or "").strip():
                raise HoldError(f"a relayed decision needs {k}")
        row = {"id": "rl" + secrets.token_hex(10), "kind": str(rel["kind"]),
               "item": str(rel["item"]), "decision": str(rel["decision"]),
               "relayer": str(rel["relayer"]), "relayer_label": str(rel.get("relayer_label") or ""),
               "who": str(rel["who"])[:120], "quote": str(rel["quote"])[:1000],
               "session": str(rel["session"])[:500], "reason": str(rel.get("reason") or "")[:500],
               "summary": str(rel.get("summary") or "")[:300], "created": self.clock()}
        db.execute("INSERT INTO relays(id, kind, item, decision, relayer, relayer_label, who, "
                   "quote, session, reason, summary, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   tuple(row[k] for k in ("id", "kind", "item", "decision", "relayer",
                                          "relayer_label", "who", "quote", "session", "reason",
                                          "summary", "created")))
        return row

    def relay_add(self, rel: dict) -> dict:
        """Record a relayed decision on an item kept elsewhere (a huddle card
        or question). ``rel``: kind, item, decision, relayer, relayer_label,
        who, quote, session, reason?, summary?"""
        with self._db(write=True) as db:
            return self._relay_insert(db, rel)

    def relays_for(self, kind: str | None, items: list[str]) -> dict[str, dict]:
        """The relayed decision on each item (the latest), by item id."""
        items = [str(i) for i in items if i]
        if not items:
            return {}
        out: dict[str, dict] = {}
        with self._db() as db:
            for i in range(0, len(items), 500):
                chunk = items[i:i + 500]
                q = ",".join("?" * len(chunk))
                sql = f"SELECT * FROM relays WHERE item IN ({q})"
                args: list = list(chunk)
                if kind:
                    sql += " AND kind=?"
                    args.append(kind)
                for r in db.execute(sql + " ORDER BY created", args).fetchall():
                    out[r["item"]] = _relay_row(r)
        return out

    def relays(self, limit: int = 50) -> list[dict]:
        with self._db() as db:
            return [_relay_row(r) for r in db.execute(
                "SELECT * FROM relays ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()]

    # -- who may relay (written from the dashboard only) --------------------
    def relayer(self, principal: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM relayers WHERE principal=?", (str(principal),)).fetchone()
        return dict(r) if r is not None else None

    def relayers(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM relayers ORDER BY enabled_at").fetchall()]

    def set_relayer(self, principal: str, enabled: bool, by: str, label: str = "") -> bool:
        """Turn relaying on or off for ``principal`` (logged). Only the
        dashboard calls this, for a signed-in person. Returns whether it
        changed."""
        principal = str(principal or "").strip()
        if not principal:
            raise HoldError("which identity?")
        now = self.clock()
        with self._db(write=True) as db:
            had = db.execute("SELECT 1 FROM relayers WHERE principal=?",
                             (principal,)).fetchone() is not None
            if had == bool(enabled):
                return False
            if enabled:
                db.execute("INSERT INTO relayers(principal, label, enabled_by, enabled_at) "
                           "VALUES (?, ?, ?, ?)", (principal, str(label)[:120], by, now))
            else:
                db.execute("DELETE FROM relayers WHERE principal=?", (principal,))
            db.execute("INSERT INTO relayer_log(principal, label, enabled, by, at) "
                       "VALUES (?, ?, ?, ?, ?)", (principal, str(label)[:120], int(bool(enabled)),
                                                  by, now))
        return True


# -- the glue the authorizer uses ---------------------------------------------------

_PROCEEDS = ("allow", "would_deny", "off", "error_allow")


class Approvals:
    """Queue + settings for one hub process (owned by its
    :class:`rook.hub.authz.Authorizer`)."""

    def __init__(self, path: str, config: "ConfigSource | Config | None" = None,
                 record: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, trust: Any = None) -> None:
        #: The trust map (:class:`rook.hub.trust.TrustStore`), or None: no map.
        self.trust = trust
        if isinstance(config, Config):
            config = ConfigSource(fixed=config)
        self.config_source = config or ConfigSource()
        self.queue = ApprovalQueue(path, clock=clock)
        self._record = record
        self._monotonic = monotonic
        self.client: Any = None

    def config(self) -> Config:
        return self.config_source.get()

    def attach_client(self, client: Any) -> None:
        """The band client whose roster names the machines an ssh command
        reaches (:meth:`remote_ruling`)."""
        self.client = client

    def _client(self) -> Any:
        return self.client

    # -- deciding to hold (synchronous, on the call path) ------------------------
    def apply(self, d: Any, p: Any, policy: Any, target: Any, args: Any = None) -> Any:
        """Add the approval layer to a policy decision ``d`` for principal
        ``p`` and the call's ``args``. Returns ``d``, possibly turned into a
        hold, or a deny for an agent changing the guardrails.

        For agent-like principals only: the hard stops
        (rook/hub/hardstops.py) and the trust map's sacred entries (guarded
        ones too with advanced security on). A hold from an operator's policy
        rule is ignored: there are no blanket holds."""
        cfg = self.config()
        d.target_is_hub = bool(getattr(target, "is_rook", False))
        if d.denied:
            return d  # a deny always beats a hold
        agent = holdable(p, cfg.principals)
        why = hardstops.guardrail(d.cap, args) if agent else ""
        if why:
            return dataclasses.replace(
                d, decision="deny", rule="hardstop:guardrail",
                reason=f"{why}: only a person changes the guardrails (the dashboard, or by "
                       "voice); say what you would change in your report instead")
        if d.held:
            d = self._unhold(d, "operator hold rules are retired")
        if not agent or own_resource.get():
            return d
        ruling = self.trust_ruling(d, target)
        if ruling is not None:
            d = dataclasses.replace(d, trust_rule=ruling.rule, trust=ruling.cite)
            if _trust_holds(ruling, cfg.advanced):
                return dataclasses.replace(d, decision="hold", hold_kind="trust",
                                           hold_reason=ruling.cite, rule=ruling.rule)
        ruling = self.remote_ruling(d, args)
        if ruling is not None:
            return dataclasses.replace(d, decision="hold", hold_kind="trust", rule=ruling.rule,
                                       hold_reason=f"{ruling.cite} (reached over ssh)")
        why = hardstops.stop(d.cap, args, d.tags)
        if why:
            return dataclasses.replace(d, decision="hold", hold_kind="default", hold_reason=why,
                                       rule="hardstop")
        return d

    def remote_ruling(self, d: Any, args: Any) -> Any:
        """The trust map's ruling when a command reaches a sacred machine over
        ssh, scp or rsync from another one (None when it doesn't)."""
        if self.trust is None:
            return None
        hosts = hardstops.remote_hosts(hardstops.command_text(d.cap, args))
        if not hosts:
            return None
        roster = getattr(self._client(), "workers", {}) or {}
        for host in hosts:
            names = {host, host.split(".", 1)[0]} | {
                str(e.get("name") or "") for e in roster.values()
                if str((e.get("facts") or {}).get("hostname") or "").lower() in
                (host, host.split(".", 1)[0])}
            for name in sorted(n for n in names if n):
                r = self.trust.rule(d.cap, d.tier, tuple(d.tags or ()), worker=name)
                if r.tier == "person":
                    return r
        return None

    def trust_ruling(self, d: Any, target: Any) -> Any:
        """The trust map's ruling for a call (None without a map)."""
        if self.trust is None:
            return None
        doc = self.trust.current()
        if not doc.get("entries") and doc.get("unlisted", "balanced") == "balanced":
            return None     # an empty map changes nothing, so it cites nothing
        is_hub = bool(getattr(target, "is_rook", False))
        name = "rook" if is_hub else str(getattr(target, "name", "") or "")
        # A borrowed resource (docs/design/onboarding.md): only the house
        # agent's own declared stop/restore call for it carries the resource.
        from .trust import borrowing
        b = borrowing.get() or {}
        resource = ""
        if b and b.get("cap") == d.cap and str(b.get("principal") or "") == str(
                getattr(d, "principal", "") or "") and str(b.get("worker") or "").lower() in (
                name.lower(), str(getattr(target, "id", "") or "").lower()):
            resource = str(b.get("resource") or "")
        return self.trust.rule(d.cap, d.tier, tuple(d.tags or ()), worker=name,
                               worker_id=str(getattr(target, "id", "") or ""), on_hub=is_hub,
                               resource=resource)

    @staticmethod
    def _unhold(d: Any, why: str) -> Any:
        return dataclasses.replace(d, decision="allow", hold_kind="", hold_reason="",
                                   reason=(d.reason + "; " if d.reason else "") + f"hold ignored: {why}")

    # -- waiting (asynchronous, before the call is sent) --------------------------
    async def resolve(self, d: Any, args: Any, label: str = "", _retry: bool = True) -> Any:
        """Settle a held decision: an allow (approved, or an approval used up),
        a deny (a person denied it), or the ``held`` reply dict (no decision
        within the wait)."""
        cfg = self.config()
        given = shown_args.get()
        source = given if given is not None else args
        digest = args_hash(source)
        hit = await asyncio.to_thread(self.queue.consume_match, d.principal, d.cap, d.target,
                                      digest)
        if hit is not None:
            return self._approved(d, hit)
        denied = await asyncio.to_thread(self.queue.recent_denial, d.principal, d.cap, d.target,
                                         digest, DENIAL_ECHO_SECS)
        if denied is not None:
            return self._denied(d, denied)
        hold, created = await asyncio.to_thread(
            self.queue.create, principal=d.principal, cap=d.cap, tier=d.tier, args_hash=digest,
            label=label, via=tuple(d.via), tags=tuple(d.tags), target_id=d.target,
            target_name=d.target_name, is_hub=bool(d.target_is_hub),
            args=display_args(source, d.cap), reason=d.hold_reason, rule=d.rule,
            kind=d.hold_kind or "default", context=_scrubbed(hold_context.get()))
        if created:
            self.journal_hold(hold)       # the phone's card and the dashboard carry it
        deadline = self._monotonic() + max(0.0, cfg.wait_secs)
        while True:
            row = await asyncio.to_thread(self.queue.get, hold["id"])
            state = row["state"] if row else "expired"
            if state == "approved":
                if await asyncio.to_thread(self.queue.consume_id, hold["id"]):
                    return self._approved(d, row)
                state = "consumed"
            if state == "denied":
                return self._denied(d, row)
            if state == "consumed" and _retry:
                # An identical call used this approval first: wait for another.
                return await self.resolve(d, args, label, _retry=False)
            if state != "pending":
                break
            left = deadline - self._monotonic()
            if left <= 0:
                break
            await asyncio.sleep(min(POLL_SECS, left))
        return self.held_reply(d, hold, cfg)

    @staticmethod
    def _denied(d: Any, row: dict) -> Any:
        return dataclasses.replace(
            d, decision="deny", rule="approval:denied", hold_id=row["id"],
            decided_by=str(row.get("decided_by") or "a person"),
            note=str(row.get("note") or ""),
            reason=f"denied by {row.get('decided_by')}")

    @staticmethod
    def _approved(d: Any, row: dict) -> Any:
        return dataclasses.replace(d, decision="allow", rule=f"approval:{row['id']}",
                                   hold_id=row["id"], decided_by=str(row.get("decided_by") or ""),
                                   reason=f"approved by {row.get('decided_by')}")

    def held_reply(self, d: Any, hold: dict, cfg: Config | None = None) -> dict:
        cfg = cfg or self.config()
        where = d.target_name or d.target or "the band"
        why = f" ({hold.get('reason')})" if hold.get("reason") else ""
        named = f", {hold['name']}" if hold.get("name") else ""
        msg = (f"Held for approval (hold {hold['id']}{named}): {d.cap} on {where}{why}. Ask the user "
               f"to approve it (their phone's card, the dashboard's Approvals page, or their own "
               f"words relayed with rook_approvals), then repeat the same call; an approval "
               f"covers one identical call (same cap, worker and args) and does not expire. "
               f"Do not try to get around the hold another way.")
        out = {"ok": False, "held": True, "hold": hold["id"], "error": msg}
        if hold.get("name"):
            out["name"] = hold["name"]
        if cfg.dashboard_url:
            out["approve_at"] = cfg.dashboard_url + "/#approvals"
        return out

    # -- deciding (people only: the dashboard, or the operator's voice) -------------
    def decide(self, hold_id: str, approve: bool, by: str, *, scope: str = "call",
               note: str = "", heard: str | None = None, relay: dict | None = None) -> dict:
        """A person's decision: from the dashboard, by voice (``heard``), or
        relayed by an agent the person enabled (``relay``: the relayer and
        the person's own words; checked by rook/hub/relay.py first)."""
        row = self.queue.decide(hold_id, approve, by, scope=scope, note=note, relay=relay)
        detail = {"hold": row["id"], "decision": row["state"], "scope": scope,
                  "cap": row["cap"], "principal": row["principal"],
                  "worker": row["target_name"] or row["target_id"]}
        if heard is not None:
            detail["heard"] = heard[:300]
        if relay is not None:
            detail["relayed_by"] = {k: relay.get(k) for k in ("relayer", "relayer_label", "who",
                                                               "quote", "session")}
            if note:
                detail["reason"] = note
        self._journal("approval.decide", row["target_name"] or row["target_id"], by,
                      {"ok": True, **detail})
        return row

    # -- journal ---------------------------------------------------------------
    def journal_hold(self, hold: dict) -> None:
        self._journal("approval.hold", hold.get("target_name") or hold.get("target_id"),
                      hold["principal"],
                      {"ok": False, "held": True, "hold": hold["id"], "cap": hold["cap"],
                       "tier": hold["tier"], "reason": hold.get("reason"),
                       "kind": hold.get("kind"), "via": hold.get("via") or [],
                       "error": f"held for approval: {hold['cap']}"},
                      authz={"principal": hold["principal"], "decision": "hold",
                             "rule": hold.get("rule"), "tier": hold["tier"]})

    def _journal(self, cap: str, worker: Any, identity: str, reply: dict,
                 authz: dict | None = None) -> None:
        if self._record is None:
            return
        try:
            kw = {"cap": cap, "worker": worker, "identity": identity, "args": None,
                  "reply": reply}
            if authz is not None:
                kw["authz"] = authz
            self._record(**kw)
        except TypeError:
            try:
                self._record(cap=cap, worker=worker, identity=identity, args=None, reply=reply)
            except Exception:
                log.exception("journaling %s failed", cap)
        except Exception:
            log.exception("journaling %s failed", cap)


def _trust_holds(ruling: Any, advanced: bool) -> bool:
    """Whether a trust ruling holds the call: ``sacred`` (tier ``person``)
    always; ``guarded`` (tier ``ask``) only with advanced security on, and
    never the ``balanced`` default for an unlisted machine."""
    if ruling.tier == "person":
        return True
    return advanced and ruling.tier == "ask" and not (ruling.unlisted
                                                      and ruling.level == "balanced")


def _scrubbed(ctx: Any) -> dict | None:
    """A hold context with known vault values masked (an agent's ``why`` is
    free text)."""
    if callable(ctx):
        try:
            ctx = ctx()
        except Exception:  # noqa: BLE001 - a lookup failing never stops the hold
            log.exception("approvals: hold context lookup failed")
            ctx = None
    ctx = clean_context(ctx)
    if not ctx:
        return None
    try:
        from ..band_mcp.secret_mask import scrub
        return scrub(ctx)
    except Exception:  # noqa: BLE001 - masking is best effort, as for args
        return ctx


def public(hold: dict) -> dict:
    """A hold as the Approvals page shows it: the plain ask (rook/hub/plain.py)
    first, the technical fields for Explain."""
    from .plain import for_hold
    out = {k: hold.get(k) for k in ("id", "name", "state", "kind", "principal", "principal_label", "via",
                                    "cap", "tier", "tags", "target_id", "target_name", "args",
                                    "reason", "rule", "created", "expires", "decided_by",
                                    "decided_at", "scope", "valid_until", "consumed_at", "note",
                                    "context")}
    try:
        out["plain"] = for_hold(hold)
    except Exception:  # noqa: BLE001 - a describing bug never hides a hold
        log.exception("approvals: describing hold %s", hold.get("id"))
    out["worker"] = hold.get("target_name") or hold.get("target_id") or ""
    if hold.get("relayed"):
        out["relayed"] = hold["relayed"]
        out["relayed_text"] = relay_text(hold["relayed"])
    return out
