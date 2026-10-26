"""The chair: rounds in a chaired huddle (docs/design/huddles.md 5).

A chaired huddle is a huddle started with ``huddle.start``: a person, the
house agent (the chair) and a set of roles. Each post from a person (or a
job) starts a **round**:

1. The chair takes the turn first, with three tools: ``call_on`` (which roles
   answer, a focused question each), ``answer`` (it answers itself) and
   ``propose`` (an action card). ``@qa`` in the post skips the chair's routing
   and calls that role directly.
2. Called roles answer in parallel (``roles`` plugin turns: each in its home,
   with its tools and memory), each under ``huddle.reply_chars``, and may
   ``propose`` cards too. At most ``huddle.max_role_turns`` role turns a round.
3. A role that failed or ran out of tool calls is asked again, once by
   default (``huddle.role_retries``), with a narrower question.
4. The chair closes the round. The close is also where it decides whether
   the thread is settled: when a role still owes an answer it calls on that
   role again (at most ``huddle.max_followups`` times a round), when the
   person is needed it says what for, else it writes a short synthesis. Roles
   never call each other.

Action cards are ``{id, kind, by, summary, call: {worker, cap, args}, state}``.
A card's call is checked when it is proposed (the worker is on the band and
has the cap; task cards are normalised to ``task.write``; a session card's
task is a task id and its folder one of ``role.code_locations``), so a bad
card is refused to its proposer instead of failing on approval. A card like
one still waiting (same kind, near-identical summary or title) is not added
again: the proposer's own is revised, anyone else's is reported as the
duplicate. A person approves (the call runs under the approver's identity
and the band's policy, journaled with the card and huddle ids) or dismisses.
A card that fails says why on the card and in the room. A role with
``autonomy: act`` runs calls within its tools directly and posts the card
already done.

Every message is an ordinary chat message (the chair as ``agent:<name>``,
roles as ``role:<id>``), so every chat client reads the thread; rounds and
cards live beside it in the huddle tables.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from difflib import SequenceMatcher
from typing import Any, Callable

from ....band_mcp.secret_mask import scrub
from ....core import names
from ....core.text import clip

log = logging.getLogger("rook.hub.plugins.huddle.chair")

CARD_KINDS = ("task.create", "task.update", "job.run", "job.create", "session.send",
              "session.start", "call", "note", "ask_bake")
#: Kinds that carry a call; ``note`` and ``ask_bake`` never run anything.
NO_CALL = ("note", "ask_bake")
#: Kinds recorded as done when proposed: information for the room, never an
#: ask waiting on the person (2026-10-23: day-old role notes reached Bake's
#: phone as approval asks he could not read).
NOTED = ("note",)
#: The caps a session card may run (the lead developer fronts the sessions).
SESSION_CAPS = {"session.send": ("sessions.send", "role.session_send"),
                "session.start": ("work.stream.open", "role.session_start")}
CARD_STATES = ("proposed", "approved", "dismissed", "done", "failed")
SUMMARY_MAX = 300
ARGS_MAX = 4000
CONTEXT_MESSAGES = 30
CONTEXT_CHARS = 12000
#: A posted message (the chair's or a role's) is cut here, visibly, inside
#: the chat store's own limit (8000).
POST_CHARS = 7900
MENTION = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,31})\b", re.I)
CALL_TIMEOUT = 60.0
#: Task cards run ``task.write`` (``task.create``/``task.update`` are not caps).
TASK_CAPS = ("task.create", "task.update", "task.write")
TASK_DATA = ("title", "body", "parent", "attrs", "revision", "patch", "handoff")
#: ``work.stream.open``'s ``task``: a Rook task id or slug (the worker's own rule).
TASK_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
#: Near-identical cards (same kind): summaries or call titles this similar.
SAME_SUMMARY = 0.85
SAME_TITLE = 0.8
#: A chair close that is only this word posts nothing (one role answered, nothing open).
SETTLED = "SETTLED"
PATH = re.compile(r"(?<![\w.~-])(~?/[^\s,;:()'\"`]+)")


def _fn(name: str, description: str, props: dict, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       **({"required": required} if required else {})}}}


PROPOSE = _fn(
    "propose",
    "Propose an action card for the person to approve: kind is one of " + ", ".join(CARD_KINDS)
    + ". summary says what and why in one line, in plain words the person reads on their phone. "
      "A note is recorded at once for the room and never waits on the person; ask_bake waits "
      "for their answer. For every kind but note and ask_bake give the "
      "call to run on approval: worker (\"rook\" is the hub), cap and args; the hub checks the "
      "worker has the cap and refuses the card if not. task.create: cap task.write, args "
      "{action: \"create\", data: {title, body, parent (a project), attrs: {criteria}}}; "
      "task.update: cap task.write, args {action: \"update\", id, data: {revision, patch}}. "
      "session.send nudges a coding session: cap sessions.send on its worker, args {agent, "
      "native_id, text}. session.start starts one: cap work.stream.open, args {harness, cwd, "
      "title?, task?}: cwd is one of the code locations you were given, task is a Rook task id "
      "or slug (the session reads its instructions there), never the instructions themselves. "
      "call runs any other cap the person should approve (worker, cap, args). "
      "To change a card you proposed that is still waiting, pass revise: its id; never propose "
      "a second version.",
    {"kind": {"type": "string", "enum": list(CARD_KINDS)},
     "summary": {"type": "string"},
     "worker": {"type": "string"}, "cap": {"type": "string"}, "args": {"type": "object"},
     "revise": {"type": "string", "description": "id of your own waiting card to replace"}},
    ["kind", "summary"])
CALL_ON = _fn(
    "call_on",
    "Ask one or more roles in this huddle to answer, each with a focused question. They answer "
    "in parallel after your turn ends.",
    {"calls": {"type": "array", "items": {"type": "object", "properties": {
        "role": {"type": "string"}, "ask": {"type": "string"}}, "required": ["role", "ask"]}}},
    ["calls"])
KNOWLEDGE = _fn("knowledge_search", "Search the band's shared knowledge wiki (excerpts with slugs).",
                {"query": {"type": "string"}}, ["query"])
ANSWER = _fn("answer", "Answer the person yourself (when no role is needed).",
             {"text": {"type": "string"}}, ["text"])
WAITING = _fn("waiting", "What is waiting on the person across the band: held calls, held job "
              "calls, huddle cards and pinned questions, each with its name, who asked, what "
              "would happen, risk and age.", {})
RELAY = _fn(
    "relay",
    "Pass on the person's own decision about one waiting item: approve or deny it as they said "
    "in their newest message. quote is their exact words from that message (the hub checks); "
    "reason is theirs, for a deny; answer is their reply to a question. Never decide yourself.",
    {"item": {"type": "string", "description": "the item's name or id (from waiting)"},
     "decision": {"type": "string", "enum": ["approve", "deny"]},
     "quote": {"type": "string"}, "reason": {"type": "string"}, "answer": {"type": "string"}},
    ["item", "decision", "quote"])
SETTLE = _fn(
    "settle",
    "The roles disagree: hand it to the house agent's front door to settle. question: what they "
    "disagree on; positions: [{who, says}]; the action at stake: worker + cap, or repo + branch; "
    "ruling: the side you would pick; rule: the playbook rule id that supports it. The hub "
    "rules (citing the trust entry or playbook rule) when one settles it, else asks the person "
    "one line. Post nothing else about it.",
    {"question": {"type": "string"},
     "positions": {"type": "array", "items": {"type": "object", "properties": {
         "who": {"type": "string"}, "says": {"type": "string"}}}},
     "worker": {"type": "string"}, "cap": {"type": "string"}, "repo": {"type": "string"},
     "branch": {"type": "string"}, "ruling": {"type": "string"}, "rule": {"type": "string"}},
    ["question", "positions"])
ASK_PERSON = _fn(
    "ask_person",
    "Ask the person one thing only they can decide (not a disagreement: use settle): line, one "
    "Yes/No question under 120 characters; explain, 2-3 plain sentences. It reaches their phone "
    "and Approvals; their answer comes back here.",
    {"line": {"type": "string"}, "explain": {"type": "string"}, "more": {"type": "string"}},
    ["line", "explain"])
from .table import CALL_VOTE, PIN, TABLE_READ  # noqa: E402 - tool definitions only
TABLE_TOOLS = ("pin", "call_vote", "table_read")
TABLE_RULES = (
    " The table: pin what the roles must look at (a PR, a commit, test output, a document) with "
    "pin, at its exact version; roles read it with table_read. When the work is ready for an "
    "action (merge, ready for QA, deploy, ship), call_vote on the pinned artifact: the hub "
    "checks the trust map and the gates, and the required roles vote blind. You decide when a "
    "vote is needed; you never vote and never merge or deploy any other way.")
SETTLE_RULES = (
    "\n- When roles disagree and the thread cannot go on without picking a side, call settle "
    "(never ask the person in prose); when only the person can decide something else, call "
    "ask_person. After either, write one short line or nothing.")
WAITING_RULES = (
    "When the person asks what is waiting (for them, for approval), call waiting and answer from "
    "it tersely: the count, then each item's name and one line. Never invent items.")
RELAY_RULES = (
    " When the person's newest message decides waiting items (\"approve the first\", \"deny "
    "brave-otter-lamp, wrong box\"), call relay once per item they decided, quoting their exact "
    "words. Relay only what they said; if it is unclear which item they mean, ask them.")

CHAIR_WHERE = ("You chair a huddle: a group thread with the person and the roles listed below "
               "(each an agent with its own concerns, tools and memory).")
CHAIR_RULES = (
    "Your tools: call_on (ask roles; pick only those whose concerns fit, with a focused question "
    "each), answer (reply yourself when no role is needed, or to greet and frame), propose (an "
    "action card the person approves). Call call_on or answer at least once, then stop. Never "
    "invent what a role would say. Say you did something (asked a role, proposed a card) only "
    "after its tool call returned ok; the hub lists the cards you proposed under your message, "
    "so do not list them yourself. No ratchet: never add or tighten guardrails, holds or "
    "trust entries, and don't let the team turn a review into red tape; reduce the risk of a "
    "change instead (soft delete, narrow scope, stage, time-box, snapshot first).")
CLOSE_RULES = (
    "The roles have answered (below). Decide whether the thread is settled, then do one of:\n"
    "- A role failed, or a question is still open that a role in this huddle can answer: call "
    "call_on with that role and a narrower question{again}, and write nothing else.\n"
    "- It needs the person (a decision, an approval, something only they know): say exactly "
    "what you need from them, in a sentence or two.\n"
    "- Otherwise close: a short synthesis for the person (what matters, what is decided, what "
    "should happen next), in a few sentences. Do not repeat each answer.\n"
    "- When only one role answered and nothing is open, reply with exactly " + SETTLED
    + " (nothing is posted).\n"
    "- Roles do routine work themselves (their workspace, coding sessions on a branch that open "
    "pull requests); never propose a card for that.\n"
    "Propose a card only for something none of the roles proposed; to change a card you "
    "proposed, propose with revise set to its id. Never say a card exists or a task was created "
    "unless the propose call returned ok; the hub lists your cards under your message.")
REPORT_ASK = (
    "This is a report from a coding session that @{owner} started (newest message). Call on "
    "@{owner} with what it says, so it can check the result and say what is next. When the "
    "session produced something the thread asked other roles in this huddle to review (a pull "
    "request, a draft), also call on those roles, each with the link and what to look for. Do "
    "not answer it yourself and do not propose cards for it.")
RETRY_ASK = (
    "Your last turn on this question stopped before you answered ({error}). Answer now from "
    "what you already know, with at most a few tool calls; where you could not find something, "
    "say 'unknown' and what would find it. The question: {ask}")


class CardError(ValueError):
    pass


def _task_call(kind: str, cap: str, args: dict) -> tuple[str, dict]:
    """A task card's call as ``task.write`` (``task.create``/``task.update``
    are what the card does, not caps): flat ``{title, body, parent, ...}``
    goes under ``data``, ``action`` follows the kind."""
    a = dict(args)
    action = str(a.pop("action", "") or ("update" if cap == "task.update" or kind == "task.update"
                                          else "create"))
    data = a.pop("data", None)
    data = dict(data) if isinstance(data, dict) else {}
    for k in TASK_DATA:
        if k in a:
            data.setdefault(k, a.pop(k))
    out: dict = {"action": action, "kind": str(a.pop("kind", "task") or "task"), "data": data}
    for k in ("id", "band", "request_id"):
        if a.get(k):
            out[k] = a.pop(k)
    if a:
        raise CardError(f"a {kind} card's args: unexpected {', '.join(sorted(a))} (task.write "
                        "takes action, id, data {title, body, parent, attrs, revision, patch})")
    if action == "create" and not str(data.get("title") or "").strip():
        raise CardError("a task.create card needs args.data.title")
    if action == "create" and out["kind"] == "task" and not str(data.get("parent") or "").strip():
        raise CardError("a task.create card needs args.data.parent: the project (id or slug) "
                        "the task goes under (rook_call task.read {action: \"deck\"} lists them)")
    if action == "update" and not out.get("id"):
        raise CardError("a task.update card needs args.id (the task) and data {revision, patch}")
    for holder in (data, data.get("patch") if isinstance(data.get("patch"), dict) else None):
        attrs = holder.get("attrs") if holder is not None else None
        if isinstance(attrs, dict):
            holder["attrs"] = _list_attrs(kind, attrs)
    return "task.write", out


#: Task attrs the store keeps as lists of strings.
LIST_ATTRS = ("criteria", "tags", "workers", "dependencies")


def _list_attrs(kind: str, attrs: dict) -> dict:
    """A role's ``criteria: "one line"`` becomes ``["one line"]`` when the card
    is proposed; anything else that is not a list of strings is refused then,
    not when the person taps Yes (2026-10-23: a card failed on approval with
    "Expected a bounded list of strings")."""
    out = dict(attrs)
    for k in LIST_ATTRS:
        v = out.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            out[k] = [v.strip()] if v.strip() else []
        elif not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise CardError(f"a {kind} card's attrs.{k} is a list of strings")
    return out


def validate_card(raw: Any) -> dict:
    """``{kind, summary, call?}`` from a tool call or a cap, or CardError."""
    if not isinstance(raw, dict):
        raise CardError("a card is an object")
    kind = str(raw.get("kind") or "").strip()
    if kind not in CARD_KINDS:
        raise CardError("kind must be one of " + ", ".join(CARD_KINDS))
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise CardError("a card needs a summary")
    out: dict = {"kind": kind, "summary": scrub(clip(summary, SUMMARY_MAX))}
    call = raw.get("call") if isinstance(raw.get("call"), dict) else raw
    if kind not in NO_CALL:
        worker, cap = str(call.get("worker") or "").strip(), str(call.get("cap") or "").strip()
        if not worker or not cap:
            raise CardError(f"a {kind} card needs the call to run: worker and cap")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if cap in TASK_CAPS:
            cap, args = _task_call(kind, cap, args)
        if kind in SESSION_CAPS and cap not in SESSION_CAPS[kind]:
            raise CardError(f"a {kind} card runs {' or '.join(SESSION_CAPS[kind])}, not {cap}")
        if kind == "session.send" and cap == "sessions.send" and not (
                args.get("native_id") and args.get("text")):
            raise CardError("a session.send card needs args {agent, native_id, text}")
        if kind == "session.start" and cap == "role.session_start":
            if not (args.get("role") and args.get("branch") and args.get("instructions")):
                raise CardError("a role.session_start card needs args {role, branch, "
                                "instructions, location?}")
        elif kind == "session.start":
            if not args.get("cwd"):
                raise CardError("a session.start card needs args {harness, cwd}")
            task = str(args.get("task") or "")
            if task and not TASK_REF.fullmatch(task):
                raise CardError("a session.start card's task is a Rook task id or slug, not "
                                "instructions: propose a task.create card with the instructions "
                                "first, then start the session with its slug")
        if len(json.dumps(args, default=str)) > ARGS_MAX:
            raise CardError(f"a card's args: at most {ARGS_MAX} characters as JSON")
        out["call"] = {"worker": worker[:64], "cap": cap[:80], "args": args}
    return out


def _key(card: dict) -> str:
    """What makes two cards the same proposal (for de-duplication)."""
    call = card.get("call")
    if call:
        return json.dumps([card["kind"], call["worker"], call["cap"], call["args"]],
                          sort_keys=True, default=str)
    return card["kind"] + ":" + _norm(card["summary"])


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def _title(card: dict) -> str:
    args = (card.get("call") or {}).get("args") or {}
    data = args.get("data") if isinstance(args.get("data"), dict) else {}
    return _norm(data.get("title") or args.get("title") or "")


def near_same(a: dict, b: dict) -> bool:
    """Two cards of one kind that say the same thing: near-identical
    summaries (by characters and by words), or near-identical call titles."""
    if a["kind"] != b["kind"]:
        return False
    if _key(a) == _key(b):
        return True
    sa, sb = _norm(a["summary"]), _norm(b["summary"])
    wa, wb = set(sa.split()), set(sb.split())
    words = len(wa & wb) / max(1, len(wa | wb))
    if SequenceMatcher(None, sa, sb).ratio() >= SAME_SUMMARY and words >= 0.6:
        return True
    ta, tb = _title(a), _title(b)
    return bool(ta and tb) and SequenceMatcher(None, ta, tb).ratio() >= SAME_TITLE


def card_error(card: dict) -> str:
    """Why a failed card failed, from its result."""
    res = card.get("result") if isinstance(card.get("result"), dict) else {}
    inner = res.get("result") if isinstance(res.get("result"), dict) else {}
    for why in (res.get("denied"), res.get("error"), inner.get("error")):
        if why:
            return str(why)[:500]
    return "the call failed" if card.get("state") == "failed" else ""


def _is_person(identity: str) -> bool:
    return str(identity or "").split(":", 1)[0] in ("user", "human")


class Chair:
    """Rounds and cards for :class:`.service.HuddleService`.

    ``node()`` returns the hub node (plugins ``home`` and ``role``, the band
    client, the journal); ``settings()`` the huddle plugin's settings."""

    def __init__(self, svc: Any, node: Callable[[], Any], settings: Callable[[], dict]) -> None:
        self.svc = svc
        self._node = node
        self._settings = settings
        self._locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()      # running rounds (tests await them)

    # -- plumbing --------------------------------------------------------------
    @property
    def store(self):
        return self.svc.store

    def _plugin(self, ns: str):
        node = self._node()
        return node.plugin(ns) if node is not None else None

    def home(self):
        home = self._plugin("home")
        if home is None or not home.enabled():
            raise ValueError("the house agent is off (Manage > Home agent); it chairs huddles")
        return home

    def roles(self):
        roles = self._plugin("role")
        if roles is None:
            raise ValueError("huddles with roles need the roles plugin")
        return roles

    def setting(self, key: str, default: Any) -> Any:
        v = (self._settings() or {}).get(key)
        return default if v is None else v

    def chair_identity(self) -> str:
        home = self._plugin("home")
        return home.identity if home is not None else "agent:home"

    def journal(self, cap: str, args: dict, reply: dict, room: str, identity: str,
                audit: dict | None = None) -> None:
        j = getattr(self._node(), "journal", None)
        if j is None:
            return
        try:
            j.record(cap=cap, worker="rook", identity=identity, args=args, reply=reply,
                     thread_id=room, **({"audit": audit} if audit else {}))
        except Exception:
            log.exception("huddle: journaling %s failed", cap)

    # -- roster ----------------------------------------------------------------
    def roster(self, h: dict) -> list[dict]:
        """The huddle's roles that exist and are on, as role documents."""
        ids = json.loads(h.get("roles") or "[]") if isinstance(h.get("roles"), str) \
            else list(h.get("roles") or [])
        plugin = self._plugin("role")
        out = []
        for rid in ids:
            doc = plugin.store.get(rid) if plugin is not None else None
            if doc is not None and doc.get("enabled", True):
                out.append(doc)
        return out

    # -- posts -------------------------------------------------------------------
    def on_post(self, h: dict, seq: int, sender: str, text: str) -> str | None:
        """A post landed in a chaired huddle: start a round when a person (or
        a job) wrote it, or a guest agent that mentions the chair or a role
        (roles and the chair never start one: no loops). Returns the round
        id."""
        if h.get("roles") is None or h.get("archived"):
            return None
        if not (_is_person(sender) or str(sender).startswith("job:")):
            # A guest agent: only when it addresses the chair or a role.
            chair = self.chair_identity().split(":", 1)[-1]
            ids = {r["id"] for r in self.roster(h)} | {chair}
            if str(sender).startswith("role:") or sender == self.chair_identity() or \
                    not {m.lower() for m in MENTION.findall(text or "")} & ids:
                return None
        return self.begin(h, seq, sender, text)

    def begin(self, h: dict, seq: int, sender: str, text: str) -> str | None:
        """Start a round on post ``seq`` (no check of who wrote it: the house
        agent's own episodes start here, front.py). Returns the round id."""
        if h.get("roles") is None or h.get("archived"):
            return None
        rid = "r_" + uuid.uuid4().hex[:12]
        self.store.round_add(rid, h["room"], int(seq), sender, {})
        task = asyncio.get_running_loop().create_task(self._run(rid, h["room"], text))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return rid

    def on_report(self, h: dict, room: str, owner: str, sender: str, text: str,
                  word: str = "") -> str | None:
        """A coding session a role started reports back (DONE, BLOCKED,
        QUESTION or something unexpected): posted in the huddle, and a round
        starts where the chair calls on that role (always) and on any role the
        thread asked to review the result. Returns the round id."""
        if h.get("roles") is None or h.get("archived"):
            return None
        sent = self.svc._send(room, sender, text, [f"role:{owner}"])
        rid = "r_" + uuid.uuid4().hex[:12]
        self.store.round_add(rid, room, int(sent["seq"]), sender, {"report": word, "owner": owner})
        task = asyncio.get_running_loop().create_task(self._run(rid, room, text, owner=owner))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return rid

    async def wait(self, rid: str, timeout: float) -> dict:
        """Wait for a round to end (a job's huddle step)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.store.round(rid)
            if r is None or r["state"] in ("done", "failed"):
                return r or {}
            await asyncio.sleep(0.25)
        return self.store.round(rid) or {}

    def resume(self) -> None:
        """Rounds cut off by a restart are marked failed (nothing reruns)."""
        for r in self.store.unfinished_rounds():
            self.store.round_set(r["id"], state="failed", error="the hub restarted mid-round",
                                 ended=time.time())

    # -- a round -------------------------------------------------------------------
    async def _run(self, rid: str, room: str, text: str, owner: str = "") -> None:
        lock = self._locks.setdefault(room, asyncio.Lock())
        async with lock:                      # one round per huddle at a time
            try:
                await self._round(rid, room, text, owner)
            except Exception as e:  # noqa: BLE001 - a round failing never breaks the huddle
                log.warning("huddle %s: round %s failed: %s", room, rid, e)
                err = str(e)[:300]
                self.store.round_set(rid, state="failed", error=err, ended=time.time())
                with contextlib.suppress(Exception):
                    self.svc._send(room, self.chair_identity(),
                                   "(The chair could not finish this round: " + err + ")", quiet=True)

    def _transcript(self, room: str, upto: int = 0) -> str:
        """The room's recent messages, oldest first (``upto``: none after that
        seq, so a queued round answers its own post, not a later one)."""
        lines = []
        for m in self.svc.chat.tail(room, CONTEXT_MESSAGES + (20 if upto else 0)):
            if upto and int(m.get("seq") or 0) > upto:
                continue
            lines.append(f"[{m['sender']}] {m['text']}")
        return clip("\n".join(lines), CONTEXT_CHARS, tail=True)

    async def _round(self, rid: str, room: str, text: str, owner: str = "") -> None:
        h = self.store.get(room)
        roster = self.roster(h)
        by_id = {r["id"]: r for r in roster}
        if owner and owner not in by_id:
            # The role that started the session left the huddle (or is off):
            # it still hears its own session's report.
            with contextlib.suppress(Exception):
                doc = self.roles().role(owner)
                if doc.get("enabled", True):
                    by_id[owner] = doc
        max_turns = int(self.setting("max_role_turns", 6))
        followups = max(0, int(self.setting("max_followups", 2)))
        mentioned = [m.lower() for m in MENTION.findall(text or "")]
        direct = [r for r in dict.fromkeys(mentioned) if r in by_id] if not owner else []
        plan: dict = {"direct": bool(direct), "calls": [], "answered": False}
        if direct:
            plan["calls"] = [{"role": r, "ask": text} for r in direct[:max_turns]]
        elif owner and self._report_seen(room, owner, rid):
            # The role already spoke after this report was posted (a round
            # queued behind another one): it saw the report; skip the round.
            self.store.round_set(rid, state="done", ended=time.time(),
                                 error="skipped: the role already answered after this report")
            return
        elif owner and not roster:
            # A 1:1 huddle: nobody else to route to; the owner hears its report.
            plan["calls"] = [{"role": owner, "ask": text}]
        elif owner:
            try:
                plan.update(await self._chair_turn(rid, room, h, roster, text,
                                                   REPORT_ASK.format(owner=owner)))
            except Exception as e:  # noqa: BLE001 - the owner still hears its report
                log.warning("huddle %s: chair turn on a session report failed: %s", room, e)
            if owner not in [c["role"] for c in plan["calls"]]:
                plan["calls"] = [{"role": owner, "ask": text}] + plan["calls"]
        elif not roster and self._front_on():
            # A 1:1 huddle with the house agent (the web's chat panel): the
            # front door routes it (rook/hub/plugins/home/front.py).
            plan.update(await self._front_turn(rid, room, text))
        else:
            plan.update(await self._chair_turn(rid, room, h, roster, text))
        self.store.round_set(rid, plan=plan)
        calls = [c for c in plan["calls"] if c["role"] in by_id][:max_turns]
        if not calls:
            self.store.round_set(rid, state="done", ended=time.time())
            return
        replies: list[dict] = []
        answered: list[str] = []
        passes = 0
        routed = not plan["direct"]
        while calls:
            self.store.round_set(rid, state="roles")
            if routed:
                lines = [f"@{c['role']}: {c['ask']}" for c in calls]
                self.svc._send(room, self.chair_identity(), "\n".join(lines),
                               [f"role:{c['role']}" for c in calls], quiet=True)
            routed = True                    # follow-ups are always the chair's routing
            seen = self.svc.chat.last_seq(room)          # what these turns are shown
            transcript = self._transcript(room)
            start = len(replies)
            replies += [{"role": c["role"], "ask": c["ask"], "state": "running", "seen": seen,
                         **({"followup": passes} if passes else {})} for c in calls]
            self.store.round_set(rid, replies=replies)
            results = await asyncio.gather(*(
                self._role_with_retry(rid, room, by_id[c["role"]], c["ask"], transcript,
                                      replies, start + i) for i, c in enumerate(calls)))
            answered += [r for r in results if r]
            failed = [replies[start + i] for i, r in enumerate(results) if not r]
            # The chair closes every round it routed, and any round where a
            # role still owes an answer; a direct question answered by one role
            # is that role's answer.
            if plan["direct"] and not failed and len(answered) < 2:
                break
            self.store.round_set(rid, state="closing")
            left = followups - passes
            verdict = await self._close(rid, room, h, roster, text, answered,
                                        [r for r in replies if r["state"] == "failed"], left)
            passes += 1
            calls = [c for c in verdict.get("calls") or [] if c["role"] in by_id][:max_turns] \
                if left > 0 else []
        self.store.round_set(rid, state="done", ended=time.time())

    def _report_seen(self, room: str, owner: str, rid: str) -> bool:
        """The owner already answered in a turn that was shown this report
        (its turn started after the report was posted). A turn that was
        already running when the report arrived did not see it."""
        r = self.store.round(rid) or {}
        after = int(r.get("trigger_seq") or 0)
        if not after:
            return False
        for other in self.store.rounds(room, 10):
            if other["id"] == rid:
                continue
            for rep in other.get("replies") or []:
                if rep.get("role") == owner and rep.get("state") == "done" \
                        and int(rep.get("seen") or 0) >= after:
                    return True
        return False

    def _chair_messages(self, home: Any, cfg: dict, roster: list[dict], room: str,
                        rules: str, extra: str = "", memory: str = "") -> list[dict]:
        if roster:
            lines = ["Roles in this huddle (people write @<id>):"]
            for r in roster:
                # The one-line description routes; the full charter is the role's own.
                about = r.get("description") or r.get("charter") or ""
                lines.append(f"- @{r['id']} ({r['name']}): {about}".rstrip())
                if r.get("concerns"):
                    lines.append("  concerns: " + "; ".join(r["concerns"][:8]))
                if r.get("examples"):
                    lines.append("  good at: " + "; ".join(r["examples"][:3]))
        else:
            lines = ["No roles are in this huddle: you answer the person yourself."]
        system = home.system_prompt(cfg, CHAIR_WHERE, memory) + "\n\n" + "\n".join(lines) \
            + "\n\n" + rules
        user = "The huddle so far, oldest first:\n" + self._transcript(room)
        waiting = self.pending_text(room)
        if waiting:
            user += "\n\n" + waiting
        if extra:
            user += "\n\n" + extra
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def pending_text(self, room: str) -> str:
        """The cards still waiting in this huddle, for the chair: it revises
        its own instead of proposing another version."""
        mine = self.chair_identity()
        table = self._table_text(room)
        cards = self.store.cards(room)
        rows = [c for c in cards if c["state"] == "proposed"]
        dismissed = [c for c in cards if c["state"] == "dismissed"
                     and (c.get("result") or {}).get("dismissed")]
        lines = []
        if rows:
            lines.append("Cards waiting for the person in this huddle (revise yours with propose "
                         "revise=<id>; do not propose another version):")
            for c in rows[-12:]:
                who = "yours" if c["by"] == mine else c["by"]
                lines.append(f"- {c['id']} [{c['kind']}, {who}] {c['summary']}")
        if dismissed:
            lines.append("Cards the person dismissed, with their reason (adapt; do not propose "
                         "them again unchanged):")
            for c in dismissed[-6:]:
                lines.append(f"- {c['id']} [{c['kind']}] {c['summary']}: "
                             f"{c['result']['dismissed']}")
        if table:
            lines.append(table)
        return "\n".join(lines)

    # -- the conference table (table.py) ----------------------------------------------------
    def _table(self):
        t = getattr(self.svc, "table", None)
        return t if t is not None and self.setting("votes", True) else None

    def _table_tools(self, roster: list[dict]) -> list[dict]:
        """``pin`` and ``call_vote`` for the chair of a huddle with roles."""
        return [PIN, CALL_VOTE] if roster and self._table() is not None else []

    def _table_text(self, room: str) -> str:
        t = self._table()
        if t is None:
            return ""
        with contextlib.suppress(Exception):
            return t.summary_text(room)
        return ""

    async def _table_tool(self, name: str, args: dict, room: str, reader: str = "") -> str:
        t = self._table()
        if t is None:
            return json.dumps({"ok": False, "error": "the conference table is off (huddle.votes)"})
        from .table import GitHubError, TableError
        try:
            if name == "pin":
                out = await t.pin(room, self.chair_identity(), args)
            elif name == "call_vote":
                out = await t.call(room, self.chair_identity(), args)
            else:
                out = await t.read(room, str(args.get("artifact") or ""),
                                   str(args.get("what") or "summary"), str(args.get("path") or ""),
                                   int(args.get("offset") or 0), reader)
                return clip(json.dumps({"ok": True, **out}, default=str), 14000)
        except (TableError, GitHubError, ValueError) as e:
            return json.dumps({"ok": False, "error": str(e)[:400]})
        return json.dumps({"ok": True, **out}, default=str)

    def _call_on(self, state: dict, ids: set, max_turns: int, args: dict) -> str:
        ok, bad = [], []
        for c in args.get("calls") or []:
            if not isinstance(c, dict):
                continue
            r = str(c.get("role") or "").lstrip("@").strip().lower()
            ask = str(c.get("ask") or "").strip()
            if r in ids and ask and r not in [x["role"] for x in state["calls"]]:
                if len(state["calls"]) < max_turns:
                    state["calls"].append({"role": r, "ask": ask[:2000]})
                    ok.append(r)
            else:
                bad.append(r or "?")
        out: dict = {"ok": bool(ok), "called": ok}
        if bad:
            out["not_in_huddle"] = bad
            out["roles"] = sorted(ids)
        return json.dumps(out)

    async def _chair_turn(self, rid: str, room: str, h: dict, roster: list[dict],
                          text: str, extra: str = "") -> dict:
        home = self.home()
        cfg = home.config()
        cfg["tools"] = False
        state: dict = {"calls": [], "answer": None, "cards": []}
        ids = {r["id"] for r in roster}
        max_turns = int(self.setting("max_role_turns", 6))

        async def handler(name: str, args: dict) -> str:
            if name == "call_on":
                return self._call_on(state, ids, max_turns, args)
            if name == "answer":
                state["answer"] = str(args.get("text") or "").strip()
                return json.dumps({"ok": True})
            if name == "propose":
                out = await self._propose(rid, room, self.chair_identity(), args)
                state["cards"].append((args, out))
                return json.dumps(out)
            if name == "knowledge_search":
                return await home._run_tool(name, args, room)
            if name in ("waiting", "relay") and relay is not None:
                return await self._relay_tool(relay, name, args, rid, room, asker, text)
            if name in TABLE_TOOLS:
                return await self._table_tool(name, args, room)
            return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

        tools = ([CALL_ON] if roster else []) + [ANSWER, PROPOSE] + self._table_tools(roster)
        asker = (self.store.round(rid) or {}).get("asker")
        extra_rules = ""
        relay = self._relay()
        if relay is not None:
            waiting_tools, extra_rules = self._waiting_tools(relay, asker)
            tools += waiting_tools
        # The chair is the house agent: what it remembers about the asker and
        # the shared facts, and (with home.tools) the knowledge search, as in
        # its chat replies.
        memory = ""
        with contextlib.suppress(Exception):
            if home.may_use_memory(home.chat_principal(asker)):
                memory = (await home.recall_for(text, home.person_of(asker), room))["text"]
        search = bool(home.settings.get("tools")) and home._has_knowledge() and \
            home.may_read_knowledge(home.chat_principal(asker))
        if search:
            tools.append(KNOWLEDGE)
        msgs = self._chair_messages(home, cfg, roster, room,
                                    CHAIR_RULES + (" " + extra_rules if extra_rules else "")
                                    + (TABLE_RULES if self._table_tools(roster) else ""),
                                    extra or "Respond to the newest message.", memory)
        res = await home.tool_turn(msgs, cfg, tools, handler, via="huddle-chair", max_calls=6)
        self.journal("huddle.chair", {"round": rid}, {"ok": True, "result": {
            "model": res.get("model"), "called": [c["role"] for c in state["calls"]],
            "tools": res.get("tools_used")}}, room, home.identity, home.audit())
        answer = state["answer"] or ("" if state["calls"] else (res.get("content") or "").strip())
        footer = self._cards_footer(state["cards"])
        if answer or footer:
            body = "\n\n".join(p for p in (clip(scrub(answer), POST_CHARS), footer) if p)
            sent = self.svc._send(room, home.identity, body)
            self._attach(rid, home.identity, sent["seq"])
        return {"calls": state["calls"], "answered": bool(answer), "model": res.get("model")}

    def _front_on(self) -> bool:
        home = self.home()
        with contextlib.suppress(Exception):
            return bool(home is not None and home.front_door())
        return False

    async def _front_turn(self, rid: str, room: str, text: str) -> dict:
        home = self.home()
        rnd = self.store.round(rid) or {}
        asker = str(rnd.get("asker") or "")
        if asker == home.identity:
            return {"calls": [], "answered": False}     # its own post: no loop
        relay = self._relay()
        extra: list[dict] = []
        if relay is not None:
            extra = [t for t in self._waiting_tools(relay, asker)[0]
                     if t["function"]["name"] == "relay"]

        async def extra_handler(name: str, args: dict) -> str:
            return await self._relay_tool(relay, name, args, rid, room, asker, text)

        out = await home.front.handle(
            text, source="web", asker=asker or "user:operator",
            principal=home.chat_principal(asker), room=room,
            context="The conversation so far, oldest first (answer only the newest message):\n"
            + self._transcript(room, int(rnd.get("trigger_seq") or 0)),
            extra_tools=extra, extra_handler=extra_handler, person=home.person_of(asker))
        sent = self.svc._send(room, home.identity, clip(scrub(out["answer"]), POST_CHARS))
        self._attach(rid, home.identity, sent["seq"])
        self.journal("huddle.chair", {"round": rid}, {"ok": True, "result": {
            "model": out.get("model"), "path": out["path"], "refs": out["refs"]}},
            room, home.identity, home.audit())
        return {"calls": [], "answered": True, "model": out.get("model"), "path": out["path"]}

    async def _front_tool(self, name: str, args: dict, room: str, rid: str) -> str:
        """``settle`` and ``ask_person`` in a chaired huddle: the house agent's
        front door rules or asks (front.py)."""
        home = self.home()
        front = getattr(home, "front", None)
        if front is None:
            return json.dumps({"ok": False, "error": "the front door is off"})
        try:
            if name == "settle":
                out = front.settle(str(args.get("question") or ""), args.get("positions") or [],
                                   cap=str(args.get("cap") or ""),
                                   worker=str(args.get("worker") or ""),
                                   repo=str(args.get("repo") or ""),
                                   branch=str(args.get("branch") or ""),
                                   ruling=str(args.get("ruling") or ""),
                                   rule=str(args.get("rule") or ""), room=room,
                                   source="huddle", request=rid)
            else:
                a = front.make_ask("gap", args.get("line"), args.get("explain"),
                                   args.get("more") or "", source="huddle", room=room,
                                   request=rid)
                out = {"asked": a["name"], "line": a["line"]}
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)[:300]})
        return json.dumps({"ok": True, **out}, default=str)

    # -- what is waiting, and relaying the person's decisions (rook/hub/relay.py) ----
    def _relay(self):
        ap = self._plugin("approvals")
        return getattr(ap, "relay", None)

    def _waiting_tools(self, relay: Any, asker: str | None) -> tuple[list[dict], str]:
        """``waiting`` when anything is waiting; ``relay`` too when the house
        agent may relay and a person started this round."""
        if not _is_person(asker):
            return [], ""                 # the list is the person's, not a guest's
        try:
            if not relay.items():
                return [], ""
        except Exception:  # noqa: BLE001 - the list failing never sinks a round
            log.exception("huddle: listing what is waiting failed")
            return [], ""
        if relay.advanced() and relay.may_relay(self.chair_identity()):
            return [WAITING, RELAY], WAITING_RULES + RELAY_RULES
        return [WAITING], WAITING_RULES

    async def _relay_tool(self, relay: Any, name: str, args: dict, rid: str, room: str,
                          asker: str | None, said: str) -> str:
        from ...relay import Relay, summary
        if name == "waiting":
            items = relay.items()
            return json.dumps({"ok": True, "count": len(items), "summary": summary(items),
                               "items": [Relay.public(i) for i in items]})
        if not _is_person(asker):
            return json.dumps({"ok": False, "error": "only a person's own words are relayed"})
        if not relay.advanced():
            return json.dumps({"ok": False, "error": "the chair relays only with advanced "
                                                     "security on; the person answers on the card"})
        quote = " ".join(str(args.get("quote") or "").split())
        if not quote or quote.lower() not in " ".join(str(said or "").split()).lower():
            return json.dumps({"ok": False, "error": "quote must be the person's exact words from "
                                                     "their newest message"})
        from ...relay import voice_verdict
        decision = str(args.get("decision") or "")
        if voice_verdict(quote) != decision:
            return json.dumps({"ok": False, "error": f"the quoted words must clearly say "
                                                     f"{decision} (yes/approve/go ahead, or "
                                                     "no/deny); otherwise ask the person"})
        who = str(asker).split(":", 1)[-1]
        try:
            out = await relay.decide(
                str(args.get("item") or ""), decision,
                {"who": who, "quote": quote, "session": f"huddle {room} round {rid}"},
                relayer=self.chair_identity(), relayer_label="the house agent (chair)",
                reason=str(args.get("reason") or ""), answer=str(args.get("answer") or ""))
        except (ValueError, PermissionError, KeyError) as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True, **out}, default=str)

    def _cards_footer(self, proposed: list[tuple[dict, dict]]) -> str:
        """What the chair's propose calls actually did, from their results:
        the card ids it made or revised, and the ones the hub refused, so its
        message never claims a card that does not exist."""
        lines = []
        for args, out in proposed:
            what = str(args.get("summary") or "").strip()[:120]
            if out.get("ok") and out.get("card"):
                c = self.store.card(out["card"]) or {}
                how = ("revised, waiting for your approval" if out.get("revised") else
                       "already waiting" if out.get("duplicate") else
                       {"proposed": "waiting for your approval", "done": "done",
                        "failed": "failed"}.get(c.get("state") or out.get("state"), "proposed"))
                kind = c.get("kind") or args.get("kind")
                if kind in NOTED:
                    how = "noted"
                lines.append(f"- {_card_label({'id': out['card'], **c}, f'{kind}, {how}')}: "
                             f"{c.get('summary') or what}")
            else:
                lines.append(f"- Not proposed ({args.get('kind') or 'card'}: {what}): "
                             f"{out.get('error') or 'refused'}")
        return ("Cards:\n" + "\n".join(lines)) if lines else ""

    async def _role_with_retry(self, rid: str, room: str, role: dict, ask: str, transcript: str,
                               replies: list[dict], i: int) -> str:
        """A role's answer, asked again with a narrower question (up to
        ``huddle.role_retries`` times) when its turn failed or ran out of tool
        calls."""
        retries = max(0, int(self.setting("role_retries", 1)))
        attempt, question = 0, ask
        while True:
            last = attempt >= retries
            out = await self._role_turn(rid, room, role, question, transcript, replies, i, last)
            if out or last:
                return out
            attempt += 1
            replies[i].update(state="running", attempts=attempt + 1)
            self.store.round_set(rid, replies=replies)
            question = RETRY_ASK.format(error=replies[i].get("error") or "it failed", ask=ask)

    async def _role_turn(self, rid: str, room: str, role: dict, ask: str, transcript: str,
                         replies: list[dict], i: int, last: bool = True) -> str:
        roles = self.roles()
        chars = int(self.setting("reply_chars", 1200))
        guide = (f"You are answering in a huddle the house agent chairs. Aim for under "
                 f"{chars} characters. Do routine work yourself (your workspace; a coding session "
                 "on a branch when you may start one). For anything that needs the person, call "
                 "propose with the exact call (an action card); do not describe it at length, "
                 "and never say a card exists unless propose (or the tool that made it) "
                 "returned ok.")
        ident = f"role:{role['id']}"
        table = self._table_text(room)
        tools = [PROPOSE] + ([TABLE_READ] if table else [])
        if table:
            guide += ("\n\n" + table + "\nRead pinned artifacts with table_read: it reads the "
                      "pinned version through the hub, not your own checkout.")

        async def extra(role_doc: dict, name: str, args: dict) -> str:
            if name == "table_read":
                return await self._table_tool(name, args, room, ident)
            return json.dumps(await self._propose(rid, room, ident, args, role=role_doc))

        async def approval(card: dict) -> dict:
            # A call above the role's tier (the hub's rule, not the role's):
            # it becomes a card the person approves.
            return await self._propose(rid, room, ident, card)

        try:
            res = await roles.turn(role, ask, "The huddle so far:\n" + transcript,
                                   self.chair_identity(), extra_tools=tools,
                                   extra_handler=extra, guide=guide, via=f"huddle-{role['id']}",
                                   preread=True, room=room, approval=approval)
            # Posted whole: reply_chars is what the role aims for, not a cut.
            # Only past a chat post's size is it cut, and then visibly.
            text = clip((res.get("answer") or "").strip() or "(no answer)", POST_CHARS)
            sent = self.svc._send(room, ident, scrub(text))
            replies[i].update(state="done", seq=sent["seq"], model=res.get("model"))
            replies[i].pop("error", None)
            self._attach(rid, ident, sent["seq"])
            out = f"[{role['name']}] {text}"
        except Exception as e:  # noqa: BLE001 - one role failing never sinks the round
            err = str(e)[:300]
            replies[i].update(state="failed", error=err)
            with contextlib.suppress(Exception):
                if last:
                    self.svc._send(room, ident, f"({role['name']} could not answer: {err})",
                                   quiet=True)
                else:
                    self.svc._send(room, ident, f"({role['name']} stopped: {err}. The chair asks "
                                                "again with a narrower question.)", quiet=True)
            out = ""
        self.store.round_set(rid, replies=replies)
        return out

    async def _close(self, rid: str, room: str, h: dict, roster: list[dict], text: str,
                     answered: list[str], failed: list[dict] | None = None,
                     followups: int = 0) -> dict:
        """The chair's close: a synthesis, a question for the person, or (with
        ``followups`` left) roles to call on again. Returns ``{calls}``."""
        home = self.home()
        cfg = home.config()
        cfg["tools"] = False
        state: dict = {"calls": [], "cards": []}
        ids = {r["id"] for r in roster}
        max_turns = int(self.setting("max_role_turns", 6))

        async def handler(name: str, args: dict) -> str:
            if name == "propose":
                out = await self._propose(rid, room, self.chair_identity(), args)
                state["cards"].append((args, out))
                return json.dumps(out)
            if name == "call_on" and followups > 0:
                return self._call_on(state, ids, max_turns, args)
            if name in ("settle", "ask_person") and front_on:
                out = await self._front_tool(name, args, room, rid)
                state.setdefault("front", []).append(json.loads(out))
                return out
            if name in TABLE_TOOLS:
                return await self._table_tool(name, args, room)
            return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

        cards = [c["summary"] for c in self.store.cards(room, rid)]
        extra = "The roles' answers this round:\n" + ("\n\n".join(answered) or "(none)")
        if failed:
            extra += "\n\nRoles that could not answer (asked again already): " + "; ".join(
                f"@{r['role']}: {r.get('error') or 'failed'}" for r in failed)
        if cards:
            extra += "\n\nCards proposed this round:\n" + "\n".join(f"- {c}" for c in cards)
        again = (f" (you may do this {followups} more time{'s' if followups != 1 else ''} this "
                 "round)") if followups > 0 else " (no more this round: say what is still open)"
        rules = CLOSE_RULES.replace("{again}", again) \
            + (TABLE_RULES if self._table_tools(roster) else "")
        front_on = self._front_on()
        if front_on:
            rules += SETTLE_RULES
        msgs = self._chair_messages(home, cfg, roster, room, rules, extra)
        tools = [PROPOSE] + ([CALL_ON] if followups > 0 and roster else []) \
            + ([SETTLE, ASK_PERSON] if front_on else []) + self._table_tools(roster)
        res = await home.tool_turn(msgs, cfg, tools, handler, via="huddle-close", max_calls=4)
        body = (res.get("content") or "").strip()
        if state["calls"] or body.strip(" .").upper() == SETTLED:
            body = ""                        # calling on roles again / nothing to add
        footer = self._cards_footer(state["cards"])
        if body or footer:
            post = "\n\n".join(p for p in (clip(scrub(body), POST_CHARS), footer) if p)
            sent = self.svc._send(room, home.identity, post)
            self.store.round_set(rid, close_seq=sent["seq"])
            self._attach(rid, home.identity, sent["seq"])
        return {"calls": state["calls"]}

    # -- cards -------------------------------------------------------------------------
    def _attach(self, rid: str, by: str, seq: int) -> None:
        """Cards ``by`` proposed this round before its message was posted
        belong to that message."""
        for c in self.store.cards(self.store.round(rid)["room"], rid):
            if c["by"] == by and not c["seq"]:
                self.store.card_set(c["id"], seq=seq)

    def _roster_now(self) -> tuple[dict, str | None]:
        node = self._node()
        roster: dict = {}
        with contextlib.suppress(Exception):
            roster.update(dict(getattr(getattr(node, "client", None), "workers", {}) or {}))
        hub_id = getattr(node, "worker_id", None)
        with contextlib.suppress(Exception):
            roster[hub_id] = node.entry()
        return roster, hub_id

    def check_call(self, card: dict) -> str:
        """Why a card's call cannot run as proposed ("" when it can): its
        worker is on the band and has the cap, and a session card starts in
        one of the hub's code locations (when any are set)."""
        call = card.get("call")
        if not call:
            return ""
        roster, hub_id = self._roster_now()
        if not roster:
            return ""                         # no band view (tests, a hub starting): approval checks
        from ..jobs.kinds import resolve_worker
        try:
            found = resolve_worker(roster, call["worker"], call["cap"], hub_id)
        except ValueError as e:
            return str(e)
        if not found:
            return f"no worker {call['worker']!r} is on the band now"
        entry = roster.get(found[0]) or {}
        caps = entry.get("caps")
        if caps is not None and call["cap"] not in caps:
            near = sorted(c for c in caps if c.split(".")[0] == call["cap"].split(".")[0])[:8]
            return (f"{found[1]} has no cap {call['cap']}"
                    + (f" (it has {', '.join(near)})" if near else ""))
        if card["kind"] == "session.start" and call["cap"] == "work.stream.open":
            where = self._code_paths()
            cwd = str(call["args"].get("cwd") or "").rstrip("/")
            if where and not any(cwd == p or cwd.startswith(p + "/") for p in where):
                return ("a session starts in one of the code locations: " + ", ".join(where)
                        + " (Settings, role.code_locations)")
        return ""

    def _code_paths(self) -> list[str]:
        roles = self._plugin("role")
        lines = []
        with contextlib.suppress(Exception):
            lines = roles.code_locations() if roles is not None else []
        paths = []
        for line in lines:
            paths += [p.rstrip("/") for p in PATH.findall(line) if len(p) > 1]
        return list(dict.fromkeys(paths))

    async def _propose(self, rid: str | None, room: str, by: str, raw: dict,
                       role: dict | None = None, seq: int | None = None) -> dict:
        try:
            card = validate_card(raw)
        except CardError as e:
            return {"ok": False, "error": str(e)}
        why = self.check_call(card)
        if why:
            return {"ok": False, "error": f"not proposed: {why}"}
        if (self.store.get(room) or {}).get("archived"):
            # Its cards do not reach the person: refuse, never wait unseen.
            return {"ok": False, "error": "not proposed: that huddle is archived"}
        waiting = [c for c in self.store.cards(room) if c["state"] == "proposed"]
        target = str(raw.get("revise") or "").strip()
        if target:
            old = next((c for c in waiting if target in (c["id"], c.get("name"))
                        or c.get("name") == names.normalize(target)), None)
            if old is None:
                return {"ok": False, "error": f"no waiting card {target} in this huddle (only "
                                              "proposed cards can be revised)"}
            if old["by"] != by:
                return {"ok": False, "error": f"{target} is {old['by']}'s card; only its proposer "
                                              "revises it"}
            return self._revise(old, card)
        same = _key(card)
        for c in self.store.cards(room, rid) if rid else []:
            if _key(c) == same:
                return {"ok": True, "card": c["id"], "duplicate": True, "state": c["state"]}
        for c in waiting:
            if near_same(c, card):
                if c["by"] == by:
                    return self._revise(c, card)
                return {"ok": True, "card": c["id"], "duplicate": True, "state": c["state"],
                        "note": f"{c['by']} already proposed this; it is waiting for the person"}
        card.update(id="c_" + uuid.uuid4().hex[:12], room=room, round=rid, seq=seq, by=by,
                    state="proposed")
        if card["kind"] in NOTED:
            card.update(state="done", result={"ok": True, "noted": True})
        if role is not None and card.get("call") and self._runs_now(role, card):
            # A card for something the role may do itself (a read in its
            # scope): the hub runs it at once and keeps the card as done.
            roles = self.roles()
            call = card["call"]
            reply = await roles.role_call(role, call["worker"], call["cap"],
                                          self._call_args(card))
            card["state"] = "done" if reply.get("ok", True) and "error" not in reply else "failed"
            card["result"] = _short(reply)
        self.store.card_add(card)
        if card["state"] == "failed":
            self._report_failure(card)
        return {"ok": True, "card": card["id"], "name": card.get("name"), "state": card["state"]}

    def _runs_now(self, role: dict, card: dict) -> bool:
        """The hub's tier (rook/hub/plugins/roles/tiers.py) lets ``role`` run
        this card's call without a person: a role with routine (or act)
        autonomy proposing a read in its scope. A role that only proposes
        always waits for the person; unknown caps are approval."""
        call = card["call"]
        roles = self.roles()
        from ..roles import tiers
        if not tiers.is_routine(role):
            return False                     # a role that only proposes: its cards wait
        roster, hub_id = self._roster_now()
        entry = None
        with contextlib.suppress(Exception):
            from ..jobs.kinds import resolve_worker
            found = resolve_worker(roster, call["worker"], call["cap"], hub_id)
            entry = roster.get(found[0]) if found else None
        with contextlib.suppress(Exception):
            return roles.runs_now(role, call["cap"], entry, call["worker"])
        return False

    def _revise(self, old: dict, card: dict) -> dict:
        """The proposer's newer version of a card still waiting: one card,
        updated, not a second one."""
        self.store.card_set(old["id"], expect=("proposed",), summary=card["summary"],
                            call=card.get("call"))
        return {"ok": True, "card": old["id"], "revised": True, "state": "proposed"}

    @staticmethod
    def _call_args(card: dict) -> dict:
        """The card's args as run: a task write gets a request id made from
        the card, so running it twice never makes two tasks."""
        call = card["call"]
        args = dict(call["args"])
        if call["cap"] == "task.write" and not args.get("request_id"):
            args["request_id"] = f"huddle-card:{card['id']}"
        return args

    def _report_failure(self, card: dict) -> None:
        """A failed card says why: on the card (``result``/``error``) and in
        the room, under the chair's name."""
        why = card_error(card) or "the call failed"
        call = card.get("call") or {}
        with contextlib.suppress(Exception):
            self.svc._send(card["room"], self.chair_identity(),
                           f"{_card_label(card, card['kind'] + ': ' + card['summary'][:120])} failed: "
                           f"{why}" + (f" [{call.get('cap')} on {call.get('worker')}]"
                                       if call else ""))

    async def decide(self, cid: str, action: str, by: str, principal: Any = None,
                     reason: str = "", relay: dict | None = None) -> dict:
        """A person approves or dismisses a card. Approving runs its call as
        ``by`` (``principal`` for the policy), journaled with the card and
        huddle ids. ``relay``: an agent the person enabled relays their
        decision (rook/hub/relay.py checked it); the card records who said so.
        A dismissal's ``reason`` goes back to the proposer in the room."""
        card = self.store.card(cid)
        if card is None:
            raise ValueError("no such card")
        if relay is None and not _is_person(by):
            raise PermissionError("only a person approves or dismisses action cards")
        reason = str(reason or (relay or {}).get("reason") or "").strip()[:500]
        decided_by = relay["label"] if relay else by
        if action == "dismiss":
            if not self.store.card_set(cid, expect=("proposed",), state="dismissed",
                                       decided_by=decided_by, decided=time.time(),
                                       **({"result": {"dismissed": reason}} if reason else {})):
                raise ValueError(f"that card is already {card['state']}")
            self._report_dismissal(card, decided_by, reason, relay)
            return {"card": cid, "state": "dismissed", **({"reason": reason} if reason else {})}
        if action != "approve":
            raise ValueError("action: approve or dismiss")
        if not self.store.card_set(cid, expect=("proposed",), state="approved",
                                   decided_by=decided_by, decided=time.time()):
            raise ValueError(f"that card is already {card['state']}")
        if relay is not None:
            with contextlib.suppress(Exception):
                self.svc._send(card["room"], self.chair_identity(),
                               f"{_card_label(card, card['kind'])} {relay['text']}",
                               [card["by"]] if card.get("by") else [], quiet=True)
        if not card.get("call"):              # a note or a question: approving acknowledges it
            self.store.card_set(cid, state="done", result={"ok": True})
            return {"card": cid, "state": "done"}
        reply = await self._run_call(card, by, principal)
        inner = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        ok = bool(reply.get("ok", True)) and not reply.get("error") and not reply.get("denied") \
            and inner.get("ok", True) is not False
        state = "done" if ok else "failed"
        self.store.card_set(cid, state=state, result=_short(reply))
        out = {"card": cid, "state": state, "result": _short(reply)}
        if not ok:
            failed = self.store.card(cid) or {**card, "state": state, "result": _short(reply)}
            out["error"] = card_error(failed)
            self._report_failure(failed)
        return out

    def _report_dismissal(self, card: dict, by: str, reason: str, relay: dict | None) -> None:
        """A dismissed card tells its proposer, with the person's reason, so
        it can adapt instead of proposing it again."""
        if not reason and relay is None:
            return
        who = relay["text"] if relay else f"dismissed by {by}"
        line = f"{_card_label(card, card['kind'] + ': ' + card['summary'][:120])} {who}"
        if reason:
            line += f". Reason: {reason}"
        with contextlib.suppress(Exception):
            self.svc._send(card["room"], self.chair_identity(), line,
                           [card["by"]] if card.get("by") else [], quiet=True)

    async def _run_call(self, card: dict, by: str, principal: Any) -> dict:
        node = self._node()
        call = card["call"]
        client = getattr(node, "client", None)
        roster: dict = {}
        with contextlib.suppress(Exception):
            roster.update(dict(getattr(client, "workers", {}) or {}))
        hub_id = getattr(node, "worker_id", None)
        with contextlib.suppress(Exception):
            roster[hub_id] = node.entry()
        from ..jobs.kinds import resolve_worker
        try:
            found = resolve_worker(roster, call["worker"], call["cap"], hub_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not found:
            return {"ok": False, "error": f"no live worker {call['worker']!r}"}
        wid, name = found
        args = {"card": card["id"], "huddle": card["room"], "worker": name, "cap": call["cap"]}
        # A hold this call meets says which huddle it is for, and the card's summary as why.
        from ...approvals import hold_context
        title = ""
        with contextlib.suppress(Exception):
            title = str((self.store.get(card["room"]) or {}).get("title") or "")
        ctok = hold_context.set({"huddle": title or card["room"], "why": card.get("summary") or ""})
        try:
            if client is None:
                reply = await asyncio.wait_for(node.dispatch(call["cap"], self._call_args(card), by),
                                               CALL_TIMEOUT)
            else:
                reply = await client.call(cap=call["cap"], args=self._call_args(card), target=wid,
                                          timeout=CALL_TIMEOUT, identity=by, principal=principal)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": f"no reply within {CALL_TIMEOUT:.0f}s"}
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            hold_context.reset(ctok)
        reply = scrub(reply) if isinstance(reply, dict) else {"ok": True, "result": reply}
        self.journal("huddle.card", args, {"ok": reply.get("ok", True), "result": _short(reply)},
                     card["room"], by)
        return reply


def _card_label(card: dict, detail: str) -> str:
    """``Card brave-otter-lamp (c_1a2b…, detail)``: the card's speakable name
    first, its id beside it (``Card c_1a2b… (detail)`` when unnamed)."""
    if card.get("name"):
        return f"Card {card['name']} ({card['id']}, {detail})"
    return f"Card {card['id']} ({detail})"


def _short(reply: dict) -> dict:
    """A reply small enough to keep on the card (its error kept whole)."""
    text = json.dumps(reply, default=str)
    if len(text) <= 2000:
        return reply
    out = {"ok": reply.get("ok", True), "truncated": clip(text, 2000)}
    for k in ("error", "denied"):
        if reply.get(k):
            out[k] = str(reply[k])[:500]
    return out
