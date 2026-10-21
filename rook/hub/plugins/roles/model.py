"""Huddle roles: validation, the shipped presets and the store
(docs/design/huddles.md 4).

A role is what an agent is responsible for (a persona is how it talks)::

    {"id": "qa", "name": "QA engineer",
     "description": "One line: what it is for (the chair reads this).",
     "category": "engineering",     # a preset category id (presets/categories.toml)
     "charter": "What it owns, what it never does, how it decides, when it asks a person.",
     "concerns": ["what it watches and raises unprompted", ...],
     "checklist": ["what it works through on every turn", ...],
     "examples": ["a question it answers well", ...],
     "seed_memory": ["a fact it starts with", ...],   # written once, when its home is made
     "sources": [{"cap": "job.read", "args": {...}, "worker": "rook"}, ...],
     "tools": {"allow": ["job.read", "info.*"], "deny": []},
     "builtin": [],                 # harness built-ins: read, search
     "autonomy": "propose",         # propose | act
     "memory": "write",             # write | read | off
     "backend": "", "model": "", "effort": "",   # blank: the house agent's
     "max_tool_calls": 30,          # optional: tool calls per turn (else the category's, else role.max_tool_calls)
     "host": "",                    # agent host keeping its home; blank: home.agent_host
     "persona": "", "enabled": true,
     "voice": ""}                   # optional: the voice its messages are read in (blank: the band's)

Roles are versioned like persona profiles: every change bumps ``rev`` and
lands in the history with its actor.

The presets (starting roles, grouped into categories) are data: one TOML
file per preset in ``presets/`` and the category list in
``presets/categories.toml`` (:func:`load_presets`).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

from ....core.db import open_db

ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PATTERN = re.compile(r"^[A-Za-z0-9_.*?:\[\]-]{1,80}$")
MODEL = re.compile(r"^[A-Za-z0-9_.:/\[\]-]{0,120}$")
#: A voice engine's voice id: ``af_heart``, ``sojourn``, ``kokoro:af_heart``.
VOICE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
BACKENDS = ("", "claude-code", "codex", "opencode")
AUTONOMY = ("propose", "act")
MEMORY = ("write", "read", "off")
BUILTIN = ("read", "search")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
TEXT_LIMITS = {"name": 80, "description": 160, "category": 32, "charter": 2000, "persona": 48,
               "host": 64}
MAX_ITEMS = 20
MAX_EXAMPLES = 5
MAX_ITEM = 300
MAX_TOOL_CALLS = 100
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


def _tool_calls(value: Any, what: str) -> int:
    if value in (None, "", 0):
        return 0
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_TOOL_CALLS:
        raise RoleError(f"{what}: a whole number from 1 to {MAX_TOOL_CALLS} (blank: the default)")
    return value


def validate(doc: Any) -> dict:
    """A clean role document, or RoleError."""
    if not isinstance(doc, dict):
        raise RoleError("role must be an object")
    known = {"id", "name", "description", "category", "charter", "concerns", "checklist",
             "examples", "seed_memory", "sources", "tools", "builtin", "autonomy", "memory",
             "backend", "model", "effort", "max_tool_calls", "host", "persona", "enabled",
             "voice", "rev"}
    extra = sorted(set(doc) - known)
    if extra:
        raise RoleError(f"unknown field {extra[0]!r}")
    rid = doc.get("id")
    if not isinstance(rid, str) or not ID.match(rid):
        raise RoleError("id: 1-32 of a-z 0-9 _ -, starting with a letter")
    out: dict[str, Any] = {"id": rid, "name": _text(doc, "name") or rid,
                           "description": " ".join(_text(doc, "description").split()),
                           "category": _text(doc, "category"),
                           "charter": _text(doc, "charter"),
                           "concerns": _strings(doc.get("concerns"), "concerns"),
                           "checklist": _strings(doc.get("checklist"), "checklist"),
                           "examples": _strings(doc.get("examples"), "examples"),
                           "seed_memory": _strings(doc.get("seed_memory"), "seed_memory")}
    if out["category"] and not ID.match(out["category"]):
        raise RoleError("category: a category id (a-z 0-9 _ -, starting with a letter)")
    if len(out["examples"]) > MAX_EXAMPLES:
        raise RoleError(f"examples: at most {MAX_EXAMPLES}")
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
    calls = _tool_calls(doc.get("max_tool_calls"), "max_tool_calls")
    if calls:                          # blank or 0: the category's, else the setting
        out["max_tool_calls"] = calls
    voice = doc.get("voice") or ""
    if not isinstance(voice, str) or not VOICE.match(voice.strip()):
        raise RoleError("voice: a voice id such as af_heart (blank: the band's default voice)")
    if voice.strip():                  # blank: the band's default
        out["voice"] = voice.strip()
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


# -- presets ----------------------------------------------------------------------------

#: Where the presets live: ``categories.toml`` and one ``<id>.toml`` per preset.
PRESETS_DIR = Path(__file__).with_name("presets")
#: Keys a preset file may have besides the role fields: its place in its category.
PRESET_ONLY = ("order",)
CATEGORY_KEYS = {"id", "name", "order", "description", "guard", "max_tool_calls"}
GUARD_KEYS = {"autonomy", "deny", "note"}


def _category(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - CATEGORY_KEYS:
        raise RoleError(f"a category is {{{', '.join(sorted(CATEGORY_KEYS))}}}")
    cid = raw.get("id")
    if not isinstance(cid, str) or not ID.match(cid):
        raise RoleError(f"category id {cid!r}: a-z 0-9 _ -, starting with a letter")
    name, desc = raw.get("name"), raw.get("description", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise RoleError(f"category {cid}: a name of at most 60 characters")
    if not isinstance(desc, str) or len(desc) > 200:
        raise RoleError(f"category {cid}: a description of at most 200 characters")
    order = raw.get("order", 1000)
    if not isinstance(order, int) or isinstance(order, bool):
        raise RoleError(f"category {cid}: order is a whole number")
    guard = raw.get("guard") or {}
    if not isinstance(guard, dict) or set(guard) - GUARD_KEYS:
        raise RoleError(f"category {cid}: guard is {{autonomy, deny, note}}")
    if guard.get("autonomy") not in (None, *AUTONOMY):
        raise RoleError(f"category {cid}: guard.autonomy is one of {', '.join(AUTONOMY)}")
    deny = _strings(guard.get("deny"), f"category {cid}: guard.deny", 80)
    if any(not PATTERN.match(p) for p in deny):
        raise RoleError(f"category {cid}: guard.deny takes cap patterns")
    out = {"id": cid, "name": name.strip(), "order": order, "description": desc.strip()}
    calls = _tool_calls(raw.get("max_tool_calls"), f"category {cid}: max_tool_calls")
    if calls:
        out["max_tool_calls"] = calls
    if guard:
        out["guard"] = {"autonomy": guard.get("autonomy") or "", "deny": deny,
                        "note": str(guard.get("note") or "").strip()}
    return out


def _guarded(doc: dict, cat: dict) -> dict:
    """A preset with its category's guard applied: the guard's autonomy is
    required (a preset that says otherwise is an error, never silently
    changed) and its deny patterns are added to the preset's own."""
    guard = cat.get("guard")
    if not guard:
        return doc
    if guard["autonomy"] and doc.get("autonomy", "propose") != guard["autonomy"]:
        raise RoleError(f"preset {doc['id']}: the {cat['id']} category needs autonomy "
                        f"{guard['autonomy']}")
    tools = dict(doc.get("tools") or DEFAULT_TOOLS)
    deny = list(tools.get("deny") or [])
    tools["deny"] = deny + [p for p in guard["deny"] if p not in deny]
    return {**doc, "tools": tools}


