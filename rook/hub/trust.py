"""The trust map: what agents may do to what, enforced by the hub
(docs/design/trust.md).

One human-readable, versioned file in the hub data directory, ``trust.json``,
beside ``policy.json`` and ``approvals.db``, and an append-only history,
``trust-history.jsonl``. Both hub processes read the same file.

**Entries.** A project, host, repository or resource, with a **level**:

* ``sacred``: changed only on the person's direct instruction. Agents may
  read, propose and prepare;
* ``guarded``: agents vote, act, then tell. Until votes exist (H6), an
  ask-first hold the person approves;
* ``ship-it``: agents just go.

Each entry says what it covers with selectors (``worker:<name glob>``,
``cap:<cap glob>``, ``repo:<owner>/<name>[#<branch glob>]``,
``project:<slug>``) and carries who set it, when, and the person's exact
words (the ``relayed_by`` / ``closed_by`` pattern).

**Rulings.** The tier of an action is ``MATRIX[level][kind]``: the action's
kind (:func:`classify`: ``read``, ``prepare``, ``change``, ``admin``) against
the strictest level of the entries it touches. Targets no entry covers take
the map's ``unlisted`` level (``balanced`` unless the person tightened it).
An active **exception** (scoped, with an expiry) may lower the tier for the
entries and targets it names, never for the entries it excludes. Every
ruling carries a citation (:attr:`Ruling.cite`): the entry, its version and
the person's words.

**Who changes it.** Only a person, on the dashboard, loosens anything.
Agents propose (``proposals``). The house agent may tighten (raise a level,
add a guarded or sacred entry, end an exception early); that is logged and
shown on the Trust page until the person acknowledges it. Nothing relays a
change. ``system`` (the hub machine's own CLI, like editing policy.json) may
seed it.

**Borrowed state.** ``baseline`` records a sacred host's normal state, one
resource at a time ("kaiju runs Strata A on GPU 1"), with the exact calls that
stop and restore it. A scoped exception may name such resources
(``resource:<id>`` targets): only the house agent's declared stop or restore
call for that resource (:data:`borrowing`) matches, never anything else on
the host. Every change made that way is a ``ledger`` lease: what changed,
why, the person's words, how to restore it, until when (B3,
docs/design/onboarding.md).

**Tool grants.** ``grants`` lists the MCP servers a role's persistent Claude
Code sessions were given (``grant_tool`` / ``revoke_tool``,
docs/design/persistent-agents.md 3.6). A grant is guarded: the person makes it
(dashboard), or the house agent makes it because the person asked for it in
the request it is working on (``Actor.via == "person-request"``, with their
words); an agent's own idea is a proposal. A revoke tightens, so the house
agent may always revoke.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime as _dt
import fnmatch
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("rook.hub.trust")

FORMAT = "rook-trust/1"
LEVELS = ("ship-it", "guarded", "sacred")
#: What a target no entry covers is treated as. ``balanced``: changes go,
#: admin and destructive actions ask (the hub's long-standing default).
UNLISTED_LEVELS = ("ship-it", "balanced", "guarded", "sacred")
KINDS = ("read", "prepare", "change", "admin")
TIERS = ("go", "ask", "person")
ENTRY_KINDS = ("project", "host", "repo", "resource", "devices")

#: The tier of an action: ``MATRIX[level][kind]``. Reads and preparing (the
#: hub's own records: tasks, knowledge, chat, huddles) are always free; that
#: is how agents "read, propose and prepare" even on sacred things.
MATRIX: dict[str, dict[str, str]] = {
    "ship-it":  {"read": "go", "prepare": "go", "change": "go", "admin": "go"},
    "balanced": {"read": "go", "prepare": "go", "change": "go", "admin": "ask"},
    "guarded":  {"read": "go", "prepare": "go", "change": "ask", "admin": "ask"},
    "sacred":   {"read": "go", "prepare": "go", "change": "person", "admin": "person"},
}
TIER_WORDS = {
    "go": "agents just go",
    "ask": "ask first (until votes exist, a person approves)",
    "person": "only on the person's direct instruction",
}

#: Hub caps that write the band's own records: planning and proposing, never
#: a change to a machine or a project's code. Free on any level.
PREPARE_CAPS = ("task.write", "knowledge.write", "chat.write", "chat.presence", "huddle.write",
                "huddle.post", "huddle.start", "memory.remember", "home.ask", "role.ask",
                "trust.propose", "trust.tighten", "home.settle", "home.playbook_edit",
                "home.overturn", "huddle.pin", "huddle.vote", "home.session_note")

WORDS_MAX, WHO_MAX, TEXT_MAX = 1000, 120, 2000
PROPOSALS_MAX = 50
HISTORY_TAIL = 200
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SEL = re.compile(r"^(worker|cap|repo|project|resource):(.+)$")

#: Set by the house agent around one declared stop or restore call of a
#: baseline resource (``{"resource", "cap", "worker", "principal"}``); the
#: approval layer passes the resource to the ruling only when the call it is
#: ruling on is that call (same cap, worker and principal). In-process only:
#: no agent can set it.
borrowing: contextvars.ContextVar[dict | None] = contextvars.ContextVar("rook_trust_borrowing",
                                                                        default=None)


class TrustError(ValueError):
    """A change refused; the text says why and what to do instead."""


def rank(level: str) -> int:
    """Strictness: ship-it 0, balanced 1, guarded 2, sacred 3."""
    return UNLISTED_LEVELS.index(level) if level in UNLISTED_LEVELS else len(UNLISTED_LEVELS)


def tier_rank(tier: str) -> int:
    return TIERS.index(tier) if tier in TIERS else len(TIERS)


# -- what an action is ------------------------------------------------------------

def classify(cap: str, tier: str, tags: tuple | list = (), on_hub: bool = False) -> str:
    """The kind of an action: ``read`` (a read-tier cap), ``prepare`` (a hub
    cap that writes the band's own records), ``admin`` (admin tier or tagged
    destructive) or ``change`` (everything else)."""
    cap = str(cap or "")
    if (tier or "exec") == "read":
        return "read"
    from ..core.authz import is_status_read
    if is_status_read(cap) and tier != "admin" and "destructive" not in (tags or ()):
        # Battery, device info: a read whatever tier an older app still announces, but
        # never something a worker announces as admin or destructive under that name.
        return "read"
    if on_hub and cap in PREPARE_CAPS:
        return "prepare"
    if tier == "admin" or "destructive" in (tags or ()):
        return "admin"
    return "change"


def norm_repo(text: str) -> str:
    """``https://github.com/Bake-Ware/rook.git`` -> ``bake-ware/rook``."""
    s = str(text or "").strip().lower()
    s = re.sub(r"^(https?://|ssh://|git@)", "", s)
    s = re.sub(r"^github\.com[:/]", "", s)
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return s


