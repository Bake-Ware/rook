"""Persona profiles: validation, rendering, scoped resolution and the store.

A **profile** is a named persona document (docs/design/persona.md):

    {"id": "calm-engineer", "name": "Ada", "owner": "Alex",
     "voice": "Calm, direct, a little dry.",
     "rules": ["..."], "do": ["..."], "dont": ["..."],
     "formatting": "Short paragraphs; code in fenced blocks.",
     "addenda": {"claude-code": "...", "codex": "...", "hermes": "...", "voice": "..."},
     "description": "operator note, never rendered"}

An **assignment** says which profile applies at a scope:

    default            every caller (target '')
    band:<band id>     callers on that band
    family:<name>      an agent family / harness (claude-code, codex, hermes, voice, mcp, ...)
    user:<id>          a dashboard account id, or an API token's agent_id or label

The most specific assignment wins, whole: user > family > band > default.
Profiles and assignments are versioned; every change lands in the history
with its actor.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
FAMILY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
SCOPES = ("user", "family", "band", "default")          # most specific first
KNOWN_FAMILIES = ("claude-code", "codex", "hermes", "voice", "mcp")
#: Harness names used elsewhere in Rook (work launch templates, clientInfo,
#: User-Agent fragments) mapped to a family.
FAMILY_ALIASES = {"claude": "claude-code", "claudecode": "claude-code", "claude_code": "claude-code",
                  "claude-ai": "claude-code", "codex-cli": "codex", "codex_cli_rs": "codex",
                  "hermes-agent": "hermes"}

TEXT_LIMITS = {"name": 60, "owner": 60, "voice": 600, "formatting": 600, "description": 200}
LIST_KEYS = ("rules", "do", "dont")
MAX_ITEMS = 20
MAX_ITEM = 300
MAX_ADDENDUM = 1200
MAX_ADDENDA = 12
#: Budget for the persona inside MCP ``initialize`` instructions (characters).
#: Instructions are paid once per connect; the full text is one call away.
MCP_BUDGET = 1200


class PersonaError(ValueError):
    pass


def family(value: str | None) -> str:
    """Normalize a harness/client name to a family (``claude`` -> ``claude-code``)."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    v = FAMILY_ALIASES.get(v, v)
    return v if FAMILY.match(v) else ""


def family_from_client(text: str | None) -> str:
    """Best-effort family from a client name or User-Agent string."""
    t = (text or "").lower()
    if not t:
        return ""
    for needle, fam in (("claude", "claude-code"), ("codex", "codex"), ("hermes", "hermes")):
        if needle in t:
            return fam
    return ""


