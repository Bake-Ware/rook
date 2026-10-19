"""Huddle roles: validation, the shipped presets and the store
(docs/design/huddles.md 4).

A role is what an agent is responsible for (a persona is how it talks)::

    {"id": "qa", "name": "QA engineer",
     "charter": "One paragraph: what the role is for.",
     "concerns": ["what it watches and raises unprompted", ...],
     "sources": [{"cap": "job.read", "args": {...}, "worker": "rook"}, ...],
     "tools": {"allow": ["job.read", "info.*"], "deny": []},
     "builtin": [],                 # harness built-ins: read, search
     "autonomy": "propose",         # propose | act
     "memory": "write",             # write | read | off
     "backend": "", "model": "", "effort": "",   # blank: the house agent's
     "host": "",                    # agent host keeping its home; blank: home.agent_host
     "persona": "", "enabled": true}

Roles are versioned like persona profiles: every change bumps ``rev`` and
lands in the history with its actor.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ....core.db import open_db

ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PATTERN = re.compile(r"^[A-Za-z0-9_.*?:\[\]-]{1,80}$")
MODEL = re.compile(r"^[A-Za-z0-9_.:/\[\]-]{0,120}$")
BACKENDS = ("", "claude-code", "codex", "opencode")
AUTONOMY = ("propose", "act")
MEMORY = ("write", "read", "off")
BUILTIN = ("read", "search")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
TEXT_LIMITS = {"name": 80, "charter": 2000, "persona": 48, "host": 64}
MAX_ITEMS = 20
MAX_ITEM = 300
DEFAULT_TOOLS = {"allow": ["knowledge.read"], "deny": []}


class RoleError(ValueError):
    pass


def _text(doc: dict, key: str) -> str:
    v = doc.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise RoleError(f"{key} must be text")
    v = v.strip()
    if len(v) > TEXT_LIMITS[key]:
        raise RoleError(f"{key} is longer than {TEXT_LIMITS[key]} characters")
    return v


def _strings(value: Any, key: str, limit: int = MAX_ITEM) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise RoleError(f"{key} must be a list of text")
    out = [x.strip() for x in value if x.strip()]
    if len(out) > MAX_ITEMS:
        raise RoleError(f"{key}: at most {MAX_ITEMS} items")
    if any(len(x) > limit for x in out):
        raise RoleError(f"{key}: each item at most {limit} characters")
    return out


def _choice(doc: dict, key: str, choices: tuple, default: str) -> str:
    v = doc.get(key, default)
    v = default if v is None else v
    if v not in choices:
        raise RoleError(f"{key} must be one of {', '.join(c or '(blank)' for c in choices)}")
    return v


def validate(doc: Any) -> dict:
    """A clean role document, or RoleError."""
    if not isinstance(doc, dict):
        raise RoleError("role must be an object")
    known = {"id", "name", "charter", "concerns", "sources", "tools", "builtin", "autonomy",
             "memory", "backend", "model", "effort", "host", "persona", "enabled", "rev"}
    extra = sorted(set(doc) - known)
    if extra:
        raise RoleError(f"unknown field {extra[0]!r}")
    rid = doc.get("id")
    if not isinstance(rid, str) or not ID.match(rid):
        raise RoleError("id: 1-32 of a-z 0-9 _ -, starting with a letter")
    out: dict[str, Any] = {"id": rid, "name": _text(doc, "name") or rid,
                           "charter": _text(doc, "charter"),
                           "concerns": _strings(doc.get("concerns"), "concerns")}
    sources = doc.get("sources") or []
    if not isinstance(sources, list) or len(sources) > MAX_ITEMS:
        raise RoleError(f"sources: a list of at most {MAX_ITEMS} {{cap, args?, worker?}}")
    out["sources"] = []
    for s in sources:
        if not isinstance(s, dict) or not isinstance(s.get("cap"), str) \
                or not PATTERN.match(s["cap"]) or "*" in s["cap"]:
            raise RoleError("each source is {cap, args?, worker?} with a cap name")
        args = s.get("args") or {}
        if not isinstance(args, dict) or len(json.dumps(args)) > 2000:
            raise RoleError("a source's args: an object (at most 2000 characters as JSON)")
        src = {"cap": s["cap"], "args": args}
        if s.get("worker"):
            if not isinstance(s["worker"], str) or len(s["worker"]) > 64:
                raise RoleError("a source's worker: a worker name")
            src["worker"] = s["worker"]
        out["sources"].append(src)
    tools = doc.get("tools", DEFAULT_TOOLS)
    tools = DEFAULT_TOOLS if tools is None else tools
    if not isinstance(tools, dict) or set(tools) - {"allow", "deny"}:
        raise RoleError("tools: {allow: [cap patterns], deny: [cap patterns]}")
    out["tools"] = {}
    for k in ("allow", "deny"):
        pats = _strings(tools.get(k), f"tools.{k}", 80)
        bad = [p for p in pats if not PATTERN.match(p)]
        if bad:
            raise RoleError(f"tools.{k}: {bad[0]!r} is not a cap pattern (e.g. task.read, info.*)")
        out["tools"][k] = pats
    builtin = _strings(doc.get("builtin"), "builtin")
    if set(builtin) - set(BUILTIN):
        raise RoleError(f"builtin: any of {', '.join(BUILTIN)}")
    out["builtin"] = sorted(set(builtin))
    out["autonomy"] = _choice(doc, "autonomy", AUTONOMY, "propose")
    out["memory"] = _choice(doc, "memory", MEMORY, "write")
    out["backend"] = _choice(doc, "backend", BACKENDS, "")
    out["effort"] = _choice(doc, "effort", EFFORTS, "")
    model = doc.get("model") or ""
    if not isinstance(model, str) or not MODEL.match(model):
        raise RoleError("model: an alias or a model id")
    out["model"] = model
    out["host"] = _text(doc, "host")
    out["persona"] = _text(doc, "persona")
    enabled = doc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RoleError("enabled must be true or false")
    out["enabled"] = enabled
    return out


def in_scope(role: dict, cap: str) -> bool:
    """Whether ``cap`` matches the role's ``tools.allow`` and no ``deny``."""
    tools = role.get("tools") or {}
    if any(fnmatch.fnmatchcase(cap, p) for p in tools.get("deny") or []):
        return False
    return any(fnmatch.fnmatchcase(cap, p) for p in tools.get("allow") or [])