@dataclass(frozen=True)
class Ctx:
    """What an action touches."""
    cap: str = ""
    worker: str = ""
    worker_id: str = ""
    repo: str = ""
    branch: str = ""
    project: str = ""
    resource: str = ""


def _glob(pat: str, value: str) -> bool:
    return bool(value) and fnmatch.fnmatchcase(value.lower(), pat.lower())


def selector_matches(sel: str, ctx: Ctx) -> bool:
    m = _SEL.match(str(sel or "").strip())
    if not m:
        return False
    kind, pat = m.group(1), m.group(2).strip()
    if kind == "worker":
        return _glob(pat, ctx.worker) or (bool(ctx.worker_id) and pat == ctx.worker_id)
    if kind == "cap":
        return _glob(pat, ctx.cap)
    if kind == "project":
        return _glob(pat, ctx.project)
    if kind == "resource":
        return bool(ctx.resource) and pat.lower() == ctx.resource.lower()
    if kind == "repo":
        repo, _, branch = pat.partition("#")
        if not ctx.repo or norm_repo(repo) != norm_repo(ctx.repo):
            return False
        return not branch or _glob(branch, ctx.branch or "")
    return False


def check_selector(sel: Any) -> str:
    s = " ".join(str(sel or "").split())
    if not _SEL.match(s):
        raise TrustError(f"target {sel!r}: one of worker:<name>, cap:<cap glob>, "
                         "repo:<owner>/<name>[#<branch>], project:<slug>, resource:<baseline id>")
    return s


# -- time -------------------------------------------------------------------------

def parse_when(value: Any) -> float:
    """A timestamp, or an ISO 8601 time (with an offset; a bare one is UTC)."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    s = str(value or "").strip()
    if not s:
        raise TrustError("a time is required (ISO 8601 with an offset, e.g. "
                         "2026-10-23T12:00:00-05:00)")
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise TrustError(f"{s!r} is not an ISO 8601 time") from None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


# -- who --------------------------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    """Who is changing the map: ``person`` (the dashboard), ``house`` (the
    house agent: tighten only), ``agent`` (propose only) or ``system`` (the
    hub machine's CLI). ``words`` are the person's exact words."""
    kind: str
    who: str
    principal: str = ""
    words: str = ""
    session: str = ""
    via: str = ""
    #: How a spoken decision was checked (``via == "voice"``): the voiceprint
    #: score, the read-back the person confirmed and the challenge word.
    evidence: tuple = ()

    def stamp(self, now: float) -> dict:
        out = {"who": self.who[:WHO_MAX], "kind": self.kind, "when": _iso(now), "at": now,
               "words": self.words[:WORDS_MAX]}
        for k in ("principal", "session", "via"):
            v = getattr(self, k)
            if v:
                out[k] = str(v)[:TEXT_MAX]
        if self.evidence:
            out["evidence"] = {str(k)[:40]: (v if isinstance(v, (int, float, bool)) or v is None
                                             else str(v)[:TEXT_MAX]) for k, v in self.evidence}
        return out


def _clean_words(text: Any) -> str:
    return " ".join(str(text or "").split())[:WORDS_MAX]


# -- the ruling -------------------------------------------------------------------

@dataclass
class Ruling:
    tier: str
    kind: str
    level: str
    version: int
    entries: list = field(default_factory=list)   # brief entries matched, strictest first
    exception: dict | None = None
    unlisted: bool = False
    base_tier: str = ""          # the tier before an exception lowered it

    @property
    def entry(self) -> dict | None:
        return self.entries[0] if self.entries else None

    @property
    def rule(self) -> str:
        """``trust:<entry>@v<version>`` (``trust:unlisted@v..`` for a gap),
        plus ``+<exception>`` when an exception lowered it."""
        head = self.entry["id"] if self.entry else "unlisted"
        out = f"trust:{head}@v{self.version}"
        if self.exception:
            out += f"+{self.exception['id']}"
        return out

    @property
    def cite(self) -> str:
        """One line a person and an agent both read: the entry, its level,
        the person's words, and any exception that changed the outcome."""
        e = self.entry
        if e:
            by = e.get("set_by") or {}
            words = f': "{by["words"]}"' if by.get("words") else ""
            when = (by.get("when") or "")[:10]
            head = (f'trust map v{self.version}, entry "{e["id"]}" ({e["level"]}), set by '
                    f'{by.get("who") or "?"}{" " + when if when else ""}{words}')
        else:
            head = f"trust map v{self.version}: no entry covers this, so unlisted ({self.level})"
        base = self.base_tier or self.tier
        out = f"{head}. {self.kind} -> {base}: {TIER_WORDS.get(base, base)}"
        if self.exception:
            x = self.exception
            by = x.get("set_by") or {}
            words = f': "{by["words"][:200]}"' if by.get("words") else ""
            out += (f'; lowered to {self.tier} by exception "{x["id"]}" until '
                    f'{x.get("expires") or "?"} ({by.get("who") or "?"}{words})')
        return out

    def public(self) -> dict:
        return {"tier": self.tier, "base_tier": self.base_tier or self.tier, "kind": self.kind,
                "level": self.level,
                "version": self.version, "rule": self.rule, "cite": self.cite,
                "entries": [e["id"] for e in self.entries], "unlisted": self.unlisted,
                "exception": self.exception["id"] if self.exception else None}


def _brief(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "title", "level", "set_by") if e.get(k) is not None}