def load_presets(root: str | Path = PRESETS_DIR) -> tuple[list[dict], list[dict]]:
    """``(categories, presets)`` from ``root``: the categories in their sort
    order, each preset a clean role document (plus ``order``) in its
    category's order. Any bad file is a RoleError naming it."""
    root = Path(root)
    try:
        raw = tomllib.loads((root / "categories.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RoleError(f"presets: categories.toml: {e}") from None
    cats = [_category(c) for c in raw.get("category") or []]
    by_id = {c["id"]: c for c in cats}
    if len(by_id) != len(cats):
        raise RoleError("presets: two categories share an id")
    presets: list[dict] = []
    for path in sorted(root.glob("*.toml")):
        if path.name == "categories.toml":
            continue
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
            order = doc.pop("order", 1000)
            if not isinstance(order, int) or isinstance(order, bool):
                raise RoleError("order is a whole number")
            if doc.get("id") != path.stem:
                raise RoleError(f"id must be {path.stem!r}, the file's name")
            cat = by_id.get(doc.get("category") or "")
            if cat is None:
                raise RoleError(f"category {doc.get('category')!r} is not in categories.toml")
            if not str(doc.get("description") or "").strip():
                raise RoleError("a preset needs a one-line description")
            clean = validate(_guarded(doc, cat))
        except (RoleError, tomllib.TOMLDecodeError) as e:
            raise RoleError(f"presets: {path.name}: {e}") from None
        presets.append({**clean, "order": order})
    rank = {c["id"]: i for i, c in enumerate(sorted(cats, key=lambda c: (c["order"], c["id"])))}
    presets.sort(key=lambda p: (rank[p["category"]], p["order"], p["name"]))
    return sorted(cats, key=lambda c: (c["order"], c["id"])), presets


_LOADED: tuple[list[dict], list[dict]] | None = None


def _loaded() -> tuple[list[dict], list[dict]]:
    global _LOADED
    if _LOADED is None:
        _LOADED = load_presets()
    return _LOADED


def categories() -> list[dict]:
    """The preset categories in sort order: {id, name, order, description, guard?}."""
    return copy.deepcopy(_loaded()[0])


def presets() -> list[dict]:
    """Every preset as a role document to save with ``role.set``, grouped by
    category (in the categories' order) and in each category's order."""
    return [{k: v for k, v in p.items() if k not in PRESET_ONLY}
            for p in copy.deepcopy(_loaded()[1])]


def preset(pid: str) -> dict | None:
    return next((p for p in presets() if p["id"] == pid), None)


def grouped(full: bool = False) -> list[dict]:
    """The categories, each with its presets (``full``: whole role documents;
    otherwise id, name, description and autonomy), empty categories left out."""
    docs = presets()
    out = []
    for c in categories():
        mine = [p if full else {k: p[k] for k in ("id", "name", "description", "category",
                                                    "autonomy")}
                for p in docs if p["category"] == c["id"]]
        if mine:
            out.append({**c, "presets": mine})
    return out


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