#: The starting roster (docs/design/huddles.md 4.2), offered on the page and by
#: ``role.presets``; nothing is created until someone saves one.
PRESETS: tuple[dict, ...] = (
    {"id": "pm", "name": "Project manager",
     "charter": "Keeps the work moving: knows what is on deck, what is blocked and why, and "
                "what should happen next.",
     "concerns": ["the task deck and its priorities", "what is blocked and why",
                  "scope creep", "what is on deck next", "deadlines the person mentioned"],
     "sources": [{"cap": "task.read", "args": {"action": "deck"}, "worker": "rook"}],
     "tools": {"allow": ["task.read", "huddle.read", "job.read", "knowledge.read"], "deny": []},
     "autonomy": "propose"},
    {"id": "qa", "name": "QA engineer",
     "charter": "Watches quality: failing or flaky jobs, regressions after deploys, workers "
                "out of date or offline, and anything shipped without evidence.",
     "concerns": ["failing or flaky jobs", "test results on recent changes",
                  "regressions after deploys", "workers out of date or offline",
                  "anything shipped without evidence"],
     "sources": [{"cap": "job.read", "args": {"action": "runs"}, "worker": "rook"}],
     "tools": {"allow": ["job.read", "task.read", "info.*", "knowledge.read"], "deny": []},
     "autonomy": "propose"},
    {"id": "lead", "name": "Lead developer",
     "charter": "Fronts the coding agent sessions: knows what each session is doing and "
                "whether it is stuck, and the technical decisions and debt. Does not write "
                "code itself: it proposes a nudge to a session (session.send) or a new "
                "session for a task (session.start) as an action card.",
     "concerns": ["open branches and pull requests", "what every coding session is doing",
                  "sessions that look stuck", "technical decisions and debt"],
     "sources": [{"cap": "sessions.list", "args": {"live_only": True, "limit": 15}}],
     "tools": {"allow": ["sessions.list", "sessions.follow", "task.read", "knowledge.read"],
               "deny": []},
     "autonomy": "propose"},
)