def empty_map() -> dict:
    return {"format": FORMAT, "version": 0, "updated": 0, "unlisted": "balanced",
            "unlisted_set_by": None, "entries": [], "exceptions": [], "baseline": [], "ledger": [],
            "proposals": [], "notices": [], "grants": []}


def rule_map(doc: dict, ctx: Ctx, tier: str, tags: tuple | list = (), on_hub: bool = False,
             now: float | None = None) -> Ruling:
    """The ruling for one action on ``ctx`` under the map ``doc``."""
    now = time.time() if now is None else now
    kind = classify(ctx.cap, tier, tags, on_hub)
    matched = [e for e in doc.get("entries") or ()
               if e.get("level") in LEVELS
               and any(selector_matches(s, ctx) for s in e.get("targets") or ())]
    matched.sort(key=lambda e: -rank(e["level"]))
    version = int(doc.get("version") or 0)
    if matched:
        level = matched[0]["level"]
    else:
        level = doc.get("unlisted") if doc.get("unlisted") in UNLISTED_LEVELS else "balanced"
    out = Ruling(tier=MATRIX[level][kind], kind=kind, level=level, version=version,
                 entries=[_brief(e) for e in matched], unlisted=not matched)
    if out.tier == "go":
        return out
    ids = {e["id"] for e in matched}
    for x in doc.get("exceptions") or ():
        if not exception_active(x, now):
            continue
        if ids & set(x.get("excludes") or ()):
            continue
        applies = x.get("applies_to") or ["*"]
        if "*" not in applies and not (ids & set(applies)):
            continue
        if x.get("targets") and not any(selector_matches(s, ctx) for s in x["targets"]):
            continue
        if kind not in (x.get("kinds") or ("change", "admin")):
            continue
        lowered = x.get("tier") if x.get("tier") in TIERS else "go"
        if tier_rank(lowered) < tier_rank(out.tier):
            out.base_tier = out.base_tier or out.tier
            out.tier = lowered
            out.exception = {k: x.get(k) for k in ("id", "title", "expires", "set_by")}
        if out.tier == "go":
            break
    return out


def exception_active(x: dict, now: float) -> bool:
    return not x.get("ended") and float(x.get("expires_at") or 0) > now


# -- validation -------------------------------------------------------------------

def _check_id(value: Any, what: str) -> str:
    s = str(value or "").strip().lower()
    if not _ID.match(s):
        raise TrustError(f"{what}: a short id (letters, digits, . _ -), like kaiju")
    return s


