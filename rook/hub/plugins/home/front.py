"""The house agent as front door (docs/design/front-door.md).

Every request that reaches the house agent (the phone's voice background via
``home.ask``, the web 1:1 huddle and chat, MCP clients, Telegram through a
bridged room, a huddle it chairs) goes through :meth:`FrontDoor.handle`. The
house agent triages it with a small tool set and the hub, not the model,
decides what it may say happened:

* **answer**: it answers (questions, chat, status);
* **do**: it does it itself, when the action is quick, reversible and the
  trust map says ``go`` for it (``do_action``);
* **dispatch**: it starts a coding session for a role (the lead developer by
  default) and tracks it (``dispatch_session``);
* **huddle**: it opens or continues a huddle episode for fuzzy or
  multi-discipline work (``open_huddle``, ``continue_huddle``);
* **ask**: anything the trust map, the playbook or reversibility says needs
  the person becomes a one-line **ask** (Yes / No / Explain) in the relayed
  approvals list (rook/hub/relay.py), where the phone's decision card and
  any client can answer it.

The reply always starts with the path the hub saw: ``Done.`` only after an
action call returned ok, ``I've got the team on it: <three-word name>.``
after a session or huddle started, ``I need you for one thing: ...`` after an
ask. The model's own claims are not trusted: a reply that says it did
something when no action ran is corrected (:func:`compose`).

**Disputes** between agents (:meth:`FrontDoor.settle`) are ruled from the
trust map or the playbook when an entry answers them (the ruling cites the
entry and lands in the digest, no ask), and asked otherwise (sacred actions,
guarded ones, gaps).

**The digest** (``front_decisions``) lists what the house agent decided for
the person without asking, each with the entry it cited; an **overturn**
only ever tightens: it raises the trust entry one level (the house agent's
``tighten``) or adds an "Ask me first" rule to the playbook (playbook.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from ....core import names
from ....core.authz import is_status_read
from ....core.db import open_db
from ....core.text import clip
from .onboard import OnboardError, Onboarding
from .playbook import ACT, TITLES, Playbook, PlaybookError, direction

log = logging.getLogger("rook.hub.plugins.home.front")

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
NAMESPACE = "home"
PATHS = ("answer", "do", "dispatch", "huddle", "ask", "onboard")
ASK_KINDS = ("action", "dispute", "gap", "playbook", "remember", "restore", "review", "vote")
ASK_STATES = ("open", "yes", "no", "expired", "cancelled")
DECISION_KINDS = ("did", "dispatched", "huddle", "ruled", "tightened")
LINE_MAX, EXPLAIN_MAX, MORE_MAX = 160, 600, 4000
ACTION_TIMEOUT = 60.0
#: How long a Yes waits for the hold its call meets (the trust map's) to show up.
HOLD_WAIT = 8.0
DIGEST_DEFAULT = 20
#: A worker counts as online when it was heard from this recently (as rook_workers).
ONLINE_SECS = 65

ROUTER_WHERE = (
    "You are the person's personal assistant and the front door of their Rook band: every "
    "request reaches you first. Understand what they want, protect their attention, and take "
    "exactly one path per request.")
ROUTER_RULES = """Paths (pick one; the hub tells the person which one you took):
- answer: questions, chat, status. Just reply (use band_status, knowledge_search, digest or waiting when you need facts).
- do: a quick, reversible action. Call do_action (worker, cap, args, summary); band_status with the worker lists its caps. The hub checks the trust map: it runs the action when the map says go, and otherwise turns it into a one-line ask for the person. When the playbook says to ask first, pass ask_first=true and the rule id.
- dispatch: one clear piece of code work. Call dispatch_session (instructions the session can follow alone, a branch name like fix/short-name, a title). It reports back on its own.
- huddle: fuzzy, big or multi-discipline work. Call open_huddle (title, roles, brief), or continue_huddle when a huddle on it exists.
- ask: something only the person can decide that no tool covers: call ask_person. Never use ask_person for an action you could run (deleting, restarting, sending): call do_action with ask_first=true instead, so their yes runs it with a one-line question (under 120 characters, answerable Yes or No), a 2-3 sentence plain explanation, and more detail.
Rules:
- Never say you did, started, sent or changed anything yourself: the hub writes that line from what your tools returned. Write only what the person needs beyond it, in one to three short sentences, plain words.
- When the person corrects how you route ("next time just do that", "ask me about X", "don't bother me with Y"), call playbook_edit with their exact words; tightening applies at once, loosening becomes a question for them.
- When they overturn a decision you made ("no, undo that rule", "overturn brave-otter-lamp"), call overturn with their words.
- When they answer one of your open asks in this message ("yes, do it", "no"), call answer_ask with their exact words.
- Never act on a family device, never touch secrets, never loosen the trust map."""


class FrontError(ValueError):
    """Refused; the text says why."""


def _now() -> float:
    return time.time()


def _one_line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _fn(name: str, description: str, props: dict | None = None,
        required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


TOOLS = {
    "do_action": _fn(
        "do_action", "Do a quick action yourself: run cap on worker (\"rook\" is the hub) with "
        "args. summary: what it does, in a few words (\"restart pianobar\"). The hub runs it when "
        "the trust map says go and it is reversible; otherwise it asks the person.",
        {"worker": {"type": "string"}, "cap": {"type": "string"}, "args": {"type": "object"},
         "summary": {"type": "string"},
         "ask_first": {"type": "boolean", "description": "the playbook says to ask first"},
         "rule": {"type": "string", "description": "the playbook rule id you follow"}},
        ["worker", "cap", "summary"]),
    "dispatch_session": _fn(
        "dispatch_session", "Start a coding session for a piece of code work and track it: "
        "instructions it can follow alone, branch (e.g. fix/short-name), title. role: who owns "
        "it (default the lead developer); location: a code location name when there are several.",
        {"instructions": {"type": "string"}, "branch": {"type": "string"},
         "title": {"type": "string"}, "role": {"type": "string"},
         "location": {"type": "string"}, "rule": {"type": "string"}},
        ["instructions", "branch", "title"]),
    "open_huddle": _fn(
        "open_huddle", "Open a huddle episode for fuzzy or multi-discipline work: title, roles "
        "(ids; empty: the default roles), and the brief the roles start from.",
        {"title": {"type": "string"}, "roles": {"type": "array", "items": {"type": "string"}},
         "brief": {"type": "string"}, "rule": {"type": "string"}},
        ["title", "brief"]),
    "continue_huddle": _fn(
        "continue_huddle", "Add to an existing huddle (its name or id): the roles pick it up.",
        {"huddle": {"type": "string"}, "text": {"type": "string"}}, ["huddle", "text"]),
    "ask_person": _fn(
        "ask_person", "Ask the person one thing only they can decide: line (one Yes/No question, "
        "under 120 characters), explain (2-3 plain sentences), more (detail on request).",
        {"line": {"type": "string"}, "explain": {"type": "string"},
         "more": {"type": "string"}}, ["line", "explain"]),
    "check_trust": _fn(
        "check_trust", "The trust map's ruling (and its citation) for a cap on a worker, or a "
        "push/merge to repo + branch.",
        {"cap": {"type": "string"}, "worker": {"type": "string"}, "repo": {"type": "string"},
         "branch": {"type": "string"}}),
    "playbook_edit": _fn(
        "playbook_edit", "Change your playbook when the person corrects you: op add (section "
        "do|ask|dispatch|huddle|answer|rule, text: the rule) or remove (id). words: their exact "
        "words. Tightening applies now; loosening becomes a question for them.",
        {"op": {"type": "string", "enum": ["add", "remove"]},
         "section": {"type": "string", "enum": list(TITLES)}, "text": {"type": "string"},
         "id": {"type": "string"}, "words": {"type": "string"}}, ["op", "words"]),
    "overturn": _fn(
        "overturn", "The person overturns a decision you made for them (its name or id from "
        "digest): it tightens the entry it cited. words: their exact words.",
        {"decision": {"type": "string"}, "words": {"type": "string"}}, ["decision", "words"]),
    "answer_ask": _fn(
        "answer_ask", "Pass on the person's answer to one of your open asks (its name or id): "
        "decision yes or no, words: their exact words from this message.",
        {"ask": {"type": "string"}, "decision": {"type": "string", "enum": ["yes", "no"]},
         "words": {"type": "string"}}, ["ask", "decision", "words"]),
    "digest": _fn("digest", "The decisions you made for the person (newest first), each with "
                  "the entry it cited, and your open asks.",
                  {"limit": {"type": "integer"}}),
    "waiting": _fn("waiting", "Everything waiting on the person across the band (held calls, "
                   "cards, questions, your asks)."),
    "role_config": _fn(
        "role_config", "A role's Claude Code setup for its persistent sessions (role: an id, "
        "or house for you). action get: model, effort, granted MCP servers and what can be "
        "granted. grant / revoke (server: an MCP server name): only when the person asks for "
        "it in this request (words: their exact words); granting a tool is theirs to decide, "
        "never yours to start. set (the person's request): persistent, voice, model, effort.",
        {"action": {"type": "string", "enum": ["get", "grant", "revoke", "set"]},
         "role": {"type": "string"}, "server": {"type": "string"},
         "words": {"type": "string"}, "persistent": {"type": "boolean"},
         "voice": {"type": "boolean"}, "model": {"type": "string"},
         "effort": {"type": "string"}}, ["action", "role"]),
    "band_status": _fn(
        "band_status", "The band's machines (\"rook\" is the hub): name, online, what each is, "
        "and the cap families it offers. With worker: that machine's caps (prefix narrows "
        "them), to pick one for do_action; shell.exec {\"argv\": [...]} runs a command on a "
        "desktop worker.",
        {"worker": {"type": "string"}, "prefix": {"type": "string"}}),
}
TOOLS.update({
    "note": _fn("note", "Brainstorming: add the gist of a new point to your running notes for "
                "this conversation (title: a short name for the idea, once you have one).",
                {"text": {"type": "string"}, "title": {"type": "string"}}, ["text"]),
    "offer_project": _fn(
        "offer_project", "The talk turned to building: offer once to set it up as a project "
        "(the hub asks the question). title: a short name for it.",
        {"title": {"type": "string"}}),
    "project_intake": _fn(
        "project_intake", "Record the person's answer while setting up a project. answer: start "
        "(yes, set it up) | decline | cancel | title | where (machine, kind folder|repo, new, "
        "path, repo) | protection (level toy|guarded|sacred) | touches (touches [{host, "
        "resource, what}] or nothing=true) | review (review_days). words: their exact words "
        "from this message. The hub asks the next question.",
        {"answer": {"type": "string"}, "words": {"type": "string"},
         "title": {"type": "string"}, "machine": {"type": "string"},
         "kind": {"type": "string", "enum": ["folder", "repo"]}, "new": {"type": "boolean"},
         "path": {"type": "string"}, "repo": {"type": "string"},
         "level": {"type": "string", "enum": ["toy", "guarded", "sacred"]},
         "touches": {"type": "array", "items": {"type": "object", "properties": {
             "host": {"type": "string"}, "resource": {"type": "string"},
             "what": {"type": "string"}}}},
         "nothing": {"type": "boolean"}, "review_days": {"type": "integer"}},
        ["answer", "words"]),
    "project_setup": _fn(
        "project_setup", "The person agreed to the plan read back: set the project up. words: "
        "their exact words.", {"words": {"type": "string"}}, ["words"]),
    "borrow": _fn(
        "borrow", "Stop a host's known normal state (a baseline resource) for a project, "
        "recorded in the ledger with how to restore it. words: their exact words.",
        {"resource": {"type": "string"}, "project": {"type": "string"},
         "words": {"type": "string"}}, ["resource", "words"]),
    "restore": _fn(
        "restore", "Bring borrowed state back (\"bring Strata back\"): what (resource, project "
        "or lease), words: their exact words.",
        {"what": {"type": "string"}, "words": {"type": "string"}}, ["what", "words"]),
    "ledger": _fn("ledger", "What is borrowed right now (what changed, why, how to restore), "
                  "and each host's normal state. host: one machine.",
                  {"host": {"type": "string"}}),
    "record_baseline": _fn(
        "record_baseline", "Record a sacred host's normal state for one resource: id, entry "
        "(the host's trust entry), title, normal (one line), stop and restore {worker, cap, "
        "args}: the exact calls.",
        {"id": {"type": "string"}, "entry": {"type": "string"}, "title": {"type": "string"},
         "normal": {"type": "string"}, "stop": {"type": "object"},
         "restore": {"type": "object"}, "words": {"type": "string"}},
        ["id", "entry", "title", "normal", "stop", "restore"]),
})
ONBOARD_TOOLS = ("note", "offer_project", "project_intake", "project_setup", "borrow", "restore",
                 "ledger", "record_baseline")
KNOWLEDGE = _fn("knowledge_search", "Search the band's shared knowledge wiki (excerpts with "
                "slugs).", {"query": {"type": "string"}}, ["query"])

_LEAD_DONE = re.compile(r"^\s*(?:all\s+)?(?:done|finished|completed)\b[\s.!,:;-]*", re.I)
_CLAIM = re.compile(
    r"\b(?:i(?:'ve| have)|i)\s+(?:just\s+)?(?:done|did|restarted|started|stopped|deployed|merged|"
    r"created|deleted|removed|sent|turned|launched|kicked off|opened|changed|updated|fixed|"
    r"ran|run|set up|installed)\b", re.I)


#: While setting a project up only the answers are recorded: a model line
#: saying it already set or marked something is dropped (the hub's question
#: and, after setup, its read-back say what is set).
_SET_CLAIM = re.compile(r"\b(?:i(?:'ve| have)?|it'?s|it is|that'?s|that is)\s+(?:now\s+|just\s+)?"
                        r"(?:set|marked|made|configured|created|saved|recorded)\b", re.I)

_GONE = re.compile(r"(?:^|[._-])(delete|remove|rm|drop|purge|wipe|erase|format|destroy|reset|"
                   r"uninstall|deauth|revoke|kill|truncate|prune|forget)(?:$|[._-])", re.I)
_GONE_ARGV = re.compile(r"^(rm|rmdir|dd|mkfs\S*|shred|wipefs|fdisk|parted|shutdown|reboot|"
                        r"poweroff|halt|kill|killall|pkill|truncate)$")


def irreversible(cap: str, kind: str, tags: tuple | list, args: Any) -> str:
    """Why an action is not easily undone ("" when it looks reversible): an
    admin or destructive cap, a cap named for removing something, or a shell
    command that deletes, wipes, kills or powers off."""
    if kind == "admin":
        return f"{cap} is {'destructive' if 'destructive' in (tags or ()) else 'admin'}"
    if _GONE.search(cap.split(".", 1)[-1]):
        return f"{cap} removes something"
    if isinstance(args, dict):
        argv = args.get("argv") or args.get("cmd") or args.get("command")
        words = argv if isinstance(argv, list) else str(argv or "").split()
        words = [str(w).rsplit("/", 1)[-1] for w in words if str(w).strip()]
        if words and words[0] == "sudo":
            words = words[1:]
        if words and _GONE_ARGV.match(words[0]):
            return f"`{' '.join(words[:3])}` is not easily undone"
        if words[:2] in (["git", "push"], ["git", "reset"]) and ("--force" in words or "-f" in words
                                                                 or "--hard" in words):
            return "a force push or hard reset is not easily undone"
    return ""


def compose(state: dict, content: str) -> tuple[str, str]:
    """The reply and the path, from what the tools actually did (``state``),
    never from the model's own claims."""
    text = " ".join(str(content or "").split())
    did = [a for a in state["did"] if a.get("ok")]
    started = state["started"]
    asked = state["asked"]
    said = [x for x in state.get("onboard") or () if x]
    heads = []
    path = "answer"
    if did:
        path = "do"
        heads.append("Done: " + "; ".join(a["summary"] for a in did) + ".")
    if started:
        path = started[0]["path"] if path == "answer" else path
        heads.append("I've got the team on it: " + ", ".join(s["name"] for s in started) + ".")
    if asked:
        if path == "answer":
            path = "ask"
        a = asked[0]
        heads.append(f"I need you for one thing: {a['line']} (Yes / No / Explain: {a['name']})")
    ask_last = ""
    if said:
        # The onboarding line is the hub's, word for word: what setup did
        # leads; a question (the offer, the next intake question, the plan
        # read back) ends the reply, so it is the last thing they hear.
        if path in ("answer", "ask"):
            path = "onboard"
        if said[-1].rstrip().endswith("?"):
            ask_last = said[-1]
        else:
            heads.append(said[-1])
    fixed = {a["summary"] for a in did}
    failed = [a for a in state["did"] if not a.get("ok") and not a.get("asked")
              and a["summary"] not in fixed]
    if failed:
        heads.append("Not done: " + "; ".join(f"{a['summary']} ({a.get('error') or 'failed'})"
                                              for a in failed) + ".")
    if not did and not started and not state.get("acted"):
        # Only the hub says an action happened. A model claiming one is corrected.
        text = _LEAD_DONE.sub("", text)
        if _CLAIM.search(text):
            text = "Nothing has been done yet. " + text
    elif did:
        text = _LEAD_DONE.sub("", text)
    if ask_last and (text.rstrip().endswith("?") or (
            not did and not started and not state.get("acted")
            and (_CLAIM.search(text) or _SET_CLAIM.search(text)))):
        text = ""        # one question at a time, and nothing claimed as set: the hub's
    out = " ".join(p for p in heads + [text, ask_last] if p).strip()
    return out or "(no answer)", path