def presets() -> list[dict]:
    return [validate(copy.deepcopy(p)) for p in PRESETS]


class RoleStore:
    """SQLite store (the plugin's migrations create the tables)."""

    def __init__(self, path: str | Path, migrate) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.db = open_db(self.path, wal=False)
        migrate(self.db)

    def get(self, rid: str) -> dict | None:
        row = self.db.execute("SELECT doc, rev FROM roles WHERE id=?", (rid,)).fetchone()
        if row is None:
            return None
        doc = json.loads(row["doc"])
        doc["rev"] = row["rev"]
        return doc

    def list(self) -> list[dict]:
        out = []
        for row in self.db.execute("SELECT id, rev, doc, updated, actor FROM roles ORDER BY id"):
            doc = json.loads(row["doc"])
            out.append({"id": row["id"], "rev": row["rev"], "name": doc.get("name", ""),
                        "enabled": doc.get("enabled", True), "host": doc.get("host", ""),
                        "backend": doc.get("backend", ""), "autonomy": doc.get("autonomy"),
                        "updated": row["updated"], "actor": row["actor"]})
        return out

    def save(self, doc: dict, actor: str, note: str = "", expect_rev: int | None = None,
             dry_run: bool = False) -> dict:
        clean = validate(doc)
        with self._lock:
            old = self.get(clean["id"])
            old_rev = old["rev"] if old else 0
            if expect_rev is not None and int(expect_rev) != old_rev:
                raise RoleError(f"role {clean['id']} changed (rev {old_rev}); reload it")
            prev = {k: v for k, v in old.items() if k != "rev"} if old else None
            if prev == clean:
                return {"ok": True, "id": clean["id"], "rev": old_rev, "changed": [],
                        "unchanged": True}
            changed = (["created"] if prev is None else
                       [k for k in sorted(set(prev) | set(clean)) if prev.get(k) != clean.get(k)])
            rev = old_rev + 1
            if dry_run:
                return {"ok": True, "id": clean["id"], "rev": rev, "changed": changed,
                        "dry_run": True, "role": clean}
            now = time.time()
            body = json.dumps(clean, sort_keys=True)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO roles VALUES(?,?,?,?,?)",
                                (clean["id"], rev, body, now, actor))
                self.db.execute("INSERT INTO role_history(ref, rev, doc, ts, actor, note) "
                                "VALUES(?,?,?,?,?,?)",
                                (clean["id"], rev, body, now, actor, note[:200]))
        return {"ok": True, "id": clean["id"], "rev": rev, "changed": changed}

    def update(self, rid: str, actor: str, note: str = "", **fields) -> dict:
        """Change some fields of a saved role (a new revision)."""
        with self._lock:
            old = self.get(rid)
        if old is None:
            raise RoleError(f"no role {rid!r}")
        doc = {**{k: v for k, v in old.items() if k != "rev"}, **fields}
        return self.save(doc, actor, note)

    def delete(self, rid: str, actor: str, note: str = "") -> dict:
        with self._lock:
            old = self.get(rid)
            if old is None:
                raise RoleError(f"no role {rid!r}")
            with self.db:
                self.db.execute("DELETE FROM roles WHERE id=?", (rid,))
                self.db.execute("INSERT INTO role_history(ref, rev, doc, ts, actor, note) "
                                "VALUES(?,?,NULL,?,?,?)",
                                (rid, old["rev"], time.time(), actor, note[:200]))
        return {"ok": True, "id": rid, "deleted": True}

    def history(self, rid: str = "", limit: int = 50) -> list[dict]:
        q = "SELECT ref, rev, doc, ts, actor, note FROM role_history"
        args: list[Any] = []
        if rid:
            q += " WHERE ref=?"
            args.append(rid)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        out = []
        for r in self.db.execute(q, args):
            d = dict(r)
            d["doc"] = json.loads(d["doc"]) if d["doc"] else None
            out.append(d)
        return out