def clean_entry(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise TrustError("entry: an object {id, title, kind, level, targets, description}")
    level = str(raw.get("level") or "").strip().lower()
    if level not in LEVELS:
        raise TrustError("level: one of " + ", ".join(LEVELS))
    kind = str(raw.get("kind") or "resource").strip().lower()
    if kind not in ENTRY_KINDS:
        raise TrustError("kind: one of " + ", ".join(ENTRY_KINDS))
    targets = raw.get("targets") or []
    if isinstance(targets, str):
        targets = [t for t in re.split(r"[\n,]", targets) if t.strip()]
    targets = [check_selector(t) for t in targets]
    if not targets:
        raise TrustError("targets: at least one (worker:kaiju, repo:Bake-Ware/rook#master, ...)")
    return {"id": _check_id(raw.get("id"), "id"), "title": str(raw.get("title") or raw.get("id"))
            .strip()[:120], "kind": kind, "level": level, "targets": sorted(set(targets)),
            "description": str(raw.get("description") or "").strip()[:TEXT_MAX]}


def clean_exception(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise TrustError("exception: an object {id, title, applies_to, excludes, targets, kinds, "
                         "tier, expires}")
    applies = raw.get("applies_to") or ["*"]
    applies = [str(a).strip().lower() for a in (applies if isinstance(applies, list) else [applies])
               if str(a).strip()]
    excludes = [str(a).strip().lower() for a in raw.get("excludes") or [] if str(a).strip()]
    kinds = [k for k in (raw.get("kinds") or ["change", "admin"]) if k in KINDS]
    tier = str(raw.get("tier") or "go").strip().lower()
    if tier not in TIERS:
        raise TrustError("tier: one of " + ", ".join(TIERS))
    expires_at = parse_when(raw.get("expires"))
    out = {"id": _check_id(raw.get("id"), "id"), "title": str(raw.get("title") or raw.get("id"))
           .strip()[:120], "applies_to": applies or ["*"], "excludes": excludes,
           "targets": [check_selector(t) for t in raw.get("targets") or []],
           "kinds": kinds or ["change", "admin"], "tier": tier,
           "expires": str(raw.get("expires")) if not isinstance(raw.get("expires"), (int, float))
           else _iso(expires_at), "expires_at": expires_at,
           "description": str(raw.get("description") or "").strip()[:TEXT_MAX]}
    if raw.get("project"):
        out["project"] = _check_id(raw["project"], "project")
    return out


def clean_call(raw: Any, what: str) -> dict:
    """``{worker, cap, args}``: one exact call (a baseline resource's stop or
    restore)."""
    if not isinstance(raw, dict):
        raise TrustError(f"{what}: an object {{worker, cap, args}}")
    worker, cap = str(raw.get("worker") or "").strip(), str(raw.get("cap") or "").strip()
    if not worker or not re.match(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", cap):
        raise TrustError(f"{what}: worker and cap are required (e.g. kaiju, shell.exec)")
    args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    if len(json.dumps(args, default=str)) > TEXT_MAX:
        raise TrustError(f"{what}.args: too long")
    return {"worker": worker[:WHO_MAX], "cap": cap, "args": args}


def clean_baseline(raw: Any) -> dict:
    """A sacred host's normal state, one resource: what normal is, and the
    exact calls that stop and restore it."""
    if not isinstance(raw, dict):
        raise TrustError("baseline: an object {id, entry, title, normal, stop, restore}")
    out = {"id": _check_id(raw.get("id"), "id"),
           "entry": _check_id(raw.get("entry"), "entry (the host's trust entry id)"),
           "title": str(raw.get("title") or raw.get("id")).strip()[:120]}
    normal = " ".join(str(raw.get("normal") or "").split())
    if not normal:
        raise TrustError("baseline.normal: what normal looks like (\"kaiju runs Strata A on "
                         "GPU 1\")")
    out["normal"] = normal[:TEXT_MAX]
    out["stop"] = clean_call(raw.get("stop"), "baseline.stop")
    out["restore"] = clean_call(raw.get("restore"), "baseline.restore")
    if raw.get("check"):
        out["check"] = clean_call(raw.get("check"), "baseline.check")
    return out


def clean_lease(raw: Any) -> dict:
    """A borrowed-state ledger row (B3 builds the flows): what changed away
    from a sacred resource's normal state, why, how to restore it, until when."""
    if not isinstance(raw, dict):
        raise TrustError("lease: an object {id, resource, what, why, restore, until?}")
    out = {"id": _check_id(raw.get("id"), "id"),
           "resource": _check_id(raw.get("resource"), "resource (an entry id)")}
    for k in ("what", "why", "restore"):
        v = str(raw.get(k) or "").strip()
        if not v:
            raise TrustError(f"lease.{k} is required")
        out[k] = v[:TEXT_MAX]
    if raw.get("exception"):
        out["exception"] = _check_id(raw["exception"], "exception")
    if raw.get("project"):
        out["project"] = _check_id(raw["project"], "project")
    if raw.get("baseline"):
        out["baseline"] = _check_id(raw["baseline"], "baseline")
    if raw.get("restore_call"):
        out["restore_call"] = clean_call(raw["restore_call"], "lease.restore_call")
    if raw.get("words"):
        out["words"] = _clean_words(raw["words"])
    if raw.get("until"):
        out["until_at"] = parse_when(raw["until"])
        out["until"] = str(raw["until"])
    out["state"] = "open"
    return out


# -- the store --------------------------------------------------------------------

OPS = ("set_entry", "delete_entry", "set_unlisted", "add_exception", "end_exception",
       "add_lease", "close_lease", "set_baseline", "decide_proposal", "ack", "confirm",
       "grant_tool", "revoke_tool")
#: ``Actor.via`` for a grant the house agent makes because the person asked for
#: it in the request it is working on (docs/design/persistent-agents.md 3.6).
PERSON_REQUEST = "person-request"


def clean_grant(raw: Any) -> dict:
    """A tool grant: an MCP server (by name) for a role's persistent sessions."""
    if not isinstance(raw, dict):
        raise TrustError("grant: {role, server}")
    role = str(raw.get("role") or "").strip()
    server = str(raw.get("server") or "").strip()
    if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", role):
        raise TrustError("grant role: a role id")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", server) or server in ("rook", "flow"):
        raise TrustError("grant server: an MCP server name (not rook or flow)")
    return {"id": f"{role}:{server}", "role": role, "server": server,
            "note": str(raw.get("note") or "")[:TEXT_MAX]}


class TrustStore:
    """``trust.json`` + ``trust-history.jsonl``: read with a short mtime cache
    (the call path does no I/O otherwise), written under a file lock with an
    atomic rename. A missing or broken file reads as an empty map (unlisted:
    balanced), which is the hub's behaviour before the map existed."""

    def __init__(self, path: str | None, *, check_every: float = 2.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.history_path = (os.path.join(os.path.dirname(path), "trust-history.jsonl")
                             if path else None)
        self._check_every = check_every
        self._clock = clock
        self._doc = empty_map()
        self._mtime: float | None = None
        self._checked = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._mem_history: list = []

    # -- reading ---------------------------------------------------------------
    def current(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if now - self._checked >= self._check_every or self._mtime is None:
                self._checked = now
                self._reload()
            return self._doc

    def _reload(self) -> None:
        if not self.path:
            return
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            if self._mtime is not None:
                self._doc, self._mtime = empty_map(), None
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict) or doc.get("format") != FORMAT:
                raise ValueError(f"not a {FORMAT} file")
            base = empty_map()
            base.update(doc)
            self._doc, self._mtime, self.error = base, mtime, None
        except Exception as e:  # noqa: BLE001 - keep the last good map
            self.error = f"{type(e).__name__}: {e}"
            self._mtime = mtime
            log.error("ROOK TRUST ALERT: %s unreadable, keeping the last good map: %s",
                      self.path, self.error)

    def rule(self, cap: str, tier: str, tags: tuple | list = (), *, worker: str = "",
             worker_id: str = "", on_hub: bool = False, repo: str = "", branch: str = "",
             project: str = "", resource: str = "", now: float | None = None) -> Ruling:
        ctx = Ctx(cap=cap, worker=worker, worker_id=worker_id or "", repo=repo, branch=branch,
                  project=project, resource=resource)
        return rule_map(self.current(), ctx, tier, tags, on_hub,
                        self._clock() if now is None else now)

    def history(self, limit: int = 50, entry: str = "") -> list[dict]:
        rows: list = []
        if self.history_path and os.path.exists(self.history_path):
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    with contextlib.suppress(ValueError):
                        rows.append(json.loads(line))
        else:
            rows = list(self._mem_history)
        if entry:
            rows = [r for r in rows if r.get("id") == entry]
        return list(reversed(rows[-max(1, int(limit)):]))

    # -- writing ---------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        if not self.path:
            with self._lock:
                yield
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        lock_path = self.path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows hubs
                pass
            yield
        finally:
            os.close(fd)

    def _read_fresh(self) -> dict:
        if not self.path:
            return copy.deepcopy(self._doc)
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict) or doc.get("format") != FORMAT:
                raise ValueError(f"not a {FORMAT} file")
        except FileNotFoundError:
            return empty_map()
        except Exception as e:  # noqa: BLE001 - never overwrite a map we can't read
            raise TrustError(f"the trust map on disk is unreadable ({e}); fix {self.path} by "
                             "hand first") from None
        base = empty_map()
        base.update(doc)
        return base

    def _write(self, doc: dict, history: list[dict]) -> None:
        if not self.path:
            self._doc = doc
            self._mem_history.extend(history)
            return
        tmp = f"{self.path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.write("\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.path)
        if history:
            with open(self.history_path, "a", encoding="utf-8") as f:
                for row in history:
                    f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        with self._lock:
            self._doc, self._checked = doc, 0.0
            self._mtime = None

    def change(self, op: str, data: dict, actor: Actor) -> dict:
        """Apply one change for ``actor`` (see the module doc for who may do
        what). Returns ``{version, op, id, before, after}``."""
        if op not in OPS:
            raise TrustError("op: one of " + ", ".join(OPS))
        if actor.kind not in ("person", "house", "system"):
            raise TrustError("only a person changes the trust map, on the dashboard's Trust page; "
                             "agents propose a change (rook_trust action=propose) and the house "
                             "agent may only tighten it")
        words = _clean_words(actor.words)
        if op not in ("ack",) and len(words) < 2:
            raise TrustError("words are required: the person's own words for this change "
                             "(who said what, as they said it)")
        data = data if isinstance(data, dict) else {}
        with self._locked():
            doc = self._read_fresh()
            now = self._clock()
            before, after, ident = self._apply(doc, op, data, actor, now)
            doc["version"] = int(doc.get("version") or 0) + 1
            doc["updated"] = now
            doc["updated_by"] = {"who": actor.who, "kind": actor.kind}
            if actor.kind == "house" and op != "ack":
                doc.setdefault("notices", []).append({
                    "version": doc["version"], "op": op, "id": ident, "who": actor.who,
                    "words": words, "at": now, "when": _iso(now)})
            row = {"version": doc["version"], "at": now, "when": _iso(now), "op": op, "id": ident,
                   "actor": actor.stamp(now), "words": words, "before": before, "after": after}
            self._write(doc, [row])
        return {"version": doc["version"], "op": op, "id": ident, "before": before,
                "after": after}

    # -- the operations --------------------------------------------------------
    def _apply(self, doc: dict, op: str, data: dict, actor: Actor, now: float):
        house = actor.kind == "house"
        if op == "set_entry":
            entry = clean_entry(data.get("entry") if "entry" in data else data)
            old = next((e for e in doc["entries"] if e["id"] == entry["id"]), None)
            if house:
                self._house_entry(old, entry)
            entry["set_by"] = actor.stamp(now)
            if actor.via:
                entry["confirmed"] = False
            elif actor.kind == "person":
                entry["confirmed"] = True
            entry["rev"] = int((old or {}).get("rev") or 0) + 1
            if old is not None:
                doc["entries"] = [entry if e["id"] == entry["id"] else e for e in doc["entries"]]
            else:
                doc["entries"].append(entry)
            return old, entry, entry["id"]
        if op == "delete_entry":
            ident = _check_id(data.get("id"), "id")
            old = next((e for e in doc["entries"] if e["id"] == ident), None)
            if old is None:
                raise TrustError(f"no entry {ident!r}")
            if house:
                raise TrustError("removing an entry can loosen the map: only the person does "
                                 "that, on the dashboard")
            doc["entries"] = [e for e in doc["entries"] if e["id"] != ident]
            return old, None, ident
        if op == "set_unlisted":
            level = str(data.get("level") or "").strip().lower()
            if level not in UNLISTED_LEVELS:
                raise TrustError("level: one of " + ", ".join(UNLISTED_LEVELS))
            old = doc.get("unlisted") or "balanced"
            if house and rank(level) <= rank(old):
                raise TrustError(f"unlisted is {old}; the house agent may only make it stricter")
            doc["unlisted"] = level
            doc["unlisted_set_by"] = actor.stamp(now)
            return {"unlisted": old}, {"unlisted": level}, "unlisted"
        if op == "add_exception":
            if house:
                raise TrustError("an exception loosens the map: only the person adds one")
            x = clean_exception(data.get("exception") if "exception" in data else data)
            if any(e["id"] == x["id"] for e in doc["exceptions"]):
                raise TrustError(f"exception {x['id']!r} exists; end it and add a new one")
            x["set_by"] = actor.stamp(now)
            doc["exceptions"].append(x)
            return None, x, x["id"]
        if op == "end_exception":
            ident = _check_id(data.get("id"), "id")
            x = next((e for e in doc["exceptions"] if e["id"] == ident), None)
            if x is None:
                raise TrustError(f"no exception {ident!r}")
            old = dict(x)
            x["ended"] = actor.stamp(now)
            return old, x, ident
        if op == "set_baseline":
            # Describing a host's normal state loosens nothing: an exception
            # (the person's) still has to name the resource before any of
            # its calls goes without the person.
            b = clean_baseline(data.get("baseline") if "baseline" in data else data)
            if not any(e["id"] == b["entry"] for e in doc["entries"]):
                raise TrustError(f"no entry {b['entry']!r}: a baseline belongs to a host's entry")
            doc.setdefault("baseline", [])
            old = next((r for r in doc["baseline"] if r["id"] == b["id"]), None)
            if old is not None and house and any(
                    x.get("ended") is None and f"resource:{b['id']}" in (x.get("targets") or ())
                    for x in doc["exceptions"]) and (old.get("stop"), old.get("restore")) != (
                    b["stop"], b["restore"]):
                raise TrustError(f"an exception names {b['id']}: changing its calls is the "
                                 "person's alone")
            b["set_by"] = actor.stamp(now)
            doc["baseline"] = [r for r in doc["baseline"] if r["id"] != b["id"]] + [b]
            return old, b, b["id"]
        if op == "add_lease":
            lease = clean_lease(data.get("lease") if "lease" in data else data)
            if not any(e["id"] == lease["resource"] for e in doc["entries"]):
                raise TrustError(f"no entry {lease['resource']!r} to borrow from")
            if any(r["id"] == lease["id"] for r in doc["ledger"]):
                raise TrustError(f"lease {lease['id']!r} exists")
            lease["authorized_by"] = actor.stamp(now)
            doc["ledger"].append(lease)
            return None, lease, lease["id"]
        if op == "close_lease":
            ident = _check_id(data.get("id"), "id")
            lease = next((r for r in doc["ledger"] if r["id"] == ident), None)
            if lease is None:
                raise TrustError(f"no lease {ident!r}")
            old = dict(lease)
            lease["state"] = "restored" if data.get("restored", True) else "closed"
            lease["closed_by"] = actor.stamp(now)
            return old, lease, ident
        if op == "grant_tool":
            g = clean_grant(data.get("grant") if "grant" in data else data)
            if house and actor.via != PERSON_REQUEST:
                raise TrustError("granting a tool is the person's call: the house agent grants "
                                 "only when the person asked for it; propose it instead")
            doc.setdefault("grants", [])
            old = next((r for r in doc["grants"] if r["id"] == g["id"] and not r.get("revoked")),
                       None)
            if old is not None:
                return old, old, g["id"]
            g["granted_by"] = actor.stamp(now)
            doc["grants"] = [r for r in doc["grants"] if r["id"] != g["id"]] + [g]
            return None, g, g["id"]
        if op == "revoke_tool":
            g = clean_grant(data.get("grant") if "grant" in data else data)
            doc.setdefault("grants", [])
            old = next((r for r in doc["grants"] if r["id"] == g["id"] and not r.get("revoked")),
                       None)
            if old is None:
                raise TrustError(f"{g['server']} is not granted to {g['role']}")
            before = dict(old)
            old["revoked"] = actor.stamp(now)
            return before, old, g["id"]
        if op == "decide_proposal":
            if actor.kind != "person":
                raise TrustError("only the person accepts or rejects a proposal")
            ident = str(data.get("id") or "")
            prop = next((p for p in doc["proposals"] if p["id"] == ident), None)
            if prop is None or prop.get("state") != "pending":
                raise TrustError(f"no pending proposal {ident!r}")
            old = dict(prop)
            accept = bool(data.get("accept"))
            if accept:
                inner_op, inner = prop["change"]["op"], prop["change"].get("data") or {}
                if inner_op not in OPS or inner_op in ("decide_proposal", "ack"):
                    raise TrustError(f"proposal {ident} has no change to apply")
                self._apply(doc, inner_op, inner, actor, now)
            prop["state"] = "accepted" if accept else "rejected"
            prop["decided_by"] = actor.stamp(now)
            return old, prop, ident
        if op == "ack":
            if actor.kind != "person":
                raise TrustError("only the person acknowledges the house agent's changes")
            upto = int(data.get("version") or doc.get("version") or 0)
            old = list(doc.get("notices") or ())
            doc["notices"] = [n for n in old if int(n.get("version") or 0) > upto]
            return {"notices": len(old)}, {"notices": len(doc["notices"])}, "notices"
        if op == "confirm":
            if actor.kind != "person":
                raise TrustError("only the person confirms an entry")
            ident = _check_id(data.get("id"), "id")
            entry = next((e for e in doc["entries"] if e["id"] == ident), None)
            if entry is None:
                raise TrustError(f"no entry {ident!r}")
            old = copy.deepcopy(entry)
            entry["confirmed"] = True
            entry["confirmed_by"] = actor.stamp(now)
            return old, entry, ident
        raise TrustError(f"unknown op {op}")  # pragma: no cover

    @staticmethod
    def _house_entry(old: dict | None, new: dict) -> None:
        """The house agent only tightens: a new entry must be guarded or
        sacred; an existing one may only rise in level, with the same or
        more targets."""
        if old is None:
            if new["level"] == "ship-it":
                raise TrustError("a ship-it entry loosens the map: only the person adds one; "
                                 "propose it instead")
            return
        if rank(new["level"]) < rank(old["level"]):
            raise TrustError(f"{old['id']} is {old['level']}: lowering it is the person's alone; "
                             "propose it instead")
        if not set(old.get("targets") or ()) <= set(new["targets"]):
            raise TrustError(f"dropping targets from {old['id']} loosens it: only the person "
                             "does that")

    def propose(self, change: dict, *, by: str, principal: str = "", why: str = "") -> dict:
        """An agent's proposed change, queued for the person on the Trust page.
        The change is checked now, so the person sees only applicable ones."""
        if not isinstance(change, dict) or change.get("op") not in OPS \
                or change.get("op") in ("decide_proposal", "ack", "confirm"):
            raise TrustError("change: {op, data} with op one of set_entry, delete_entry, "
                             "set_unlisted, add_exception, end_exception, add_lease, close_lease, "
                             "grant_tool, revoke_tool")
        why = _clean_words(why)
        if len(why) < 4:
            raise TrustError("why is required: what you saw that makes this change right "
                             "(e.g. the person approved the same thing 5 times)")
        probe = copy.deepcopy(self.current())
        self._apply(probe, change["op"], change.get("data") or {}, Actor("person", by, words=why),
                    self._clock())
        import secrets as _secrets
        with self._locked():
            doc = self._read_fresh()
            pending = [p for p in doc["proposals"] if p.get("state") == "pending"]
            if len(pending) >= PROPOSALS_MAX:
                raise TrustError(f"{PROPOSALS_MAX} proposals already wait for the person")
            now = self._clock()
            prop = {"id": "tp_" + _secrets.token_hex(4), "state": "pending",
                    "change": {"op": change["op"], "data": change.get("data") or {}},
                    "by": by[:WHO_MAX], "principal": principal[:WHO_MAX], "why": why,
                    "at": now, "when": _iso(now)}
            doc["proposals"].append(prop)
            doc["proposals"] = [p for p in doc["proposals"] if p.get("state") == "pending"] + \
                [p for p in doc["proposals"] if p.get("state") != "pending"][-HISTORY_TAIL:]
            row = {"version": doc.get("version") or 0, "at": now, "when": _iso(now),
                   "op": "propose", "id": prop["id"], "actor": {"who": by, "kind": "agent",
                                                                "principal": principal},
                   "words": why, "before": None, "after": prop}
            self._write(doc, [row])
        return prop

    # -- reading for people and agents ---------------------------------------------
    def summary(self, now: float | None = None) -> str:
        """A few lines agents get in context."""
        doc = self.current()
        now = self._clock() if now is None else now
        by_level: dict[str, list[str]] = {lv: [] for lv in LEVELS}
        for e in doc.get("entries") or ():
            if e.get("level") in by_level:
                by_level[e["level"]].append(e["id"])
        parts = [f"Trust map v{doc.get('version') or 0} (the hub enforces it; every hold cites "
                 "its entry)."]
        words = {"sacred": "changes only on the person's direct instruction (you may read, "
                           "propose, prepare)",
                 "guarded": "ask first (votes later)", "ship-it": "just go"}
        for lv in ("sacred", "guarded", "ship-it"):
            if by_level[lv]:
                parts.append(f"{lv}: {', '.join(by_level[lv])}: {words[lv]}.")
        parts.append(f"Unlisted: {doc.get('unlisted') or 'balanced'}.")
        live = [x for x in doc.get("exceptions") or () if exception_active(x, now)]
        for x in live:
            scope = "everything" if "*" in (x.get("applies_to") or ["*"]) else \
                ", ".join(x.get("applies_to") or ())
            if x.get("excludes"):
                scope += " except " + ", ".join(x["excludes"])
            parts.append(f"Exception {x['id']}: {scope} -> {x.get('tier', 'go')} until "
                         f"{x.get('expires')}.")
        grants = [g for g in doc.get("grants") or () if not g.get("revoked")]
        if grants:
            parts.append("Tool grants: " + "; ".join(
                f"{g['server']} for {g['role']}" for g in grants[:8]) + ".")
        borrowed = [r for r in doc.get("ledger") or () if r.get("state") == "open"]
        if borrowed:
            parts.append("Borrowed now: " + "; ".join(
                f"{r['resource']}: {r['what']} ({r.get('project') or r['why'][:60]})"
                for r in borrowed[:6]) + ".")
        parts.append("Reads and the band's records are always free. Only the person loosens "
                     "the map (dashboard); propose changes with rook_trust action=propose.")
        return " ".join(parts)

    def grants(self, role: str = "") -> list[dict]:
        """Live tool grants (not revoked), for one role or all."""
        return [dict(g) for g in self.current().get("grants") or ()
                if not g.get("revoked") and (not role or g.get("role") == role)]

    def public(self, now: float | None = None) -> dict:
        doc = copy.deepcopy(self.current())
        now = self._clock() if now is None else now
        for x in doc.get("exceptions") or ():
            x["active"] = exception_active(x, now)
        doc["pending_proposals"] = sum(1 for p in doc.get("proposals") or ()
                                       if p.get("state") == "pending")
        doc["levels"], doc["unlisted_levels"] = list(LEVELS), list(UNLISTED_LEVELS)
        doc["matrix"] = MATRIX
        doc["error"] = self.error
        return doc


# -- one store per file, per process ----------------------------------------------

_stores: dict[str, TrustStore] = {}
_stores_lock = threading.Lock()


# -- in plain words (the voice's read-back) ---------------------------------------------

_KIND_WORDS = {"read": "read", "prepare": "prepare", "change": "make changes",
               "admin": "do admin actions"}
_TIER_SAY = {"go": "without asking", "ask": "after asking", "person": "only with you"}
#: Changes that only make the map stricter: a plain sure yes applies them.
TIGHTENING_OPS = ("end_exception", "revoke_tool", "close_lease")


def _targets_words(targets: Any) -> str:
    items = [str(t) for t in (targets or [])][:4]
    words = []
    for t in items:
        kind, _, val = t.partition(":")
        words.append({"worker": f"the machine {val}", "repo": f"the repo {val}",
                      "project": f"the project {val}", "cap": f"{val} calls",
                      "resource": f"{val}"}.get(kind, t))
    more = len(targets or []) - len(items)
    out = ", ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " and " + words[-1]
    return out + (f" and {more} more" if more > 0 else "") if out else "nothing"


def describe_change(doc: dict, change: dict, now: float | None = None) -> dict:
    """One plain sentence for a change to the map (what the person hears before
    saying yes), whether it **loosens** the map (anything that isn't strictly
    stricter), and whether it touches a sacred entry. ``doc`` is the current
    map. Read-back for voice decisions: the hub, never the voice, writes it."""
    now = time.time() if now is None else now
    op = str((change or {}).get("op") or "")
    data = (change or {}).get("data") or {}
    entries = {e["id"]: e for e in doc.get("entries") or []}
    sacred = False
    if op == "set_entry":
        e = data.get("entry") if isinstance(data.get("entry"), dict) else data
        ident = str(e.get("id") or "?")
        old = entries.get(ident)
        level = str(e.get("level") or "?")
        targets = sorted(set(e.get("targets") or []))
        kind = str(e.get("kind") or "resource")
        added = sorted(set(targets) - set((old or {}).get("targets") or []))
        sacred_targets = {t for x in entries.values() if x.get("level") == "sacred"
                          for t in x.get("targets") or []}
        sacred = level == "sacred" or bool(old and old.get("level") == "sacred") or any(
            fnmatch.fnmatchcase(a.lower(), t.lower()) or fnmatch.fnmatchcase(t.lower(), a.lower())
            for a in added for t in sacred_targets)
        # Stricter only when provably so: the same entry, the same targets and kind, and
        # the same or a higher level. Anything else may loosen something (a target an
        # entry starts covering leaves the unlisted level, and falls under every active
        # exception that names this entry's id; a kind changes how it rules), so it is
        # read back and needs the check word.
        loosens = not (old is not None and level in LEVELS
                       and set(targets) == set(old.get("targets") or [])
                       and kind == str(old.get("kind") or "resource")
                       and rank(level) >= rank(str(old.get("level") or "ship-it")))
        if old is None:
            sentence = (f"Add a {level} trust entry called {ident} covering "
                        f"{_targets_words(targets)}")
        else:
            dropped = sorted(set(old.get("targets") or []) - set(targets))
            parts = []
            if level != old.get("level"):
                parts.append(f"change {ident} from {old.get('level')} to {level}")
            if dropped:
                parts.append(f"stop covering {_targets_words(dropped)} under {ident}")
            if added:
                parts.append(f"also cover {_targets_words(added)} under {ident} ({level})")
            if kind != str(old.get("kind") or "resource"):
                parts.append(f"make {ident} a {kind} entry instead of {old.get('kind')}")
            sentence = (" and ".join(parts) or f"rewrite the description of {ident}")
            sentence = sentence[:1].upper() + sentence[1:]
    elif op == "delete_entry":
        ident = str(data.get("id") or "?")
        old = entries.get(ident) or {}
        sacred = old.get("level") == "sacred"
        sentence = f"Remove the {old.get('level') or ''} trust entry {ident}".replace("  ", " ")
        loosens = True
    elif op == "set_unlisted":
        level = str(data.get("level") or "?")
        old = doc.get("unlisted") or "balanced"
        sentence = f"Treat anything the map doesn't list as {level} instead of {old}"
        loosens = rank(level) < rank(old)
    elif op == "add_exception":
        x = data.get("exception") if isinstance(data.get("exception"), dict) else data
        applies = [str(a) for a in x.get("applies_to") or ["*"]]
        who = "any agent" if applies in (["*"], []) else ", ".join(applies[:3])
        kinds = " and ".join(_KIND_WORDS.get(k, k) for k in (x.get("kinds") or ["change", "admin"]))
        over = [str(t) for t in x.get("targets") or []]
        sacred = any(entries.get(i, {}).get("level") == "sacred" for i in applies) or any(
            e.get("level") == "sacred" and set(e.get("targets") or ()) & set(over)
            for e in entries.values())
        until = str(x.get("expires") or "no end date")
        sentence = (f"Let {who} {kinds} {_TIER_SAY.get(str(x.get('tier') or 'go'), 'freely')}"
                    + (f" on {_targets_words(over)}" if over else "")
                    + f" until {until}")
        loosens = True
    elif op == "end_exception":
        sentence = f"End the exception {data.get('id') or '?'} now"
        loosens = False
    elif op == "grant_tool":
        g = data.get("grant") if isinstance(data.get("grant"), dict) else data
        sentence = f"Give the {g.get('role') or '?'} role the {g.get('server') or '?'} tool"
        loosens = True
    elif op == "revoke_tool":
        g = data.get("grant") if isinstance(data.get("grant"), dict) else data
        sentence = f"Take the {g.get('server') or '?'} tool away from the {g.get('role') or '?'} role"
        loosens = False
    elif op == "close_lease":
        sentence = f"Close the borrowed-state lease {data.get('id') or '?'}"
        loosens = False
    elif op == "add_lease":
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else data
        sentence = (f"Borrow {lease.get('resource') or '?'}: {lease.get('what') or 'a change'}"
                    f" until {lease.get('until') or 'it is restored'}")
        sacred = (entries.get(str(lease.get("resource") or "")) or {}).get("level") == "sacred"
        loosens = True
    elif op == "set_baseline":
        b = data.get("baseline") if isinstance(data.get("baseline"), dict) else data
        sentence = f"Record what normal looks like for {b.get('id') or '?'}"
        loosens = True
    else:
        sentence, loosens = f"Make a trust map change ({op or 'unknown'})", True
    sentence = " ".join(sentence.split()).rstrip(".")[:300] + "."
    return {"sentence": sentence, "loosens": bool(loosens), "sacred": bool(sacred), "op": op}


def store_for(path: str | None) -> TrustStore:
    key = os.path.abspath(path) if path else ""
    with _stores_lock:
        st = _stores.get(key)
        if st is None:
            st = _stores[key] = TrustStore(path or None)
        return st


def default_path() -> str:
    from ..paths import data_path
    return data_path("trust.json", "/var/lib/rook-band-mcp/trust.json")


# -- the hub machine's CLI ----------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    """``python -m rook.hub.trust [--file trust.json] show|summary|history|seed FILE``.

    ``seed`` applies a JSON list of ``{op, data, who, words, session?, via?}``
    as ``system`` (the hub machine's own hand, like editing policy.json),
    each recorded with the person's words and who passed them on."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m rook.hub.trust")
    ap.add_argument("--file", default=None, help="trust.json (default: the hub data dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("summary")
    h = sub.add_parser("history")
    h.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("seed")
    s.add_argument("seed_file")
    s.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    st = TrustStore(args.file or default_path())
    if args.cmd == "show":
        print(json.dumps(st.current(), indent=2, ensure_ascii=False))
    elif args.cmd == "summary":
        print(st.summary())
    elif args.cmd == "history":
        for row in st.history(args.limit):
            print(json.dumps(row, ensure_ascii=False))
    elif args.cmd == "seed":
        with open(args.seed_file, encoding="utf-8") as f:
            steps = json.load(f)
        if args.dry_run:
            st = TrustStore(None)
            st._doc = copy.deepcopy(TrustStore(args.file or default_path()).current())
        for step in steps:
            actor = Actor("system", str(step.get("who") or "system"), principal="system:trust-seed",
                          words=str(step.get("words") or ""), session=str(step.get("session") or ""),
                          via=str(step.get("via") or ""))
            out = st.change(step["op"], step.get("data") or {}, actor)
            print(f"v{out['version']} {out['op']} {out['id']}")
        print(st.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