# -- the store ---------------------------------------------------------------------------

class FrontStore:
    """Requests, asks and decisions in ``<home data>/front.db``; speakable
    names (kind ``front-ask``, ``decision``) beside them."""

    def __init__(self, path: str) -> None:
        import threading
        self._lock = threading.Lock()
        self.db = open_db(path, migrations=MIGRATIONS, ns=NAMESPACE)
        with self._lock:
            names.ensure_table(self.db)
            self.db.commit()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    def _name(self, kind: str, rid: str) -> str:
        with self._lock:
            n = names.assign(self.db, kind, rid)
            self.db.commit()
            return n

    def resolve(self, kind: str, table: str, ref: str) -> dict | None:
        ref = str(ref or "").strip()
        if not ref:
            return None
        rows = self._q(f"SELECT * FROM {table} WHERE id=?", (ref,))
        if not rows:
            with self._lock:
                hits = names.find(self.db, kind, ref)
            rows = self._q(f"SELECT * FROM {table} WHERE id=?", (hits[0],)) if hits else []
        return rows[0] if rows else None

    # requests
    def add_request(self, source: str, asker: str, text: str) -> str:
        rid = "fr_" + uuid.uuid4().hex[:12]
        self._x("INSERT INTO front_requests(id, created, source, asker, text) VALUES (?,?,?,?,?)",
                (rid, _now(), source, asker, clip(text, 4000)))
        return rid

    def finish_request(self, rid: str, path: str, reply: str, data: dict) -> None:
        self._x("UPDATE front_requests SET path=?, reply=?, data=? WHERE id=?",
                (path, clip(reply, 4000), json.dumps(data, default=str), rid))

    def requests(self, limit: int = 30) -> list[dict]:
        rows = self._q("SELECT * FROM front_requests ORDER BY created DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r.get("data") or "{}")
        return rows

    # asks
    def add_ask(self, row: dict) -> dict:
        aid = "fa_" + uuid.uuid4().hex[:12]
        now = _now()
        self._x("INSERT INTO front_asks(id, created, expires, kind, state, line, explain, more, "
                "cite, action, change, options, source, room, request, asker) "
                "VALUES (?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?)",
                (aid, now, now + float(row.get("ttl") or 72 * 3600), row["kind"], row["line"],
                 row["explain"], row.get("more") or "", json.dumps(row.get("cite") or {}),
                 json.dumps(row.get("action") or {}), json.dumps(row.get("change") or {}),
                 json.dumps(row.get("options") or []), row.get("source") or "",
                 row.get("room") or "", row.get("request") or "", row.get("asker") or ""))
        self._name("front-ask", aid)
        return self.ask(aid)

    @staticmethod
    def _ask_row(r: dict) -> dict:
        for k in ("cite", "action", "change", "result"):
            r[k] = json.loads(r.get(k) or "{}")
        r["options"] = json.loads(r.get("options") or "[]")
        return r

    def ask(self, aid: str) -> dict | None:
        rows = self._q("SELECT * FROM front_asks WHERE id=?", (aid,))
        if not rows:
            return None
        r = self._ask_row(rows[0])
        with self._lock:
            r["name"] = names.name_of(self.db, "front-ask", aid)
        return r

    def find_ask(self, ref: str) -> dict | None:
        r = self.resolve("front-ask", "front_asks", ref)
        return self.ask(r["id"]) if r else None

    def asks(self, state: str | None = "open", limit: int = 50) -> list[dict]:
        self._x("UPDATE front_asks SET state='expired' WHERE state='open' AND expires<?",
                (_now(),))
        if state:
            rows = self._q("SELECT * FROM front_asks WHERE state=? ORDER BY created LIMIT ?",
                           (state, limit))
        else:
            rows = self._q("SELECT * FROM front_asks ORDER BY created DESC LIMIT ?", (limit,))
        out = [self._ask_row(r) for r in rows]
        with self._lock:
            got = names.names_of(self.db, "front-ask", [r["id"] for r in out])
        for r in out:
            r["name"] = got.get(r["id"])
        return out

    def close_ask(self, aid: str, state: str, by: str, quote: str = "", answer: str = "") -> bool:
        return self._x("UPDATE front_asks SET state=?, decided=?, decided_by=?, quote=?, answer=? "
                       "WHERE id=? AND state='open'",
                       (state, _now(), by, quote[:1000], answer[:1000], aid)) == 1

    def set_result(self, aid: str, result: dict) -> None:
        self._x("UPDATE front_asks SET result=? WHERE id=?",
                (json.dumps(result, default=str)[:8000], aid))

    # decisions
    def add_decision(self, kind: str, summary: str, cite: dict, *, source: str = "",
                     room: str = "", request: str = "", data: dict | None = None) -> dict:
        did = "fd_" + uuid.uuid4().hex[:12]
        self._x("INSERT INTO front_decisions(id, created, kind, summary, cite, source, room, "
                "request, data) VALUES (?,?,?,?,?,?,?,?,?)",
                (did, _now(), kind, _one_line(summary, 300), json.dumps(cite or {}), source,
                 room, request, json.dumps(data or {}, default=str)))
        self._name("decision", did)
        return self.decision(did)

    @staticmethod
    def _dec_row(r: dict) -> dict:
        r["cite"] = json.loads(r.get("cite") or "{}")
        r["data"] = json.loads(r.get("data") or "{}")
        r["overturned"] = json.loads(r["overturned"]) if r.get("overturned") else None
        return r

    def decision(self, did: str) -> dict | None:
        rows = self._q("SELECT * FROM front_decisions WHERE id=?", (did,))
        if not rows:
            return None
        r = self._dec_row(rows[0])
        with self._lock:
            r["name"] = names.name_of(self.db, "decision", did)
        return r

    def find_decision(self, ref: str) -> dict | None:
        r = self.resolve("decision", "front_decisions", ref)
        return self.decision(r["id"]) if r else None

    def decisions(self, limit: int = DIGEST_DEFAULT, since: float = 0.0) -> list[dict]:
        rows = [self._dec_row(r) for r in self._q(
            "SELECT * FROM front_decisions WHERE created>=? ORDER BY created DESC LIMIT ?",
            (since, limit))]
        with self._lock:
            got = names.names_of(self.db, "decision", [r["id"] for r in rows])
        for r in rows:
            r["name"] = got.get(r["id"])
        return rows

    def overturn(self, did: str, info: dict) -> bool:
        return self._x("UPDATE front_decisions SET overturned=? WHERE id=? AND overturned IS NULL",
                       (json.dumps(info, default=str), did)) == 1


# -- the front door ------------------------------------------------------------------------

class FrontDoor:
    """Routing, asks, disputes and the digest for one house agent
    (:class:`rook.hub.plugins.home.HomeAgent`)."""

    def __init__(self, home: Any) -> None:
        self.home = home
        self._store: FrontStore | None = None
        self._playbook: Playbook | None = None
        self._tasks: set[asyncio.Task] = set()
        self.onboard = Onboarding(self)

    # -- plumbing --------------------------------------------------------------
    @property
    def store(self) -> FrontStore:
        if self._store is None:
            self._store = FrontStore(str(self.home.data_dir / "front.db"))
        return self._store

    @property
    def playbook(self) -> Playbook:
        if self._playbook is None:
            self._playbook = Playbook(str(self.home.data_dir / "playbook.md"))
        return self._playbook

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        for t in list(self._tasks):
            t.cancel()

    @property
    def node(self) -> Any:
        return self.home._node

    def plugin(self, ns: str) -> Any:
        return self.node.plugin(ns) if self.node is not None else None

    def trust(self) -> Any:
        from ..trust import node_store
        return node_store(self.node) if self.node is not None else None

    def approvals(self) -> Any:
        authz = getattr(getattr(self.node, "client", None), "authz", None)
        return getattr(authz, "approvals", None)

    def relay(self) -> Any:
        ap = self.plugin("approvals")
        return getattr(ap, "relay", None)

    def principal(self) -> Any:
        """The house agent's own policy principal (an agent: the trust map and
        holds apply to it like to any agent)."""
        from ...policy import Principal
        return Principal(f"home:{self.home.agent_name}", "token", "agent",
                         label=self.home.identity)

    def person_name(self) -> str:
        att = self.plugin("attention")
        with contextlib.suppress(Exception):
            name = str(att.settings.get("person") or "").strip()
            if name and name != "the person":
                return name
        return "the person"

    # -- workers and rulings ---------------------------------------------------------
    def _roster(self) -> tuple[dict, str | None]:
        node = self.node
        roster: dict = {}
        with contextlib.suppress(Exception):
            roster.update(dict(getattr(getattr(node, "client", None), "workers", {}) or {}))
        hub_id = getattr(node, "worker_id", None)
        with contextlib.suppress(Exception):
            if hub_id:
                roster[hub_id] = node.entry()
        return roster, hub_id

    def band_status(self, worker: str = "", prefix: str = "") -> dict:
        """Read-only: the machines on the band, or one machine's caps."""
        roster, hub_id = self._roster()
        now = time.time()

        def row(wid: str, e: dict) -> dict:
            on_hub = wid == hub_id
            age = None if on_hub else round(max(0.0, now - float(e.get("last_seen") or 0.0)))
            facts = e.get("facts") if isinstance(e.get("facts"), dict) else {}
            caps = [c for c in (e.get("caps") or []) if isinstance(c, str)]
            out = {"name": "rook" if on_hub else str(e.get("name") or wid),
                   "online": on_hub or (age is not None and age <= ONLINE_SECS),
                   "description": clip(str(e.get("description") or ""), 120),
                   "families": sorted({c.split(".")[0] for c in caps})[:40]}
            if age is not None:
                out["last_seen_s"] = age
            if facts.get("os"):
                out["os"] = facts["os"]
            if e.get("device"):
                out["device"] = True
            return out
        w = worker.strip()
        if not w:
            rows = sorted((row(k, e) for k, e in roster.items() if isinstance(e, dict)),
                          key=lambda r: (r["name"] != "rook", not r["online"], r["name"].lower()))
            return {"ok": True, "workers": rows[:60],
                    "online": sum(1 for r in rows if r["online"]), "total": len(rows)}
        for k, e in roster.items():
            if not isinstance(e, dict):
                continue
            name = "rook" if k == hub_id else str(e.get("name") or k)
            if w.lower() in (name.lower(), k.lower()) or (k == hub_id and w.lower() == "hub"):
                caps = sorted(c for c in (e.get("caps") or []) if isinstance(c, str)
                              and c.startswith(prefix))
                return {"ok": True, **row(k, e), "caps": caps[:150],
                        **({"more_caps": len(caps) - 150} if len(caps) > 150 else {})}
        raise FrontError(f"no worker {w!r} on the band")

    def resolve_worker(self, worker: str, cap: str) -> dict:
        """``{wid, name, on_hub, tier, tags}`` for a cap on a worker, or FrontError."""
        from ....core import authz as core_authz
        roster, hub_id = self._roster()
        w = str(worker or "").strip()
        if not w:
            raise FrontError("worker: the worker's name (\"rook\" is the hub)")
        on_hub = w.lower() in ("rook", "hub") or (hub_id is not None and w == hub_id)
        wid, entry = None, None
        if on_hub and hub_id:
            wid, entry = hub_id, roster.get(hub_id) or {}
        else:
            for k, e in roster.items():
                if k == w or str(e.get("name") or "").lower() == w.lower():
                    wid, entry = k, e
                    break
        if wid is None and not on_hub:
            raise FrontError(f"no worker {w!r} on the band")
        entry = entry or {}
        caps = entry.get("caps")
        if isinstance(caps, (list, tuple)) and caps and cap not in caps:
            fam = [c for c in caps if c.split(".")[0] == cap.split(".")[0]][:12]
            raise FrontError(f"{entry.get('name') or w} has no cap {cap!r}"
                             + (f" (it has {', '.join(fam)})" if fam else ""))
        declared = (entry.get("tiers") or {}).get(cap)
        tier = core_authz.effective_tier(cap, declared)
        tags = tuple(core_authz.builtin_tags(cap))
        return {"wid": wid, "name": "rook" if on_hub else str(entry.get("name") or w),
                "on_hub": on_hub, "tier": tier, "tags": tags}

    def ruling(self, cap: str, w: dict) -> Any:
        st = self.trust()
        if st is None:
            return None
        return st.rule(cap, w["tier"], w["tags"], worker=w["name"], worker_id=w["wid"] or "",
                       on_hub=w["on_hub"])

    @staticmethod
    def cite_of(r: Any, rule: Any = None) -> dict:
        """A citation for the digest and asks: the trust entry (or the gap),
        and the playbook rule when one was followed."""
        out: dict = {}
        if r is not None:
            e = r.entry
            out["trust"] = {"id": e["id"] if e else "unlisted", "level": r.level,
                            "version": r.version, "tier": r.tier, "text": r.cite,
                            **({"exception": r.exception["id"]} if r.exception else {})}
            if e and (e.get("set_by") or {}).get("words"):
                out["trust"]["words"] = e["set_by"]["words"]
        if rule is not None:
            out["playbook"] = {"id": rule.id, "section": rule.section, "text": rule.text}
        return out

    @staticmethod
    def cite_line(cite: dict) -> str:
        parts = []
        t = cite.get("trust") or {}
        if t:
            if t.get("id") and t["id"] != "unlisted":
                words = f': "{t["words"]}"' if t.get("words") else ""
                parts.append(f'trust entry "{t["id"]}" ({t.get("level")}){words}')
            else:
                parts.append(f"no trust entry covers it (unlisted: {t.get('level')})")
            if t.get("exception"):
                parts.append(f"exception {t['exception']}")
        p = cite.get("playbook") or {}
        if p:
            parts.append(f'playbook rule {p["id"]} ("{_one_line(p.get("text"), 90)}")')
        if cite.get("note"):
            parts.append(cite["note"])
        return "; ".join(parts)

    def _rule(self, rid: Any) -> Any:
        rid = str(rid or "").strip()
        return self.playbook.get(rid) if rid else None

    # -- running an action as the house agent -------------------------------------------------
    async def run_action(self, action: dict, *, approved_by: str = "",
                         relay: dict | None = None) -> dict:
        """Run ``action`` ({worker, cap, args, wid}) as the house agent. With
        ``approved_by`` (the person said yes to the ask), a hold the call
        meets for this exact call is approved with their decision."""
        node = self.node
        client = getattr(node, "client", None)
        principal = self.principal()
        cap, args = action["cap"], dict(action.get("args") or {})
        t0 = _now()

        async def call() -> dict:
            if action.get("borrow"):
                # A baseline resource's own stop or restore call: the approval
                # layer lets an exception naming that resource rule on it.
                from ...trust import borrowing
                borrowing.set({"resource": action["borrow"], "cap": cap,
                               "worker": action.get("worker") or "", "principal": principal.id})
            with self.home._as_self():
                if client is None:
                    return await asyncio.wait_for(node.dispatch(cap, args, self.home.identity),
                                                  ACTION_TIMEOUT)
                return await client.call(cap=cap, args=args, target=action.get("wid"),
                                         timeout=ACTION_TIMEOUT, identity=self.home.identity,
                                         principal=principal)

        task = asyncio.get_running_loop().create_task(call())
        if approved_by:
            ap = self.approvals()
            deadline = time.monotonic() + HOLD_WAIT
            while ap is not None and not task.done() and time.monotonic() < deadline:
                hold = None
                with contextlib.suppress(Exception):
                    hold = next((h for h in ap.queue.pending()
                                 if h.get("principal") == principal.id and h.get("cap") == cap
                                 and h.get("target_id") == action.get("wid")
                                 and float(h.get("created") or 0) >= t0 - 1), None)
                if hold is not None:
                    with contextlib.suppress(Exception):
                        ap.decide(hold["id"], True, approved_by, relay=relay)
                    break
                await asyncio.sleep(0.25)
        try:
            reply = await task
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": f"no reply within {ACTION_TIMEOUT:.0f}s"}
        except Exception as e:  # noqa: BLE001 - the error goes back to the router
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if not isinstance(reply, dict):
            reply = {"ok": True, "result": reply}
        inner = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        ok = bool(reply.get("ok", True)) and not reply.get("error") and not reply.get("held") \
            and not reply.get("denied") and inner.get("ok", True) is not False
        from ....band_mcp.secret_mask import scrub
        out = {"ok": ok, "result": clip(json.dumps(scrub(reply.get("result")), default=str), 1500)}
        if not ok:
            out["error"] = _one_line(reply.get("error") or inner.get("error") or "it failed", 300)
            if reply.get("held"):
                out["held"] = reply.get("hold")
        self.home.journal("home.front.action", {"worker": action.get("worker"), "cap": cap,
                                                "approved_by": approved_by or None},
                          {"ok": ok, **({"error": out.get("error")} if not ok else {})})
        return out

    # -- asks --------------------------------------------------------------------------
    def make_ask(self, kind: str, line: str, explain: str, more: str = "", *, cite: dict | None = None,
                 action: dict | None = None, change: dict | None = None, source: str = "",
                 room: str = "", request: str = "", asker: str = "",
                 options: list | None = None) -> dict:
        line = _one_line(line, LINE_MAX)
        if len(line) < 4:
            raise FrontError("line: one short Yes/No question")
        explain = _one_line(explain, EXPLAIN_MAX) or line
        ttl = float(self.home.settings.get("ask_ttl_hours") or 72) * 3600
        row = self.store.add_ask({
            "kind": kind, "line": line, "explain": explain,
            "more": clip(str(more or ""), MORE_MAX), "cite": cite or {}, "action": action or {},
            "change": change or {}, "options": options or [], "source": source, "room": room,
            "request": request, "asker": asker, "ttl": ttl})
        self.home.journal("home.front.ask", {"kind": kind, "ask": row["id"], "name": row["name"]},
                          {"ok": True, "result": {"line": line}})
        return row

    def relay_items(self) -> list[dict]:
        """The open asks as relayed-approvals items (rook/hub/relay.py)."""
        out = []
        now = _now()
        for a in self.store.asks("open"):
            act = a.get("action") or {}
            item = {
                "kind": "house", "id": a["id"], "name": a.get("name"),
                "who": f"the house agent ({self.home.agent_name})",
                "what": f"ask you: {a['line']}", "line": a["line"], "explain": a["explain"],
                "risk": act.get("tier") or a["kind"], "from": {"house": a["kind"]},
                "created": a["created"], "age_s": int(now - a["created"]),
                "options": ["Yes", "No", "Explain"],
                "cap": act.get("cap") or "", "action": (act.get("args") or {}).get("action")
                if isinstance(act.get("args"), dict) else None,
                "tier": act.get("tier") or "write", "tags": list(act.get("tags") or []),
                "hub": bool(act.get("on_hub")) and bool(act.get("cap")),
                "worker": act.get("worker")}
            out.append(item)
        return out

    def explain(self, ref: str, more: bool = False) -> dict:
        a = self.store.find_ask(ref)
        if a is None:
            raise FrontError(f"no ask {ref!r}")
        out = {"ask": a["id"], "name": a.get("name"), "line": a["line"], "state": a["state"],
               "explain": a["explain"], "cite": self.cite_line(a.get("cite") or {})}
        if more:
            out["more"] = a.get("more") or a["explain"]
        return out

    async def answer(self, ref: str, decision: str, *, by: str, quote: str = "",
                     relay: dict | None = None, reason: str = "") -> dict:
        """The person's Yes or No to an ask (the dashboard, a relayed client,
        the phone's card). It acts on a Yes and offers to remember it."""
        a = self.store.find_ask(ref)
        if a is None:
            raise FrontError(f"no ask {ref!r} (it may be answered or expired)")
        if decision not in ("yes", "no", "approve", "deny"):
            raise FrontError("decision: yes or no")
        yes = decision in ("yes", "approve")
        if a["state"] != "open":
            raise FrontError(f"{a.get('name') or a['id']} is already {a['state']}")
        if not self.store.close_ask(a["id"], "yes" if yes else "no", by, quote,
                                    reason if not yes else ""):
            raise FrontError(f"{a.get('name') or a['id']} was answered a moment ago")
        result: dict = {"ask": a["id"], "name": a.get("name"), "answer": "yes" if yes else "no"}
        if yes:
            result.update(await self._carry_out(a, by, relay))
        elif a["kind"] == "review":
            result.update(self.onboard.answer_review(a, False, by))
        elif a["kind"] == "vote":
            result.update(await self._vote_answer(a, False, by))
        else:
            result["note"] = "Nothing was done."
        self.store.set_result(a["id"], result)
        self._tell_room(a, ("Yes" if yes else "No") + f" from {by}: {a['line']}"
                        + (f" {result.get('note')}" if result.get("note") else ""))
        self.home.journal("home.front.answer", {"ask": a["id"], "decision": result["answer"],
                                                "by": by},
                          {"ok": True, "result": {k: v for k, v in result.items()
                                                  if k in ("note", "remember", "ok")}})
        return result

    async def _vote_answer(self, a: dict, yes: bool, by: str) -> dict:
        """A vote that reached the person (rook/hub/plugins/huddle/table.py):
        Yes carries it out with the hub's credential, No drops it."""
        table = getattr(getattr(self.plugin("huddle"), "service", None), "table", None)
        mid = (a.get("action") or {}).get("motion")
        if table is None or not mid:
            return {"ok": False, "note": "The vote is gone (huddles are off)."}
        try:
            out = await table.answer(mid, yes, by)
        except Exception as e:  # noqa: BLE001 - say why
            return {"ok": False, "note": f"Not done: {str(e)[:300]}"}
        return {"ok": bool(out.get("ok")), "note": out.get("note") or ""}

    async def _carry_out(self, a: dict, by: str, relay: dict | None) -> dict:
        kind = a["kind"]
        if kind == "vote":
            return await self._vote_answer(a, True, by)
        if kind in ("action", "restore") and a.get("action"):
            act = a["action"]
            closing = (act.get("after") or {}).get("close_lease")
            if closing and not self.onboard.lease_open(closing):
                return {"ok": True, "note": "Already restored; nothing to do."}
            out = await self.run_action(act, approved_by=f"{by} (yes to house ask "
                                        f"{a.get('name') or a['id']})", relay=relay)
            res: dict = {"ok": out["ok"], "note": (f"Done: {act.get('summary')}." if out["ok"]
                                                  else f"Not done: {out.get('error')}.")}
            if out["ok"] and act.get("after"):
                res["ledger"] = self.onboard.apply_after(act, by, f"yes to {a['line']}")
            elif out["ok"] and kind == "action":
                res["remember"] = self._offer_remember(a)
            return res
        if kind == "review":
            return self.onboard.answer_review(a, True, by)
        if kind in ("playbook", "remember"):
            ch = a.get("change") or {}
            if ch.get("trust_propose"):
                st = self.trust()
                try:
                    prop = st.propose(ch["trust_propose"], by=self.home.identity,
                                      principal=self.principal().id,
                                      why=f"{by} said yes to: {a['line']}")
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "note": f"Could not propose the trust change: {e}"}
                return {"ok": True, "note": f"Proposed on the Trust page ({prop['id']}): accept "
                                            "it there to change the map."}
            try:
                row = self.playbook.change(ch.get("op") or "add", ch.get("section") or "",
                                           ch.get("text") or "", ch.get("id") or "", who=by,
                                           kind="person", words=ch.get("words") or a["line"],
                                           via=f"house ask {a.get('name') or a['id']}")
            except PlaybookError as e:
                return {"ok": False, "note": f"The playbook did not change: {e}"}
            return {"ok": True, "note": f"Playbook updated ({row['id']}).", "playbook": row}
        if kind in ("dispute", "gap"):
            res = {"ok": True, "note": "Noted; the team carries on with your answer."}
            if kind == "dispute":
                res["remember"] = self._offer_remember(a)
            elif a.get("request"):
                res["note"] = "Noted; I'm acting on it."
                self.follow_up(a, "yes", by)
            return res
        return {"ok": True}

    def follow_up(self, a: dict, answer: str, by: str) -> None:
        """After the person answers a question the router asked (a ``gap``
        ask from a request), the house agent picks the request up again with
        their answer and acts on it; the reply goes to the room it came from."""
        rows = self.store._q("SELECT * FROM front_requests WHERE id=?", (a["request"],))
        if not rows:
            return
        req = rows[0]
        text = (f"{by} answered {answer} to your question \"{a['line']}\" about their request: "
                f"\"{clip(req['text'], 1500)}\". Carry the request out now as that answer says "
                "(or, for no, leave it and say so in a line).")

        async def run() -> None:
            try:
                out = await self.handle(text, source="answer", asker=req["asker"] or by,
                                        room=a.get("room") or "",
                                        approved=(by, a) if answer == "yes" else None)
            except Exception as e:  # noqa: BLE001 - the ask stays answered; the log says why
                log.warning("front door: follow-up on %s failed: %s", a["id"], e)
                return
            cur = (self.store.ask(a["id"]) or {}).get("result") or {}
            self.store.set_result(a["id"], {**cur, "followed_up": out["request"],
                                            "path": out["path"], "note": out["answer"][:300]})
            if a.get("room"):
                self._tell_room(a, out["answer"])

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _offer_remember(self, a: dict) -> str | None:
        """After acting on a Yes, offer to remember it: a playbook rule (the
        playbook made it ask), or a proposal to loosen the trust entry (the
        map made it ask). One offer per ask; none when the rule exists."""
        if not self.home.settings.get("offer_remember", True):
            return None
        cite = a.get("cite") or {}
        act = a.get("action") or {}
        trust = cite.get("trust") or {}
        if a["kind"] == "dispute":
            q = (a.get("change") or {}).get("question") or a["line"]
            change = {"op": "add", "section": "rule",
                      "text": f"{_one_line(q, 200)}: {self.person_name()} said yes.",
                      "words": f"yes to {a['line']}"}
            line = "Remember that answer for next time?"
        elif trust.get("tier") in ("ask", "person") and trust.get("id") not in (None, "unlisted"):
            if trust.get("level") == "sacred":
                return None          # sacred stays the person's; never offered to loosen
            change = {"trust_propose": {"op": "set_entry", "data": {"id": trust["id"],
                                                                     "level": "ship-it"}}}
            line = f"Let agents just go on {trust['id']} next time?"
        else:
            text = f"{act.get('summary') or a['line']} ({act.get('cap')} on {act.get('worker')})"
            if any(r.section == "do" and r.text.lower() == text.lower()
                   for r in self.playbook.rules()):
                return None
            change = {"op": "add", "section": "do", "text": text,
                      "words": f"yes to {a['line']}"}
            line = f"Next time just {act.get('summary') or 'do that'} without asking?"
        if change.get("trust_propose"):
            # A trust proposal the person still accepts on the Trust page.
            st = self.trust()
            entry = next((e for e in (st.current().get("entries") or [])
                          if e.get("id") == trust["id"]), None) if st is not None else None
            if entry is None:
                return None
            change["trust_propose"]["data"] = {**{k: entry[k] for k in ("id", "title", "kind",
                                                                         "targets", "description")
                                                  if k in entry}, "level": "ship-it"}
        r = self.make_ask("remember", line,
                          f"You said yes to: {a['line']} Saying yes here "
                          + ("proposes the change on the Trust page; you accept it there."
                             if change.get("trust_propose") else
                             "adds it to my playbook, so next time I just do it and tell you."),
                          cite=cite, change=change, source=a.get("source") or "",
                          room=a.get("room") or "", request=a.get("request") or "",
                          asker=a.get("asker") or "")
        return r.get("name")

    def _tell_room(self, a: dict, text: str) -> None:
        if not a.get("room"):
            return
        hp = self.plugin("huddle")
        svc = getattr(hp, "service", None)
        with contextlib.suppress(Exception):
            svc._send(a["room"], self.home.identity, text, [], quiet=False)

    # -- the digest and overturning ---------------------------------------------------
    def decide(self, kind: str, summary: str, cite: dict, **where) -> dict:
        return self.store.add_decision(kind, summary, cite, **where)

    def digest(self, limit: int = DIGEST_DEFAULT, since: float = 0.0) -> dict:
        rows = self.store.decisions(max(1, min(int(limit or DIGEST_DEFAULT), 200)), since)
        items = [{"id": r["id"], "name": r.get("name"), "kind": r["kind"],
                  "summary": r["summary"], "cite": self.cite_line(r["cite"]),
                  "cite_ref": r["cite"], "created": r["created"], "room": r.get("room") or "",
                  "overturned": r.get("overturned")} for r in rows]
        live = [i for i in items if not i["overturned"]]
        if not items:
            spoken = "I haven't decided anything for you yet."
        else:
            head = (f"{len(live)} decision{'s' if len(live) != 1 else ''} I made for you"
                    if live else "Nothing I decided is standing")
            spoken = head + (": " + "; ".join(f"{i['summary']} ({i['name']})"
                                               for i in live[:5]) if live else "") + "."
        asks = [{"id": a["id"], "name": a.get("name"), "line": a["line"], "kind": a["kind"]}
                for a in self.store.asks("open")]
        return {"spoken": spoken, "decisions": items, "asks": asks}

    def overturn(self, ref: str, *, by: str, words: str, kind: str = "person") -> dict:
        """Overturn a decision: tighten what it cited. ``kind`` person (the
        dashboard) or house (the person said so to the house agent)."""
        from ...trust import LEVELS, Actor, TrustError
        d = self.store.find_decision(ref)
        if d is None:
            raise FrontError(f"no decision {ref!r} (digest lists them)")
        if d.get("overturned"):
            raise FrontError(f"{d.get('name') or d['id']} is already overturned")
        words = _one_line(words, 1000)
        if len(words) < 2:
            raise FrontError("words: the person's own words for overturning it")
        cite = d.get("cite") or {}
        effects: list[str] = []
        p = cite.get("playbook") or {}
        if p and p.get("section") in ACT and self.playbook.get(p.get("id")):
            row = self.playbook.change("remove", rid=p["id"], who=by, kind="person"
                                       if kind == "person" else "house", words=words,
                                       via=f"overturned {d.get('name') or d['id']}")
            effects.append(f"removed playbook rule {row['id']}")
        t = cite.get("trust") or {}
        st = self.trust()
        tightened = False
        if t.get("id") and t["id"] != "unlisted" and st is not None:
            entry = next((e for e in st.current().get("entries") or [] if e["id"] == t["id"]), None)
            if entry is not None and entry.get("level") != "sacred":
                up = LEVELS[min(LEVELS.index(entry["level"]) + 1, len(LEVELS) - 1)]
                new = {k: v for k, v in entry.items() if k not in ("set_by", "rev", "confirmed",
                                                                   "confirmed_by")}
                new["level"] = up
                try:
                    st.change("set_entry", new, Actor(kind if kind in ("person", "house")
                                                      else "house", by, words=words,
                                                      principal=self.principal().id,
                                                      session=f"overturn {d['id']}"))
                    effects.append(f"trust entry {t['id']}: {entry['level']} -> {up}")
                    tightened = True
                except TrustError as e:
                    effects.append(f"trust entry {t['id']} unchanged ({e})")
        if not tightened:
            subject = d["data"].get("subject") or d["summary"]
            row = self.playbook.change("add", "ask", f"Ask me before: {_one_line(subject, 200)}",
                                       who=by, kind="person" if kind == "person" else "house",
                                       words=words, via=f"overturned {d.get('name') or d['id']}")
            if not row.get("unchanged"):
                effects.append(f"added playbook rule {row['id']} (Ask me first)")
        info = {"by": by, "words": words, "at": _now(), "effects": effects}
        self.store.overturn(d["id"], info)
        if d.get("room"):
            self._tell_room(d, f"{by} overturned {d.get('name') or d['id']} ({d['summary']}). "
                               + ("; ".join(effects) or "Noted") + ".")
        self.home.journal("home.front.overturn", {"decision": d["id"], "by": by},
                          {"ok": True, "result": {"effects": effects}})
        return {"decision": d["id"], "name": d.get("name"), "effects": effects,
                "note": "The action itself is not undone; ask me to undo it if it should be."}

    # -- the playbook from the person's corrections ---------------------------------------
    def edit_playbook(self, op: str, section: str = "", text: str = "", rid: str = "", *,
                      words: str, by: str, source: str = "", room: str = "",
                      request: str = "") -> dict:
        """The house agent changes its playbook on the person's correction:
        tightening applies now (logged, in the digest); loosening becomes an
        ask the person answers."""
        if op == "remove":
            rule = self.playbook.get(rid)
            if rule is None:
                raise FrontError(f"no playbook rule {rid!r}")
            section, text = rule.section, rule.text
        way = direction(op, section)
        words = _one_line(words, 1000)
        if way == "tighten":
            row = self.playbook.change(op, section, text, rid, who=by, kind="house", words=words,
                                       via=f"{self.home.agent_name}, from {source or 'chat'}")
            if not row.get("unchanged"):
                self.decide("tightened", f"Playbook: {'added' if op == 'add' else 'removed'} "
                                         f"\"{_one_line(row['text'], 120)}\" "
                                         f"({TITLES[row['section']]})",
                            {"playbook": {"id": row["id"], "section": row["section"],
                                          "text": row["text"]}, "note": f'you said: "{words}"'},
                            source=source, room=room, request=request,
                            data={"subject": row["text"]})
            return {"applied": True, **row}
        verb = "Add" if op == "add" else "Drop"
        a = self.make_ask(
            "playbook", f'{verb} to my playbook: "{_one_line(text, 90)}"?',
            f"You said: \"{_one_line(words, 200)}\". That lets me "
            + ("act without asking you" if (op == "add") == (section in ACT) else "skip asking")
            + f" ({TITLES[section]}), so I need your yes first.",
            more=f"Section: {TITLES[section]}. Rule: {text}",
            change={"op": op, "section": section, "text": text, "id": rid, "words": words},
            source=source, room=room, request=request, asker=by)
        return {"applied": False, "asked": a["name"], "line": a["line"]}

    # -- disputes ----------------------------------------------------------------------
    def settle(self, question: str, positions: list | None = None, *, cap: str = "",
               worker: str = "", repo: str = "", branch: str = "", project: str = "",
               ruling: str = "", rule: str = "", room: str = "", source: str = "huddle",
               request: str = "") -> dict:
        """Rule on a disagreement between agents from the trust map or the
        playbook, or ask the person. ``ruling`` and ``rule`` are what the house
        agent proposes (the side it picks; the playbook rule id it relies on);
        the hub accepts the ruling only when the cited entry really settles it:

        * the action at stake is ``go`` under a named trust entry (ship-it),
          or the cited playbook rule is a ``rule``/``do`` rule and the trust
          map does not say ask for the action: ruled, cited, in the digest;
        * a sacred or guarded action, or a gap: a one-line ask."""
        question = _one_line(question, 300)
        if len(question) < 4:
            raise FrontError("question: what the agents disagree on")
        pos = [p for p in (positions or []) if isinstance(p, dict)][:6]
        sides = "; ".join(f"{_one_line(p.get('who'), 40)}: {_one_line(p.get('says'), 160)}"
                          for p in pos)
        r = None
        w = None
        st = self.trust()
        if cap and worker:
            with contextlib.suppress(FrontError):
                w = self.resolve_worker(worker, cap)
            r = self.ruling(cap, w) if w is not None else None
        elif (repo or project) and st is not None:
            r = st.rule(cap or "git.push", "admin" if repo else "write", (), repo=repo,
                        branch=branch, project=project)
        pb = self._rule(rule)
        cite = self.cite_of(r, pb)
        why = ""
        ruled = False
        if r is not None and r.tier == "person":
            why = "sacred"
        elif r is not None and r.tier == "ask" and not (r.unlisted and r.level == "balanced"):
            why = "guarded"
        elif r is not None and r.tier == "go" and not r.unlisted and r.entry is not None:
            ruled = bool(ruling)
        elif pb is not None and pb.section in ("rule", "do") and ruling:
            ruled = True
        else:
            why = "gap"
        if ruled:
            d = self.decide("ruled", f"{question} -> {_one_line(ruling, 160)}", cite,
                            source=source, room=room, request=request,
                            data={"positions": pos, "subject": question})
            text = (f"Ruling ({d['name']}): {_one_line(ruling, 300)} Cited: "
                    f"{self.cite_line(cite)}.")
            if room:
                self._tell_room({"room": room}, text)
            return {"ruled": True, "decision": d["name"], "text": text}
        reason = {"sacred": "it touches something sacred, so it's yours to decide",
                  "guarded": "it touches something guarded, so the map says ask you first",
                  "gap": "nothing in the trust map or my playbook settles it"}[why]
        a = self.make_ask(
            "dispute", f"{_one_line(question, 120)} Go ahead?" if ruling else
            f"{_one_line(question, 140)}?",
            f"The team disagrees: {sides or question}. I asked because {reason}."
            + (f" My pick: {_one_line(ruling, 160)}." if ruling else ""),
            more=f"Question: {question}\nPositions: {sides}\nCited: {self.cite_line(cite)}",
            cite={**cite, "note": why}, change={"question": question, "ruling": ruling},
            source=source, room=room, request=request)
        return {"ruled": False, "asked": a["name"], "line": a["line"], "why": why}

    # -- routing --------------------------------------------------------------------------
    async def handle(self, text: str, *, source: str, asker: str, principal: Any = None,
                     context: str = "", room: str = "", history: list[dict] | None = None,
                     extra_tools: list | None = None,
                     extra_handler: Callable[[str, dict], Awaitable[str]] | None = None,
                     person: str | None = None, approved: tuple | None = None) -> dict:
        """Route one request. Returns ``{answer, path, refs, request, done,
        asks, model}``; raises HomeError/LLMError when the model fails."""
        home = self.home
        text = str(text or "").strip()
        if not text:
            raise FrontError("an empty request")
        rid = self.store.add_request(source, asker, text)
        cfg = home.config()
        state: dict = {"did": [], "started": [], "asked": [], "edits": [], "tools": [],
                       "approved": approved, "onboard": [], "acted": False}
        use_memory = home.may_use_memory(principal)
        recalled = (await home.recall_for(text, person, room or None, people=person is not None)
                    if use_memory else {"text": "", "ids": []})
        tools, handler = self.toolkit(text, source=source, asker=asker, principal=principal,
                                      room=room, request=rid, state=state,
                                      extra_tools=extra_tools, extra_handler=extra_handler)

        where = ROUTER_WHERE + f" This request came from {source} (asked by {asker})."
        system = home.system_prompt(cfg, where, recalled["text"])
        system += "\n\n" + self.playbook.prompt_text() + "\n\n" + ROUTER_RULES
        with contextlib.suppress(Exception):
            system += "\n\n" + self.onboard.prompt_text(self.onboard.key(source, asker, room),
                                                         text)
        roles = self._role_line()
        if roles:
            system += "\n\n" + roles
        asks = self.store.asks("open")
        if asks:
            system += "\n\nYour open asks (waiting on the person):\n" + "\n".join(
                f"- {a.get('name') or a['id']}: {a['line']}" for a in asks[-8:])
        user = text if not context else f"{text}\n\nContext:\n{str(context).strip()}"
        msgs = [{"role": "system", "content": system}] + list(history or []) + [
            {"role": "user", "content": f"[{asker}] {user}"[:20000]}]
        res = await home.tool_turn(msgs, cfg, tools, handler, via=f"front-{source}"[:40],
                                   max_calls=int(home.settings.get("front_max_calls") or 8))
        reply, path = compose(state, res.get("content") or "")
        refs = [s["name"] for s in state["started"]] + [a["name"] for a in state["asked"]]
        self.store.finish_request(rid, path, reply, {"tools": state["tools"], "refs": refs,
                                                     "model": res.get("model")})
        home.journal("home.front", {"request": rid, "source": source, "asker": asker},
                     {"ok": True, "result": {"path": path, "refs": refs,
                                             "tools": state["tools"], "model": res.get("model")}})
        return {"answer": reply, "path": path, "refs": refs, "request": rid,
                "done": [a["summary"] for a in state["did"] if a.get("ok")],
                "asks": [a["name"] for a in state["asked"]], "model": res.get("model"),
                "latency_ms": res.get("latency_ms"), "tools_used": res.get("tools_used") or []}

    def toolkit(self, text: str, *, source: str, asker: str, principal: Any = None,
                room: str = "", request: str = "", state: dict | None = None,
                extra_tools: list | None = None,
                extra_handler: Callable[[str, dict], Awaitable[str]] | None = None
                ) -> tuple[list[dict], Callable[[str, dict], Awaitable[str]]]:
        """The front door's tools for one request and the handler that runs them
        as the house agent for ``principal`` (a request, or a side thread forked
        from the persistent session: docs/design/persistent-agents.md)."""
        home = self.home
        cfg = home.config()
        if state is None:
            state = {"did": [], "started": [], "asked": [], "edits": [], "tools": [],
                     "approved": None, "onboard": [], "acted": False}
        tools = [TOOLS[n] for n in TOOLS]
        if cfg.get("tools") and home.may_read_knowledge(principal) and home._has_knowledge():
            tools.append(KNOWLEDGE)
        tools += list(extra_tools or [])
        extra_names = {(t.get("function") or t)["name"] for t in extra_tools or []}

        async def handler(name: str, args: dict) -> str:
            state["tools"].append(name)
            try:
                if name in extra_names and extra_handler is not None:
                    return await extra_handler(name, args)
                if name == "knowledge_search":
                    return await home._run_tool(name, args, room or None)
                out = await self._tool(name, args, state, source=source, asker=asker,
                                       principal=principal, room=room, request=request,
                                       said=text)
            except (FrontError, OnboardError, PlaybookError, ValueError, PermissionError,
                    LookupError) as e:
                out = {"ok": False, "error": str(e)[:400]}
            return json.dumps(out, default=str)

        return tools, handler

    def _role_line(self) -> str:
        rp = self.plugin("role")
        if rp is None:
            return ""
        with contextlib.suppress(Exception):
            rows = [r for r in rp.store.list() if r.get("enabled", True)]
            if rows:
                return ("Roles you can bring into a huddle or give a session: " + ", ".join(
                    f"{r['id']} ({r.get('name') or r['id']})" for r in rows[:40]) + ".")
        return ""

    async def _tool(self, name: str, args: dict, state: dict, *, source: str, asker: str,
                    principal: Any, room: str, request: str, said: str) -> dict:
        where = {"source": source, "room": room, "request": request}
        if name == "do_action":
            return await self._do(args, state, asker=asker, principal=principal, **where)
        if name == "dispatch_session":
            return await self._dispatch(args, state, asker=asker, principal=principal, **where)
        if name == "open_huddle":
            return await self._huddle(args, state, asker=asker, principal=principal, **where)
        if name == "continue_huddle":
            return await self._continue(args, state, principal=principal)
        if name == "ask_person":
            a = self.make_ask("gap", args.get("line"), args.get("explain"), args.get("more"),
                              asker=asker, **where)
            state["asked"].append(a)
            return {"ok": True, "asked": a["name"], "note": "Waiting for the person; the hub "
                                                            "tells them."}
        if name == "check_trust":
            return self._check(args)
        if name == "role_config":
            return self.home.sessions.role_config(
                str(args.get("action") or "get"), args, principal=principal, said=said,
                asker=asker)
        if name in ONBOARD_TOOLS:
            return await self._onboard_tool(name, args, state, asker=asker, said=said,
                                            principal=principal, **where)
        if name == "playbook_edit":
            out = self.edit_playbook(str(args.get("op") or ""), str(args.get("section") or ""),
                                     str(args.get("text") or ""), str(args.get("id") or ""),
                                     words=str(args.get("words") or said), by=asker, **where)
            state["edits"].append(out)
            if out.get("asked"):
                state["asked"].append({"name": out["asked"], "line": out["line"]})
            return {"ok": True, **out}
        if name == "overturn":
            return {"ok": True, **self.overturn(str(args.get("decision") or ""), by=asker,
                                                words=str(args.get("words") or said),
                                                kind="house")}
        if name == "answer_ask":
            return await self._answer_in_chat(args, asker=asker, principal=principal, said=said)
        if name == "digest":
            d = self.digest(int(args.get("limit") or 10))
            return {"ok": True, "spoken": d["spoken"], "decisions": [
                {k: i[k] for k in ("name", "kind", "summary", "cite", "overturned")}
                for i in d["decisions"]], "asks": d["asks"]}
        if name == "band_status":
            return self.band_status(str(args.get("worker") or ""), str(args.get("prefix") or ""))
        if name == "waiting":
            relay = self.relay()
            if relay is None:
                return {"ok": True, "summary": "Nothing is waiting for you."}
            from ...relay import Relay, summary
            items = relay.items()
            return {"ok": True, "summary": summary(items),
                    "items": [Relay.public(i) for i in items]}
        return {"ok": False, "error": f"unknown tool {name!r}"}

    async def _onboard_tool(self, name: str, args: dict, state: dict, *, asker: str, said: str,
                            principal: Any, source: str, room: str, request: str) -> dict:
        ob = self.onboard
        key = ob.key(source, asker, room)
        where = {"asker": asker, "source": source, "room": room}
        if name == "note":
            return ob.note(key, str(args.get("text") or ""), title=str(args.get("title") or ""),
                           **where)
        if name == "offer_project":
            out = ob.offer(key, title=str(args.get("title") or ""), **where)
        elif name == "project_intake":
            out = ob.intake(key, args, said, **where)
        elif name == "project_setup":
            out = await ob.setup(key, str(args.get("words") or ""), said, request=request,
                                 principal=principal, **where)
            state["acted"] = True
            for a in out.get("asked") or ():
                state["asked"].append({"name": a["asked"], "line": a["line"]})
        elif name == "borrow":
            out = await ob.borrow(key, str(args.get("resource") or ""),
                                  str(args.get("words") or ""), said,
                                  project=str(args.get("project") or ""), request=request,
                                  state=state, principal=principal, **where)
            return out
        elif name == "restore":
            return await ob.restore(key, str(args.get("what") or ""),
                                    str(args.get("words") or ""), said, request=request,
                                    state=state, **where)
        elif name == "ledger":
            return {"ok": True, **ob.ledger(str(args.get("host") or ""))}
        else:
            return {"ok": True, **ob.record_baseline(args, str(args.get("words") or ""))}
        if out.get("say"):
            state["onboard"].append(out["say"])
        return out

    def _check(self, args: dict) -> dict:
        cap, worker = str(args.get("cap") or ""), str(args.get("worker") or "")
        st = self.trust()
        if st is None:
            return {"ok": True, "tier": "go", "cite": "no trust map on this hub"}
        if cap and worker:
            w = self.resolve_worker(worker, cap)
            r = self.ruling(cap, w)
        else:
            r = st.rule(cap or "git.push", "admin", (), repo=str(args.get("repo") or ""),
                        branch=str(args.get("branch") or ""))
        return {"ok": True, **r.public()}

    async def _do(self, args: dict, state: dict, *, asker: str, principal: Any, source: str,
                  room: str, request: str) -> dict:
        cap = str(args.get("cap") or "").strip()
        summary = _one_line(args.get("summary") or cap, 120)
        call_args = dict(args.get("args")) if isinstance(args.get("args"), dict) else {}
        w = self.resolve_worker(str(args.get("worker") or ""), cap)
        if w["on_hub"] and cap in ("task.write", "knowledge.write") \
                and not call_args.get("request_id"):
            # The hub's record writes (tasks, knowledge) need one; a retry of
            # the same request never makes two records.
            call_args["request_id"] = f"front-{request}-{len(state['did'])}"
        if not self.home.may_call(principal, cap):
            raise FrontError(f"{asker} may not run {cap} themselves, so I won't for them")
        protected = self._protected()
        r = self.ruling(cap, w)
        pb = self._rule(args.get("rule"))
        from ...trust import classify
        kind = classify(cap, w["tier"], w["tags"], w["on_hub"])
        action = {"worker": w["name"], "wid": w["wid"], "cap": cap, "args": call_args,
                  "summary": summary, "tier": w["tier"], "tags": list(w["tags"]),
                  "on_hub": w["on_hub"], "kind": kind}
        cite = self.cite_of(r, pb)
        reasons = []
        if w["name"].lower() in protected and not is_status_read(cap):
            # A family device: battery and status reads go; anything else (private
            # content included, like messages or the screen) asks the person.
            reasons.append(f"{w['name']} is a family device")
        if r is not None and r.tier != "go":
            reasons.append(r.cite)
        gone = irreversible(cap, kind, w["tags"], call_args)
        yes = state.get("approved")
        if gone and not yes:
            reasons.append(gone)
        if args.get("ask_first") and not yes:
            reasons.append("my playbook says to ask you first"
                           + (f" ({pb.id}: {pb.text})" if pb else ""))
        if reasons:
            a = self.make_ask(
                "action", f"{summary[:1].upper() + summary[1:]}? ({cap} on {w['name']})",
                f"{asker} asked for it. I need your yes because "
                + "; and ".join(_one_line(x, 220) for x in reasons[:2]) + ".",
                more=(f"Request: {_one_line(asker, 80)}. Call: {cap} on {w['name']} with "
                      f"{clip(json.dumps(call_args, default=str), 1500)}. Kind: {kind}, tier "
                      f"{w['tier']}. Cited: {self.cite_line(cite)}."),
                cite=cite, action=action, source=source, room=room, request=request, asker=asker)
            state["asked"].append(a)
            state["did"].append({"summary": summary, "ok": False, "asked": a["name"]})
            return {"ok": True, "asked": a["name"], "why": reasons,
                    "note": "Not done: it waits for the person's yes (the hub tells them)."}
        if yes:
            # The person said yes to this request (follow_up): their answer,
            # not the house agent's judgement, is what lets it run.
            by, a = yes
            cite = {**cite, "note": f"{by} said yes to {a.get('name') or a['id']}"}
            out = await self.run_action(action, approved_by=f"{by} (yes to house ask "
                                                            f"{a.get('name') or a['id']})")
        else:
            out = await self.run_action(action)
        state["did"].append({"summary": summary, **out})
        if out["ok"] and kind != "read" and not yes:     # a yes is the person's decision
            self.decide("did", f"{summary} ({cap} on {w['name']})", cite, source=source,
                        room=room, request=request, data={"subject": f"{cap} on {w['name']}",
                                                          "action": {k: action[k] for k in
                                                                     ("worker", "cap", "summary")}})
        if out["ok"] and kind == "read":
            state["did"][-1]["ok"] = False        # a read is an answer, not something done
            state["did"][-1]["asked"] = "read"
        return out

    def _protected(self) -> set[str]:
        rp = self.plugin("role")
        with contextlib.suppress(Exception):
            return {str(p).lower() for p in rp.protected()} if rp is not None else set()
        return set()

    async def _dispatch(self, args: dict, state: dict, *, asker: str, principal: Any,
                        source: str, room: str, request: str) -> dict:
        rp = self.plugin("role")
        if rp is None:
            raise FrontError("roles are off on this hub: no one to give a session to")
        if not self.home.may_call(principal, "role.session_start"):
            raise FrontError(f"{asker} may not start coding sessions themselves")
        rid = str(args.get("role") or self.home.settings.get("front_dispatch_role") or "lead")
        role = rp.role(rid.lstrip("@").lower())
        location = str(args.get("location") or "")
        branch = str(args.get("branch") or "")
        held = rp.session_trust(location, branch)
        pb = self._rule(args.get("rule"))
        if held:
            title = _one_line(args.get("title") or branch, 100)
            a = self.make_ask(
                "action", f"Start a coding session: {title}?",
                f"{asker} asked for it. {held}.",
                more=f"Role @{role['id']}, branch {branch}. Instructions: "
                     f"{clip(str(args.get('instructions') or ''), 2500)}",
                cite={"note": held}, action={
                    "worker": "rook", "cap": "role.session_start", "summary": f"start {title}",
                    "args": {"role": role["id"], "branch": branch,
                             "instructions": str(args.get("instructions") or ""),
                             "location": location}, "tier": "write", "on_hub": True,
                    "wid": self._roster()[1]},
                source=source, room=room, request=request, asker=asker)
            state["asked"].append(a)
            return {"ok": True, "asked": a["name"], "why": held}
        rec = await rp.start_session(role, location=location, branch=branch,
                                     instructions=str(args.get("instructions") or ""),
                                     title=str(args.get("title") or ""),
                                     room=room if self._chaired(room) else "",
                                     actor=f"{self.home.identity} for {asker}")
        name = rec.get("name") or rec.get("id")
        state["started"].append({"path": "dispatch", "name": name})
        st = self.trust()
        r = None
        with contextlib.suppress(Exception):
            from ..roles import sessions as rs
            loc = rs.pick_location(rs.locations(rp.code_locations()), location)
            r = st.rule("role.session_start", "write", (), worker=loc["worker"],
                        repo=rs.repo_of(loc), branch=branch) if st is not None else None
        self.decide("dispatched", f"Coding session {name}: {_one_line(args.get('title'), 120)} "
                                  f"(@{role['id']}, {branch})", self.cite_of(r, pb),
                    source=source, room=room, request=request,
                    data={"session": rec.get("id"), "subject": f"coding sessions for "
                                                               f"{_one_line(args.get('title'), 80)}"})
        return {"ok": True, "session": name, "branch": branch, "role": role["id"],
                "note": "It reports back on its own; do not poll it."}

    def _chaired(self, room: str) -> bool:
        if not room:
            return False
        hp = self.plugin("huddle")
        with contextlib.suppress(Exception):
            return bool(hp.chaired(room))
        return False

    async def _huddle(self, args: dict, state: dict, *, asker: str, principal: Any, source: str,
                      room: str, request: str) -> dict:
        hp = self.plugin("huddle")
        svc = getattr(hp, "service", None)
        if svc is None:
            raise FrontError("huddles are off on this hub")
        if not self.home.may_call(principal, "huddle.start"):
            raise FrontError(f"{asker} may not start huddles themselves")
        roles = args.get("roles")
        roles = [str(r) for r in roles] if isinstance(roles, list) and roles else None
        title = _one_line(args.get("title") or "Huddle", 120)
        brief = str(args.get("brief") or "").strip()
        if not brief:
            raise FrontError("brief: what the roles should work on")
        out = await svc.open_episode(title, roles, f"From {asker}, via {source}: {brief}",
                                     self.home.identity)
        name = out.get("name") or out["room"]
        state["started"].append({"path": "huddle", "name": name})
        pb = self._rule(args.get("rule"))
        self.decide("huddle", f"Huddle {name}: {title} ({', '.join(out.get('roles') or [])})",
                    self.cite_of(None, pb) or {"note": "work for the team"}, source=source,
                    room=out["room"], request=request, data={"subject": title})
        return {"ok": True, "huddle": name, "roles": out.get("roles"),
                "note": "The roles are on it; the huddle reports there."}

    async def _continue(self, args: dict, state: dict, *, principal: Any) -> dict:
        hp = self.plugin("huddle")
        svc = getattr(hp, "service", None)
        if svc is None:
            raise FrontError("huddles are off on this hub")
        ref = str(args.get("huddle") or "")
        out = await svc.continue_episode(ref, str(args.get("text") or ""), self.home.identity)
        name = out.get("name") or ref
        state["started"].append({"path": "huddle", "name": name})
        return {"ok": True, "huddle": name}

    async def _answer_in_chat(self, args: dict, *, asker: str, principal: Any, said: str) -> dict:
        """The person answered an ask in conversation: passed on through the
        relay rules (only a client the person enabled may carry their words),
        or straight when the person themselves is talking (web, signed in)."""
        words = " ".join(str(args.get("words") or "").split())
        if not words or words.lower() not in " ".join(said.split()).lower():
            raise FrontError("words must be the person's exact words from this message")
        decision = "yes" if str(args.get("decision")) == "yes" else "no"
        a = self.store.find_ask(str(args.get("ask") or ""))
        if a is None:
            raise FrontError(f"no open ask {args.get('ask')!r}")
        relay = self.relay()
        pid = getattr(principal, "id", "") or ""
        label = getattr(principal, "label", "") or pid
        if str(asker).startswith(("user:", "human:")):
            # A person in chat: their words, carried by the house agent itself
            # (as the chair relays them), so it must be enabled to relay.
            pid, label = self.home.identity, f"the house agent ({self.home.agent_name})"
            principal = None
        if relay is None or not relay.may_relay(pid):
            raise FrontError("this client may not pass on the person's answers yet: they answer "
                             "on the phone card or the dashboard, or turn on \"May relay my "
                             "approvals\" for it on the Tokens page")
        out = await relay.decide(a["id"], "approve" if decision == "yes" else "deny",
                                 {"who": self.person_name(), "quote": words,
                                  "session": f"house agent request via {asker}"},
                                 relayer=pid, relayer_label=label, principal=principal)
        return {"ok": True, **{k: v for k, v in out.items() if k != "recorded"}}

    # -- the dashboard's view ---------------------------------------------------------------
    def view(self) -> dict:
        d = self.digest(50)
        asks = []
        for a in self.store.asks("open"):
            asks.append({"id": a["id"], "name": a.get("name"), "kind": a["kind"],
                         "line": a["line"], "explain": a["explain"],
                         "has_more": bool(a.get("more")), "created": a["created"],
                         "cite": self.cite_line(a.get("cite") or {})})
        answered = [{"id": a["id"], "name": a.get("name"), "line": a["line"], "state": a["state"],
                     "decided_by": a.get("decided_by"), "decided": a.get("decided"),
                     "note": (a.get("result") or {}).get("note")}
                    for a in self.store.asks(None, 30) if a["state"] != "open"][:15]
        return {"asks": asks, "answered": answered, "decisions": d["decisions"],
                "spoken": d["spoken"], "playbook": self.playbook.public(),
                "requests": [{k: r[k] for k in ("id", "created", "source", "asker", "path")}
                             | {"text": _one_line(r["text"], 160),
                                "reply": _one_line(r["reply"], 200)}
                             for r in self.store.requests(15)],
                "person": self.person_name(), **self.onboard_view()}

    def onboard_view(self) -> dict:
        """New projects (threads and what setup did) and borrowed state."""
        threads = [{"id": t["id"], "stage": t["stage"], "title": t["title"], "slug": t["slug"],
                    "source": t["source"], "asker": t["asker"], "updated": t["updated"],
                    "notes": t["notes"].count("\n- ") + 1 if t["notes"] else 0,
                    "readback": (t["plan"] or {}).get("readback") or "",
                    "result": [{k: s.get(k) for k in ("step", "ok", "error", "asked", "proposal")
                                if s.get(k) is not None}
                               for s in (t["result"] or {}).get("steps") or []]}
                   for t in self.onboard.threads(10)]
        led = {}
        with contextlib.suppress(Exception):
            led = self.onboard.ledger()
        return {"onboarding": threads, "borrowed": led.get("open") or [],
                "baseline": led.get("baseline") or []}