def _clean_text(key: str, value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PersonaError(f"{key} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise PersonaError(f"{key} is longer than {limit} characters")
    return value


def validate(doc: Any) -> dict:
    """A clean profile document, or :class:`PersonaError`."""
    if not isinstance(doc, dict):
        raise PersonaError("profile must be an object")
    unknown = set(doc) - {"id", "rev", *TEXT_LIMITS, *LIST_KEYS, "addenda"}
    if unknown:
        raise PersonaError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    pid = doc.get("id")
    if not isinstance(pid, str) or not ID.match(pid):
        raise PersonaError("id must be a slug: lowercase letters, digits, - or _ (max 48)")
    out: dict[str, Any] = {"id": pid}
    for key, limit in TEXT_LIMITS.items():
        out[key] = _clean_text(key, doc.get(key), limit)
    for key in LIST_KEYS:
        items = doc.get(key) or []
        if isinstance(items, str):
            items = [line for line in items.splitlines()]
        if not isinstance(items, list):
            raise PersonaError(f"{key} must be a list of strings")
        clean = []
        for item in items:
            item = _clean_text(key, item, MAX_ITEM).lstrip("-* ").strip()
            if item:
                clean.append(item)
        if len(clean) > MAX_ITEMS:
            raise PersonaError(f"{key} has more than {MAX_ITEMS} items")
        out[key] = clean
    addenda = doc.get("addenda") or {}
    if not isinstance(addenda, dict):
        raise PersonaError("addenda must be an object of {family: text}")
    if len(addenda) > MAX_ADDENDA:
        raise PersonaError(f"at most {MAX_ADDENDA} addenda")
    clean_add = {}
    for fam, text in addenda.items():
        f = family(fam)
        if not f:
            raise PersonaError(f"addenda key {fam!r} is not a family name")
        text = _clean_text(f"addenda.{f}", text, MAX_ADDENDUM)
        if text:
            clean_add[f] = text
    out["addenda"] = dict(sorted(clean_add.items()))
    if not any(out[k] for k in ("name", "voice", "formatting", *LIST_KEYS)) and not clean_add:
        raise PersonaError("profile is empty: set at least a name, voice, rules or formatting")
    return out


def render(doc: dict | None, harness: str = "", *, heading: bool = True) -> str:
    """The persona as Markdown for one harness family (neutral wording)."""
    if not doc:
        return ""
    fam = family(harness)
    lines: list[str] = []
    name, owner = doc.get("name") or "", doc.get("owner") or ""
    if heading:
        lines.append("## Persona" + (f": {name}" if name else ""))
    if name or owner:
        who = f"You are {name}" if name else "You are the assistant"
        lines.append(who + (f", working for {owner}." if owner else "."))
    if doc.get("voice"):
        lines.append(f"Voice and tone: {doc['voice']}")
    for key, title in (("rules", "Rules"), ("do", "Do"), ("dont", "Don't")):
        if doc.get(key):
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in doc[key])
    if doc.get("formatting"):
        lines.append(f"Formatting: {doc['formatting']}")
    add = (doc.get("addenda") or {}).get(fam) if fam else None
    if add:
        lines.append(add)
    return "\n".join(lines).strip() + "\n"


def compact(text: str, budget: int = MCP_BUDGET) -> str:
    """Fit ``text`` in ``budget`` characters, cutting at a line boundary."""
    text = text.strip()
    if len(text) <= budget:
        return text
    tail = "\n(Persona trimmed; rook_call cap=persona.get worker=rook for the rest.)"
    cut = text[: budget - len(tail)]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut.rstrip() + tail


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def diff_summary(old: dict | None, new: dict | None) -> list[str]:
    if old is None:
        return ["created"]
    if new is None:
        return ["deleted"]
    return [k for k in sorted(set(old) | set(new)) if k != "rev" and old.get(k) != new.get(k)]


class PersonaStore:
    """SQLite store (the plugin's migrations create the tables)."""

    def __init__(self, path: str | Path, migrate) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        migrate(self.db)

    # -- profiles ------------------------------------------------------------
    def profile(self, pid: str) -> dict | None:
        row = self.db.execute("SELECT doc, rev, updated, actor FROM persona_profiles WHERE id=?",
                              (pid,)).fetchone()
        if row is None:
            return None
        doc = json.loads(row["doc"])
        doc["rev"] = row["rev"]
        return doc

    def profiles(self) -> list[dict]:
        out = []
        for row in self.db.execute("SELECT id, rev, doc, updated, actor FROM persona_profiles "
                                   "ORDER BY id"):
            doc = json.loads(row["doc"])
            out.append({"id": row["id"], "rev": row["rev"], "name": doc.get("name", ""),
                        "description": doc.get("description", ""), "updated": row["updated"],
                        "actor": row["actor"]})
        return out

    def save(self, doc: dict, actor: str, note: str = "", expect_rev: int | None = None,
             dry_run: bool = False) -> dict:
        clean = validate(doc)
        with self._lock:
            old = self.profile(clean["id"])
            old_rev = old["rev"] if old else 0
            if expect_rev is not None and int(expect_rev) != old_rev:
                raise PersonaError(f"profile {clean['id']} changed (rev {old_rev}); reload it")
            prev = {k: v for k, v in (old or {}).items() if k != "rev"} if old else None
            changed = diff_summary(prev, clean)
            if prev == clean:
                return {"ok": True, "id": clean["id"], "rev": old_rev, "changed": [],
                        "unchanged": True}
            rev = old_rev + 1
            if dry_run:
                return {"ok": True, "id": clean["id"], "rev": rev, "changed": changed,
                        "dry_run": True, "text": render(clean)}
            now = time.time()
            body = json.dumps(clean, sort_keys=True)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO persona_profiles VALUES(?,?,?,?,?)",
                                (clean["id"], rev, body, now, actor))
                self.db.execute("INSERT INTO persona_history(kind, ref, rev, doc, ts, actor, note) "
                                "VALUES('profile',?,?,?,?,?,?)",
                                (clean["id"], rev, body, now, actor, note[:200]))
        return {"ok": True, "id": clean["id"], "rev": rev, "changed": changed}

    def delete(self, pid: str, actor: str, note: str = "") -> dict:
        with self._lock:
            old = self.profile(pid)
            if old is None:
                raise PersonaError(f"no profile {pid!r}")
            used = [f"{r['scope']}:{r['target']}" for r in self.db.execute(
                "SELECT scope, target FROM persona_assignments WHERE profile=?", (pid,))]
            if used:
                raise PersonaError(f"profile {pid} is assigned at {', '.join(used)}; "
                                   "unassign it first")
            with self.db:
                self.db.execute("DELETE FROM persona_profiles WHERE id=?", (pid,))
                self.db.execute("INSERT INTO persona_history(kind, ref, rev, doc, ts, actor, note) "
                                "VALUES('profile',?,?,NULL,?,?,?)",
                                (pid, old["rev"], time.time(), actor, note[:200]))
        return {"ok": True, "id": pid, "deleted": True}

    # -- assignments -----------------------------------------------------------
    @staticmethod
    def _scope(scope: str, target: str) -> tuple[str, str]:
        scope = (scope or "").strip().lower()
        target = (target or "").strip()
        if scope not in SCOPES:
            raise PersonaError(f"scope must be one of {', '.join(SCOPES)}")
        if scope == "default":
            target = ""
        elif not target:
            raise PersonaError(f"scope {scope} needs a target")
        elif scope == "family":
            target = family(target)
            if not target:
                raise PersonaError("family target must be a family name (claude-code, codex, ...)")
        elif len(target) > 128:
            raise PersonaError("target is too long")
        return scope, target

    def assign(self, scope: str, target: str, profile: str, actor: str, note: str = "") -> dict:
        scope, target = self._scope(scope, target)
        profile = (profile or "").strip()
        ref = f"{scope}:{target}"
        with self._lock:
            if profile and self.profile(profile) is None:
                raise PersonaError(f"no profile {profile!r}")
            now = time.time()
            with self.db:
                if profile:
                    self.db.execute("INSERT OR REPLACE INTO persona_assignments VALUES(?,?,?,?,?)",
                                    (scope, target, profile, now, actor))
                else:
                    self.db.execute("DELETE FROM persona_assignments WHERE scope=? AND target=?",
                                    (scope, target))
                self.db.execute("INSERT INTO persona_history(kind, ref, rev, doc, ts, actor, note) "
                                "VALUES('assign',?,NULL,?,?,?,?)",
                                (ref, json.dumps({"profile": profile}) if profile else None, now,
                                 actor, note[:200]))
        return {"ok": True, "scope": scope, "target": target, "profile": profile or None}

    def assignments(self) -> list[dict]:
        order = {s: i for i, s in enumerate(reversed(SCOPES))}
        rows = [dict(r) for r in self.db.execute(
            "SELECT scope, target, profile, updated, actor FROM persona_assignments")]
        return sorted(rows, key=lambda r: (order.get(r["scope"], 9), r["target"]))

    def history(self, ref: str = "", limit: int = 50) -> list[dict]:
        q = "SELECT kind, ref, rev, doc, ts, actor, note FROM persona_history"
        args: list[Any] = []
        if ref:
            q += " WHERE ref=?"
            args.append(ref)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        out = []
        for r in self.db.execute(q, args):
            d = dict(r)
            d["doc"] = json.loads(d["doc"]) if d["doc"] else None
            out.append(d)
        return out

    # -- resolution --------------------------------------------------------------
    def resolve(self, users: Iterable[str] = (), fam: str = "", band: str = "") -> tuple[dict | None, dict]:
        """(profile or None, {scope, target}) for a caller. ``users`` are the
        caller's identifiers (account id, token agent_id, token label)."""
        rows = {(r["scope"], r["target"]): r["profile"] for r in self.assignments()}
        candidates: list[tuple[str, str]] = []
        candidates += [("user", u) for u in users if u]
        f = family(fam)
        if f:
            candidates.append(("family", f))
        if band:
            candidates.append(("band", band))
        candidates.append(("default", ""))
        for key in candidates:
            pid = rows.get(key)
            if pid:
                doc = self.profile(pid)
                if doc is not None:
                    return doc, {"scope": key[0], "target": key[1], "profile": pid}
        return None, {}
