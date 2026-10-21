"""Approval holds and the spread limit (``docs/design/permissions.md`` 3.11).

A *held* call waits until a person approves or denies it. The authorizer
(:mod:`rook.hub.authz`) decides that a call is held; this module keeps the
queue, waits for the decision and tracks how many machines each agent has
reached.

Why a call is held, for agent-like principals only (``approvals.principals``:
API tokens, unverified callers, integrations, the house agent; never people,
never hub code):

* an operator ``hold`` rule or cell in the policy;
* the built-in default, ``approvals.hold`` (``tier:admin``, ``tag:destructive``),
  unless a policy ``allow`` rule at least as specific as the default matches
  (an operator exception such as "token:ci may run worker.update on nas");
* the spread limit: the call would be the agent's ``spread_max + 1``-th
  distinct machine for exec/admin/destructive calls within the window, and no
  spread grant is active for it.

A deny always wins: a denied call is never held.

Waiting: the band client calls :meth:`Approvals.resolve` before it sends a
held call. An unconsumed approval for an identical call (same principal, cap,
target and canonical args) lets it through at once and is used up. Otherwise
the hold is created (or joined, when an identical one is pending) and the call
waits up to ``approvals.wait_secs`` for a decision. Approved: it proceeds.
Denied: a denial reply. Still pending: an ``ok: false`` reply with
``held: true`` and the hold id; the agent asks the user and repeats the call.
Nothing is ever run later on the hub's own initiative.

Storage: one SQLite file, ``approvals.db``, in the hub data directory beside
``policy.json`` (WAL, short transactions). Both hub layouts share it: the MCP
process and the dashboard open the same file, and ``rook hub serve`` is one
process. Spread counters live in memory in the process that authorizes agent
calls (the MCP side); spread grants are stored in the file so a grant made on
the dashboard reaches the MCP side.
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

log = logging.getLogger("rook.hub.approvals")

STATES = ("pending", "approved", "denied", "expired", "consumed")
SCOPES = ("call", "spread")
CHANNELS = ("telegram", "notify", "voice", "ask")
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
#: caller owns, so tidying up after itself is not held by the built-in
#: destructive hold (operator hold rules and the spread limit still apply).
own_resource: contextvars.ContextVar[bool] = contextvars.ContextVar("rook_hold_own_resource",
                                                                    default=False)


class HoldError(ValueError):
    pass


# -- settings ------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    enabled: bool = True
    hold: tuple = ("tier:admin", "tag:destructive")
    principals: tuple = ("token:*", "unverified", "integration:*", "agent:*", "role:agent",
                         "role:integration")
    wait_secs: float = 45.0
    approval_secs: float = 600.0
    spread_max: int = 5
    spread_window_secs: float = 600.0
    spread_grant_secs: float = 900.0
    contact: tuple = ("telegram",)
    contact_worker: str = ""
    voice_max_tier: str = "admin"
    notify_every_secs: float = 60.0
    hub_host_workers: tuple = ("auto",)
    dashboard_url: str = ""
    relay_dashboard_only: tuple = ("tier:admin", "hold:hub-host", "tag:physical", "worker.*",
                                   "band.*", "hub.plugin.*", "customcap.*", "role.set",
                                   "role.delete", "role.move", "job.write:delete")


def _settings() -> dict:
    from .settings_schema import _CORE
    return {s.name[len("approvals."):]: s for s, _owner, _extra in _CORE
            if s.name.startswith("approvals.")}


def _contact(values: Any) -> tuple:
    out = []
    for v in values or ():
        c = str(v).strip().lower()
        if c in ("off", "none", ""):
            if c == "off":
                return ()
            continue
        if c not in CHANNELS:
            log.warning("approvals.contact: unknown channel %r ignored (use off, %s)", c,
                        ", ".join(CHANNELS))
            continue
        if c not in out:
            out.append(c)
    return tuple(out)


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
        enabled=bool(values.get("enabled", True)),
        hold=tuple(str(x).strip() for x in values.get("hold") or () if str(x).strip()),
        principals=tuple(str(x).strip() for x in values.get("principals") or ()
                         if str(x).strip()),
        wait_secs=float(values.get("wait_secs", 45)),
        approval_secs=float(values.get("approval_secs", 600)),
        spread_max=int(values.get("spread_max", 5)),
        spread_window_secs=float(values.get("spread_window_secs", 600)),
        spread_grant_secs=float(values.get("spread_grant_secs", 900)),
        contact=_contact(values.get("contact")),
        contact_worker=str(values.get("contact_worker") or "").strip(),
        voice_max_tier=str(values.get("voice_max_tier") or "admin"),
        notify_every_secs=float(values.get("notify_every_secs", 60)),
        hub_host_workers=tuple(str(x).strip() for x in values.get("hub_host_workers") or ()
                               if str(x).strip()),
        dashboard_url=_dashboard_url(store, environ),
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


def default_score(selectors: tuple, cap: str, tier: str, tags: tuple) -> tuple[int, str] | None:
    """``(cap specificity, selector)`` of the best ``approvals.hold``
    selector matching the call, or None."""
    from .policy import PolicyError, _CapSel
    best = None
    for spec in selectors:
        try:
            sel = _CapSel(spec)
        except PolicyError:
            log.warning("approvals.hold: bad selector %r ignored", spec)
            continue
        score = sel.match(cap, tier, tags)
        if score is not None and (best is None or score > best[0]):
            best = (score, spec)
    return best


def _reason(spec: str) -> str:
    if spec.startswith("tier:"):
        return f"tier {spec[5:]}"
    if spec.startswith("tag:"):
        return f"tag {spec[4:]}"
    return f"cap {spec}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def countable(cap: str, tier: str, tags: tuple, target: Any) -> bool:
    """Spread counts exec/admin and destructive calls to a worker other than
    the hub itself."""
    if target is None or not getattr(target, "id", None):
        return False
    if getattr(target, "is_rook", False) or str(getattr(target, "name", "")).lower() == "rook":
        return False
    return core_authz.tier_rank(tier) >= core_authz.tier_rank("exec") or "destructive" in tags


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
CREATE TABLE IF NOT EXISTS grants(
    principal TEXT NOT NULL, kind TEXT NOT NULL, until REAL NOT NULL, created REAL NOT NULL,
    created_by TEXT NOT NULL, hold_id TEXT, PRIMARY KEY(principal, kind));
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
DENIAL_ECHO_SECS = 600.0
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
                        names.ensure_table(db)
                    self._ready = True
        return transaction(self.path, begin="IMMEDIATE" if write else None,
                           timeout=_DB_TIMEOUT, wal=True)

    # -- upkeep ------------------------------------------------------------
    def sweep(self, force: bool = False) -> None:
        """Expire old pending holds and unused approvals, drop finished
        grants, keep a bounded history."""
        now = self.clock()
        if not force and now - self._swept < 2.0:
            return
        self._swept = now
        with self._db(write=True) as db:
            db.execute("UPDATE holds SET state='expired' WHERE state='pending' AND expires<?",
                       (now,))
            db.execute("UPDATE holds SET state='expired' WHERE state='approved' "
                       "AND valid_until<?", (now,))
            db.execute("DELETE FROM grants WHERE until<?", (now,))
            db.execute("DELETE FROM holds WHERE state!='pending' AND id NOT IN (SELECT id FROM "
                       "holds WHERE state!='pending' ORDER BY created DESC LIMIT ?)",
                       (HISTORY_KEEP,))

    # -- writes ------------------------------------------------------------
    def create(self, *, principal: str, cap: str, tier: str, args_hash: str, label: str = "",
               via: tuple = (), tags: tuple = (), target_id: str | None = None,
               target_name: str = "", is_hub: bool = False, args: Any = None,
               reason: str = "", rule: str | None = None, kind: str = "default",
               ttl: float = 600.0) -> tuple[dict, bool]:
        """A new pending hold, or the pending one for the identical call.
        Returns ``(hold, created)``."""
        self.sweep()
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE state='pending' AND principal=? AND cap=? "
                           "AND target_id IS ? AND args_hash=? AND expires>=? "
                           "ORDER BY created LIMIT 1",
                           (principal, cap, target_id, args_hash, now)).fetchone()
            if r is not None:
                return _row(r), False
            hid = "h" + secrets.token_hex(12)
            db.execute(
                "INSERT INTO holds(id, state, kind, principal, principal_label, via_json, cap, "
                "tier, tags_json, target_id, target_name, is_hub, args_json, args_hash, reason, "
                "rule, created, expires) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (hid, kind, principal, label or "", json.dumps(list(via)), cap, tier,
                 json.dumps(list(tags)), target_id, target_name or "", int(bool(is_hub)),
                 json.dumps(args, ensure_ascii=False, default=str), args_hash, reason, rule,
                 now, now + ttl))
            names.assign(db, "hold", hid)
            r = db.execute("SELECT * FROM holds WHERE id=?", (hid,)).fetchone()
        return self._named([_row(r)])[0], True

    def decide(self, hold_id: str, approve: bool, by: str, *, scope: str = "call",
               note: str = "", approval_secs: float = 600.0,
               grant_secs: float = 900.0, relay: dict | None = None) -> dict:
        """Approve or deny a pending hold. ``scope="spread"`` (spread holds
        only) also lets the principal reach more machines for
        ``grant_secs``. Raises :class:`HoldError` unless the hold is pending.
        ``relay`` (an agent relaying the person's decision, rook/hub/relay.py):
        the audit row is written in the same transaction."""
        if scope not in SCOPES:
            raise HoldError(f"scope must be one of {', '.join(SCOPES)}")
        self.sweep(force=True)
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE id=?", (str(hold_id),)).fetchone()
            if r is None:
                raise HoldError("no such hold")
            if r["state"] != "pending":
                raise HoldError(f"hold {hold_id} is {r['state']}, not pending")
            if scope == "spread" and (not approve or r["kind"] != "spread"):
                raise HoldError("the spread scope is for approving spread holds")
            db.execute("UPDATE holds SET state=?, decided_by=?, decided_at=?, scope=?, "
                       "valid_until=?, note=? WHERE id=? AND state='pending'",
                       ("approved" if approve else "denied", by, now, scope,
                        now + approval_secs if approve else None, str(note or "")[:500],
                        r["id"]))
            if approve and scope == "spread":
                db.execute("INSERT OR REPLACE INTO grants(principal, kind, until, created, "
                           "created_by, hold_id) VALUES (?, 'spread', ?, ?, ?, ?)",
                           (r["principal"], now + grant_secs, now, by, r["id"]))
            if relay is not None:
                self._relay_insert(db, {**relay, "kind": relay.get("kind") or "hold",
                                        "item": r["id"],
                                        "decision": "approve" if approve else "deny"})
            r = db.execute("SELECT * FROM holds WHERE id=?", (r["id"],)).fetchone()
        return self._named([_row(r)])[0]

    def consume_match(self, principal: str, cap: str, target_id: str | None,
                      args_hash: str) -> dict | None:
        """Use up an approval for this identical call, if one is unused."""
        now = self.clock()
        with self._db(write=True) as db:
            r = db.execute("SELECT * FROM holds WHERE state='approved' AND principal=? AND cap=? "
                           "AND target_id IS ? AND args_hash=? AND valid_until>=? "
                           "ORDER BY decided_at LIMIT 1",
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
                             "AND state='approved' AND valid_until>=?", (now, hold_id, now))
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
        d = self._named([_row(r)])[0]
        if d["state"] == "pending" and d["expires"] < self.clock():
            d["state"] = "expired"
        return d

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
        now = self.clock()
        with self._db() as db:
            return int(db.execute("SELECT count(*) FROM holds WHERE state='pending' "
                                  "AND expires>=?", (now,)).fetchone()[0])

    def active_grant(self, principal: str) -> float | None:
        now = self.clock()
        with self._db() as db:
            r = db.execute("SELECT until FROM grants WHERE principal=? AND kind='spread' "
                           "AND until>=?", (principal, now)).fetchone()
        return float(r[0]) if r else None

    def grants(self) -> list[dict]:
        now = self.clock()
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM grants WHERE until>=? ORDER BY until", (now,)).fetchall()]

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


# -- spread ---------------------------------------------------------------------

class SpreadTracker:
    """Distinct machines each principal reached recently (in memory)."""

    MAX_PRINCIPALS = 10000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._seen: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def _fresh(self, key: str, window: float, now: float) -> dict[str, float]:
        seen = self._seen.get(key) or {}
        return {t: at for t, at in seen.items() if now - at <= window}

    def count_with(self, key: str, target: str, window: float) -> int:
        """How many distinct machines ``key`` will have reached in the window
        if this call to ``target`` goes ahead."""
        now = self.clock()
        with self._lock:
            seen = self._fresh(key, window, now)
            self._seen[key] = seen
        return len(seen) + (0 if target in seen else 1)

    def record(self, key: str, target: str) -> None:
        now = self.clock()
        with self._lock:
            self._seen.setdefault(key, {})[target] = now
            if len(self._seen) > self.MAX_PRINCIPALS:
                oldest = min(self._seen, key=lambda k: max(self._seen[k].values(), default=0))
                self._seen.pop(oldest, None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


# -- the glue the authorizer uses ---------------------------------------------------

_PROCEEDS = ("allow", "would_deny", "off", "error_allow")


class Approvals:
    """Queue + settings + spread tracker + operator contact for one hub
    process (owned by its :class:`rook.hub.authz.Authorizer`)."""

    def __init__(self, path: str, config: "ConfigSource | Config | None" = None,
                 record: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if isinstance(config, Config):
            config = ConfigSource(fixed=config)
        self.config_source = config or ConfigSource()
        self.queue = ApprovalQueue(path, clock=clock)
        self.spread = SpreadTracker(clock=monotonic)
        self._record = record
        self._monotonic = monotonic
        from .approval_contact import Contact
        self.contact = Contact(self)

    def config(self) -> Config:
        return self.config_source.get()

    def attach_client(self, client: Any) -> None:
        """The band client the operator is contacted through, and the roster
        the hub-host detection reads."""
        self.contact.client = client

    # -- hub-host workers (permissions 3.11) -------------------------------------
    def _client(self) -> Any:
        return self.contact.client

    def _hub_host_id(self) -> str | None:
        """The host id fact of the hub's own node, so we can spot workers that
        run on the hub's machine (they can edit approvals.db, policy.json and
        settings directly, so their exec+ calls are held)."""
        client = self._client()
        node = getattr(client, "_local", None)
        facts = getattr(getattr(node, "facts", None), "hw", None) or {}
        hid = facts.get("host_id")
        return str(hid) if hid else None

    def hub_host_workers(self, cfg: "Config | None" = None) -> dict:
        """What the Approvals page shows: the live workers treated as running
        on the hub's machine, how each was decided, and any detection gap."""
        cfg = cfg or self.config()
        sel = [s.lower() for s in cfg.hub_host_workers]
        if "off" in sel:
            return {"mode": "off", "workers": [], "note": "hub-host holds are off"}
        explicit = {s for s in sel if s and s != "auto"}
        auto = "auto" in sel or not explicit
        client = self._client()
        roster = getattr(client, "workers", {}) or {}
        hub_id = self._hub_host_id() if auto else None
        out = []
        for wid, w in roster.items():
            name = str(w.get("name") or wid)
            if "is_hub" in (w.get("roles") or ()):
                continue
            why = None
            if name.lower() in explicit or wid.lower() in explicit:
                why = "named in approvals.hub_host_workers"
            elif auto and hub_id and (w.get("facts") or {}).get("host_id") == hub_id:
                why = "same machine id as the hub"
            if why:
                out.append({"worker": name, "id": wid, "why": why})
        note = None
        if auto and hub_id is None:
            note = ("the hub announces no machine id, so co-located workers can't be "
                    "detected; name them in approvals.hub_host_workers")
        elif auto and hub_id and not out:
            note = "no live worker runs on the hub's machine"
        return {"mode": "auto" if auto else "list", "workers": out, "note": note,
                "detected": hub_id is not None}

    def _is_hub_host(self, target: Any, cfg: Config) -> bool:
        sel = [s.lower() for s in cfg.hub_host_workers]
        if "off" in sel or target is None or getattr(target, "is_rook", False):
            return False
        explicit = {s for s in sel if s and s != "auto"}
        name = str(getattr(target, "name", "") or "").lower()
        tid = str(getattr(target, "id", "") or "").lower()
        if name in explicit or tid in explicit:
            return True
        if "auto" in sel or not explicit:
            hub_id = self._hub_host_id()
            return bool(hub_id and (getattr(target, "facts", {}) or {}).get("host_id") == hub_id)
        return False

    # -- deciding to hold (synchronous, on the call path) ------------------------
    def apply(self, d: Any, p: Any, policy: Any, target: Any) -> Any:
        """Add the approval layer to a policy decision ``d`` for principal
        ``p``. Returns ``d``, possibly turned into a hold (or a hold turned
        into an allow where holds do not apply)."""
        cfg = self.config()
        d.target_is_hub = bool(getattr(target, "is_rook", False))
        if not cfg.enabled:
            # Off: no holds at all, operator hold rules included.
            return self._unhold(d, "approvals off") if d.held else d
        if d.denied:
            return d  # a deny always beats a hold
        if not holdable(p, cfg.principals):
            return self._unhold(d, "never held") if d.held else d
        count = countable(d.cap, d.tier, d.tags, target)
        if count:
            d.spread_target = str(target.id)
        if d.held:
            return d  # an operator hold rule or cell
        best = default_score(cfg.hold, d.cap, d.tier, d.tags)
        if best is not None and not own_resource.get():
            rule = policy.best_rule(p, d.cap, d.tier, d.tags, target)
            if not (rule is not None and rule[1].action == "allow" and rule[0] >= (0, best[0], 0)):
                return dataclasses.replace(d, decision="hold", hold_kind="default",
                                           hold_reason=_reason(best[1]))
        # Exec and above on a worker that runs on the hub's own machine: that
        # worker can rewrite the hub's files (approvals.db, policy.json,
        # settings), so it is a hold unless an operator allow rule names it.
        if core_authz.tier_rank(d.tier) >= core_authz.tier_rank("exec") \
                and not own_resource.get() and self._is_hub_host(target, cfg):
            rule = policy.best_rule(p, d.cap, d.tier, d.tags, target)
            if not (rule is not None and rule[1].action == "allow" and rule[0] >= (4, 0, 0)):
                return dataclasses.replace(d, decision="hold", hold_kind="hub-host",
                                           hold_reason="runs on the hub's machine")
        if count and cfg.spread_max > 0:
            n = self.spread.count_with(p.id, str(target.id), cfg.spread_window_secs)
            if n > cfg.spread_max and self.queue.active_grant(p.id) is None:
                minutes = max(1, round(cfg.spread_window_secs / 60))
                return dataclasses.replace(
                    d, decision="hold", hold_kind="spread",
                    hold_reason=f"spread: {_ordinal(n)} machine in {minutes} minutes")
        if d.decision in _PROCEEDS:
            self.proceeded(d)
        return d

    @staticmethod
    def _unhold(d: Any, why: str) -> Any:
        return dataclasses.replace(d, decision="allow", hold_kind="", hold_reason="",
                                   reason=(d.reason + "; " if d.reason else "") + f"hold ignored: {why}")

    def proceeded(self, d: Any) -> None:
        """A call is going ahead: count its machine for the spread limit."""
        if getattr(d, "spread_target", ""):
            self.spread.record(d.principal, d.spread_target)

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
                                         digest, max(cfg.approval_secs, DENIAL_ECHO_SECS))
        if denied is not None:
            return self._denied(d, denied)
        hold, created = await asyncio.to_thread(
            self.queue.create, principal=d.principal, cap=d.cap, tier=d.tier, args_hash=digest,
            label=label, via=tuple(d.via), tags=tuple(d.tags), target_id=d.target,
            target_name=d.target_name, is_hub=bool(d.target_is_hub),
            args=display_args(source, d.cap), reason=d.hold_reason, rule=d.rule,
            kind=d.hold_kind or "default", ttl=cfg.approval_secs)
        if created:
            self.journal_hold(hold)
            try:
                self.contact.hold_created(hold)
            except Exception:  # noqa: BLE001 - contacting the operator never blocks a call
                log.exception("approvals: contacting the operator failed")
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

    def _approved(self, d: Any, row: dict) -> Any:
        out = dataclasses.replace(d, decision="allow", rule=f"approval:{row['id']}",
                                  hold_id=row["id"], decided_by=str(row.get("decided_by") or ""),
                                  reason=f"approved by {row.get('decided_by')}")
        self.proceeded(out)
        return out

    def held_reply(self, d: Any, hold: dict, cfg: Config | None = None) -> dict:
        cfg = cfg or self.config()
        where = d.target_name or d.target or "the band"
        minutes = max(1, round(cfg.approval_secs / 60))
        why = f" ({hold.get('reason')})" if hold.get("reason") else ""
        named = f", {hold['name']}" if hold.get("name") else ""
        msg = (f"Held for approval (hold {hold['id']}{named}): {d.cap} on {where}{why}. Ask the user "
               f"to approve it on the dashboard's Approvals page, then repeat the same call; "
               f"an approval covers one identical call (same cap, worker and args) for "
               f"{minutes} minutes. Do not try to get around the hold another way.")
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
        cfg = self.config()
        row = self.queue.decide(hold_id, approve, by, scope=scope, note=note,
                                approval_secs=cfg.approval_secs,
                                grant_secs=cfg.spread_grant_secs, relay=relay)
        detail = {"hold": row["id"], "decision": row["state"], "scope": scope,
                  "cap": row["cap"], "principal": row["principal"],
                  "worker": row["target_name"] or row["target_id"]}
        if heard is not None:
            detail["heard"] = heard[:300]
        if scope == "spread":
            detail["grant_secs"] = cfg.spread_grant_secs
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


def public(hold: dict, cfg: Config | None = None) -> dict:
    """A hold as the Approvals page shows it."""
    from .approval_contact import voice_eligible
    out = {k: hold.get(k) for k in ("id", "name", "state", "kind", "principal", "principal_label", "via",
                                    "cap", "tier", "tags", "target_id", "target_name", "args",
                                    "reason", "rule", "created", "expires", "decided_by",
                                    "decided_at", "scope", "valid_until", "consumed_at", "note")}
    out["worker"] = hold.get("target_name") or hold.get("target_id") or ""
    if hold.get("relayed"):
        out["relayed"] = hold["relayed"]
        out["relayed_text"] = relay_text(hold["relayed"])
    if cfg is not None:
        out["voice_ok"] = voice_eligible(hold, cfg)
    return out
