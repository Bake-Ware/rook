"""The house agent's playbook: how it routes requests and when it asks the
person (docs/design/front-door.md 3).

One Markdown file in the house agent's home on the hub,
``<hub state>/plugins/home/playbook.md``, that a person reads and edits (the
dashboard's Decisions page, or the file itself), plus an append-only history,
``playbook-history.jsonl``. The router reads the whole file every turn.

The file is a list of rules under fixed headings::

    ## Just do it
    - [pb-3f9a1c] Create, update and close tasks and knowledge pages. -- default

Each heading is a **section** with a meaning for the router:

* ``do`` (Just do it), ``dispatch`` (Send to a coding session) and ``rule``
  (Settling disagreements) let the house agent act or decide without asking:
  adding a rule there **loosens** it, removing one **tightens** it;
* ``ask`` (Ask me first), ``answer`` (Just answer) and ``huddle`` (Bring in
  the team) make it ask, talk or deliberate instead of acting: adding a rule
  there tightens it, removing one loosens it.

The house agent may tighten on its own (applied at once and logged, the
person's words kept); a loosening it can only propose: it becomes a one-line
ask the person answers. A person edits anything. The trust map is never
overridden by the playbook: a rule can make the house agent ask *more* than
the map, never act where the map says to ask (rook/hub/trust.py).
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

#: (key, heading) in file order.
SECTIONS = (
    ("do", "Just do it"),
    ("ask", "Ask me first"),
    ("dispatch", "Send to a coding session"),
    ("huddle", "Bring in the team (a huddle)"),
    ("answer", "Just answer"),
    ("rule", "Settling disagreements"),
)
TITLES = dict(SECTIONS)
#: Sections whose rules let the house agent act or decide without the person.
ACT = ("do", "dispatch", "rule")
#: Sections whose rules make it ask, talk or deliberate.
HOLD = ("ask", "answer", "huddle")
RULE_MAX, BY_MAX, FILE_MAX, RULES_MAX = 400, 300, 40000, 200
_ID = re.compile(r"^pb-[a-z0-9]{4,12}$")
_LINE = re.compile(r"^\s*[-*]\s+(?:\[(pb-[a-z0-9]{4,12})\]\s*)?(.*?)\s*$")
_BY = re.compile(r"\s+--\s+(.*)$")

INTRO = (
    "How the house agent routes what you ask, and when it asks you. Edit freely: one rule "
    "per line starting with \"- \" under a heading. The [pb-...] tag keeps a rule's history "
    "(new lines get one when saved); who set it and their words follow \" -- \".\n"
    "The trust map (Manage > Trust) always wins: a rule here can make the house agent ask "
    "more than the map, never act where the map says to ask.")

DEFAULTS: dict[str, list[str]] = {
    "do": [
        "Create, update and close tasks, knowledge pages, notes and reminders.",
        "Quick reversible actions on ship-it targets (music, lights, a service restart the "
        "trust map lets agents do).",
    ],
    "ask": [
        "Anything that deletes data, spends money, or sends a message to someone other "
        "than me.",
        "Anything on a family device.",
    ],
    "dispatch": [
        "One clear code change in a repo with a code location (a bug fix, a small feature, "
        "a doc fix): a coding session for the lead developer, tracked to its pull request.",
    ],
    "huddle": [
        "Design questions, anything fuzzy, or work that needs more than one discipline "
        "(security, QA, product): open a huddle with the roles that fit.",
    ],
    "answer": [
        "Questions, chat, and status (what's running, what's waiting, what you decided "
        "for me).",
    ],
    "rule": [],
}


class PlaybookError(ValueError):
    """A change refused; the text says why."""


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def new_id() -> str:
    return "pb-" + secrets.token_hex(3)


def direction(op: str, section: str) -> str:
    """``tighten`` or ``loosen``: what adding or removing a rule in
    ``section`` does to how much the house agent does without asking."""
    if section not in TITLES:
        raise PlaybookError("section: one of " + ", ".join(TITLES))
    if op == "add":
        return "loosen" if section in ACT else "tighten"
    if op == "remove":
        return "tighten" if section in ACT else "loosen"
    raise PlaybookError("op: add or remove")


@dataclass
class Rule:
    id: str
    section: str
    text: str
    by: str = ""

    def line(self) -> str:
        return f"- [{self.id}] {self.text}" + (f" -- {self.by}" if self.by else "")

    def public(self) -> dict:
        return {"id": self.id, "section": self.section, "section_title": TITLES.get(self.section,
                                                                                    self.section),
                "text": self.text, "by": self.by}


def section_of(heading: str) -> str | None:
    h = " ".join(heading.lower().split())
    for key, title in SECTIONS:
        t = title.lower()
        if h == t or h.startswith(t.split(" (")[0]):
            return key
    return None


def parse(text: str) -> tuple[list[Rule], list[str]]:
    """The rules in a playbook file, in order, and the problems found (lines
    outside a known heading are kept as notes, never as rules)."""
    rules: list[Rule] = []
    problems: list[str] = []
    section: str | None = None
    seen: set[str] = set()
    for n, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.rstrip()
        if line.startswith("## "):
            section = section_of(line[3:])
            if section is None:
                problems.append(f"line {n}: unknown heading {line[3:].strip()!r} (its lines are "
                                "notes, not rules)")
            continue
        m = _LINE.match(line)
        if not m or section is None or line.startswith("#"):
            continue
        rid, body = m.group(1), m.group(2)
        by = ""
        mb = _BY.search(body)
        if mb:
            by, body = _clean(mb.group(1), BY_MAX), body[:mb.start()]
        body = _clean(body, RULE_MAX)
        if not body:
            continue
        if not rid or rid in seen:
            rid = new_id()
        seen.add(rid)
        rules.append(Rule(rid, section, body, by))
    if len(rules) > RULES_MAX:
        problems.append(f"{len(rules)} rules: at most {RULES_MAX} are read")
        rules = rules[:RULES_MAX]
    return rules, problems


def render(rules: list[Rule]) -> str:
    out = ["# House agent playbook", "", INTRO, ""]
    for key, title in SECTIONS:
        out.append(f"## {title}")
        mine = [r for r in rules if r.section == key]
        out += [r.line() for r in mine] if mine else ["(none yet)"]
        out.append("")
    return "\n".join(out)


class Playbook:
    """``playbook.md`` + ``playbook-history.jsonl``. Reads are cached on the
    file's mtime; writes go through a lock and an atomic rename."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.history_path = os.path.join(os.path.dirname(path), "playbook-history.jsonl")
        self._lock = threading.Lock()
        self._cache: tuple[int, list[Rule], list[str], str] | None = None

    # -- reading -------------------------------------------------------------
    def _ensure(self) -> None:
        if os.path.exists(self.path):
            return
        rules = [Rule(new_id(), key, text, "default")
                 for key, _ in SECTIONS for text in DEFAULTS.get(key, [])]
        self._write(render(rules), [{"op": "seed", "rules": len(rules)}])

    def load(self) -> tuple[list[Rule], list[str], str]:
        with self._lock:
            self._ensure()
            try:
                st = os.stat(self.path).st_mtime_ns
            except OSError:
                return [], ["the playbook file is missing"], ""
            if self._cache is not None and self._cache[0] == st:
                return self._cache[1], self._cache[2], self._cache[3]
            with open(self.path, encoding="utf-8") as f:
                text = f.read(FILE_MAX + 1)
            rules, problems = parse(text[:FILE_MAX])
            self._cache = (st, rules, problems, text[:FILE_MAX])
            return rules, problems, text[:FILE_MAX]

    def rules(self) -> list[Rule]:
        return self.load()[0]

    def get(self, rid: str) -> Rule | None:
        return next((r for r in self.rules() if r.id == rid), None)

    def text(self) -> str:
        return self.load()[2]

    def prompt_text(self, limit: int = 6000) -> str:
        """The rules for the router's system prompt, by section, with ids (it
        cites them)."""
        rules = self.rules()
        lines = ["Your playbook (the person's routing rules; cite a rule by its id):"]
        for key, title in SECTIONS:
            mine = [r for r in rules if r.section == key]
            if mine:
                lines.append(f"{title} [{key}]:")
                lines += [f"- [{r.id}] {r.text}" for r in mine]
        out = "\n".join(lines)
        return out if len(out) <= limit else out[:limit - 1] + "…"

    def history(self, limit: int = 50) -> list[dict]:
        rows: list[dict] = []
        with contextlib.suppress(OSError):
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    with contextlib.suppress(ValueError):
                        rows.append(json.loads(line))
        return list(reversed(rows[-max(1, int(limit)):]))

    def public(self) -> dict:
        rules, problems, text = self.load()
        return {"text": text, "rules": [r.public() for r in rules], "problems": problems,
                "sections": [{"key": k, "title": t, "acts": k in ACT} for k, t in SECTIONS],
                "history": self.history(20)}

    # -- writing ---------------------------------------------------------------
    def _write(self, text: str, rows: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.path)
        now = time.time()
        with open(self.history_path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps({"at": now, "when": _iso(now), **row}, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
        self._cache = None

    def change(self, op: str, section: str = "", text: str = "", rid: str = "", *,
               who: str, kind: str, words: str = "", via: str = "") -> dict:
        """Add or remove one rule. ``kind``: ``person`` (anything), ``house``
        (tighten only; a loosening raises :class:`PlaybookError`). Returns the
        history row."""
        words = _clean(words, 1000)
        if kind not in ("person", "house"):
            raise PlaybookError("only a person or the house agent changes the playbook")
        with self._lock:
            self._ensure()
        rules = list(self.rules())
        if op == "add":
            body = _clean(text, RULE_MAX)
            if len(body) < 4:
                raise PlaybookError("text: the rule, in a sentence")
            if section not in TITLES:
                raise PlaybookError("section: one of " + ", ".join(TITLES))
            dup = next((r for r in rules if r.section == section
                        and r.text.lower().strip(" .") == body.lower().strip(" .")), None)
            if dup is not None:
                return {"op": "add", "id": dup.id, "section": section, "text": dup.text,
                        "direction": direction("add", section), "unchanged": True}
            rule = Rule(new_id(), section, body, self._by(who, words))
        elif op == "remove":
            rule = next((r for r in rules if r.id == rid), None)
            if rule is None:
                raise PlaybookError(f"no rule {rid!r} (the playbook lists them with ids)")
            section = rule.section
        else:
            raise PlaybookError("op: add or remove")
        way = direction(op, section)
        if kind == "house" and way == "loosen":
            raise PlaybookError("that loosens the playbook: only the person does that; it is "
                                "asked as a one-line question instead")
        if op == "add":
            # After the last rule of its section, so the file keeps its order.
            idx = max((i for i, r in enumerate(rules) if r.section == section), default=-1)
            if idx < 0:
                order = [k for k, _ in SECTIONS]
                before = [i for i, r in enumerate(rules)
                          if order.index(r.section) < order.index(section)]
                idx = max(before, default=-1)
            rules.insert(idx + 1, rule)
        else:
            rules = [r for r in rules if r.id != rid]
        row = {"op": op, "id": rule.id, "section": section, "text": rule.text,
               "direction": way, "who": _clean(who, 120), "kind": kind, "words": words,
               **({"via": _clean(via, 300)} if via else {})}
        with self._lock:
            self._write(self._merge(rules), [row])
        return row

    def _merge(self, rules: list[Rule]) -> str:
        """The file with ``rules``: rendered fresh (person-written notes under
        unknown headings are kept at the end)."""
        notes = []
        with contextlib.suppress(OSError):
            with open(self.path, encoding="utf-8") as f:
                section = "x"
                for line in f.read(FILE_MAX).splitlines():
                    if line.startswith("## "):
                        section = section_of(line[3:]) or "?"
                        if section == "?":
                            notes.append(line)
                    elif section == "?":
                        notes.append(line)
        body = render(rules)
        return body + ("\n" + "\n".join(notes).strip() + "\n" if notes else "")

    @staticmethod
    def _by(who: str, words: str) -> str:
        day = _dt.date.today().isoformat()
        out = f"{_clean(who, 60)} {day}"
        if words:
            out += f': "{_clean(words, 200)}"'
        return out

    def save_text(self, text: str, *, who: str, words: str = "") -> dict:
        """A person's edit of the whole file. Every rule added or removed is
        logged with its direction; nothing is refused (the person may loosen)."""
        text = str(text or "")
        if len(text) > FILE_MAX:
            raise PlaybookError(f"the playbook is at most {FILE_MAX} characters")
        old = {r.id: r for r in self.rules()}
        new, problems = parse(text)
        rows = []
        for r in new:
            o = old.get(r.id)
            if o is None or o.text != r.text or o.section != r.section:
                if o is not None:
                    rows.append({"op": "remove", "id": o.id, "section": o.section,
                                 "text": o.text, "direction": direction("remove", o.section),
                                 "who": who, "kind": "person", "words": _clean(words, 1000)})
                if not r.by:
                    r.by = self._by(who, words)
                rows.append({"op": "add", "id": r.id, "section": r.section, "text": r.text,
                             "direction": direction("add", r.section), "who": who,
                             "kind": "person", "words": _clean(words, 1000)})
        keep = {r.id for r in new}
        for o in old.values():
            if o.id not in keep:
                rows.append({"op": "remove", "id": o.id, "section": o.section, "text": o.text,
                             "direction": direction("remove", o.section), "who": who,
                             "kind": "person", "words": _clean(words, 1000)})
        # Rewritten with ids on every rule (the person's text otherwise kept).
        lines, section, out = text.splitlines(), None, []
        by_line = iter(new)
        nxt = next(by_line, None)
        for raw in lines:
            if raw.startswith("## "):
                section = section_of(raw[3:])
                out.append(raw)
                continue
            m = _LINE.match(raw)
            if section is not None and m and m.group(2).strip() and nxt is not None \
                    and not raw.startswith("#"):
                out.append(nxt.line())
                nxt = next(by_line, None)
            else:
                out.append(raw)
        with self._lock:
            self._write("\n".join(out), rows or [{"op": "save", "who": who, "kind": "person",
                                                  "words": _clean(words, 1000)}])
        return {"changes": rows, "problems": problems}
