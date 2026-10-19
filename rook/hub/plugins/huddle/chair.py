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
3. When two or more roles answered, the chair closes the round with a short
   synthesis. Rounds end there: roles never call each other.

Action cards are ``{id, kind, by, summary, call: {worker, cap, args}, state}``.
A person approves (the call runs under the approver's identity and the
band's policy, journaled with the card and huddle ids) or dismisses. A role
with ``autonomy: act`` runs calls within its tools directly and posts the
card already done.

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
from typing import Any, Callable

from ....band_mcp.secret_mask import scrub

log = logging.getLogger("rook.hub.plugins.huddle.chair")

CARD_KINDS = ("task.create", "task.update", "job.run", "job.create", "session.send",
              "session.start", "note", "ask_bake")
#: Kinds that carry a call; ``note`` and ``ask_bake`` never run anything.
NO_CALL = ("note", "ask_bake")
#: The caps a session card may run (the lead developer fronts the sessions).
SESSION_CAPS = {"session.send": ("sessions.send",), "session.start": ("work.stream.open",)}
CARD_STATES = ("proposed", "approved", "dismissed", "done", "failed")
SUMMARY_MAX = 300
ARGS_MAX = 4000
CONTEXT_MESSAGES = 30
CONTEXT_CHARS = 12000
MENTION = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,31})\b", re.I)
CALL_TIMEOUT = 60.0


def _fn(name: str, description: str, props: dict, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       **({"required": required} if required else {})}}}


PROPOSE = _fn(
    "propose",
    "Propose an action card for the person to approve: kind is one of " + ", ".join(CARD_KINDS)
    + ". summary says what and why in one line. For every kind but note and ask_bake give the "
      "call to run on approval: worker (\"rook\" is the hub), cap and args. session.send nudges a "
      "coding session: cap sessions.send on its worker, args {agent, native_id, text}. "
      "session.start starts one: cap work.stream.open, args {harness, cwd, title?, task?}.",
    {"kind": {"type": "string", "enum": list(CARD_KINDS)},
     "summary": {"type": "string"},
     "worker": {"type": "string"}, "cap": {"type": "string"}, "args": {"type": "object"}},
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

CHAIR_WHERE = ("You chair a huddle: a group thread with the person and the roles listed below "
               "(each an agent with its own concerns, tools and memory).")
CHAIR_RULES = (
    "Your tools: call_on (ask roles; pick only those whose concerns fit, with a focused question "
    "each), answer (reply yourself when no role is needed, or to greet and frame), propose (an "
    "action card the person approves). Call call_on or answer at least once, then stop. Never "
    "invent what a role would say.")
CLOSE_RULES = (
    "The roles have answered (below). Close the round: a short synthesis for the person (what "
    "matters, what is decided, what should happen next), in a few sentences. Do not repeat each "
    "answer. Propose a card only for something none of the roles proposed. Reply with the "
    "synthesis text only.")


class CardError(ValueError):
    pass


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
    out: dict = {"kind": kind, "summary": scrub(summary[:SUMMARY_MAX])}
    call = raw.get("call") if isinstance(raw.get("call"), dict) else raw
    if kind not in NO_CALL:
        worker, cap = str(call.get("worker") or "").strip(), str(call.get("cap") or "").strip()
        if not worker or not cap:
            raise CardError(f"a {kind} card needs the call to run: worker and cap")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if kind in SESSION_CAPS and cap not in SESSION_CAPS[kind]:
            raise CardError(f"a {kind} card runs {' or '.join(SESSION_CAPS[kind])}, not {cap}")
        if kind == "session.send" and not (args.get("native_id") and args.get("text")):
            raise CardError("a session.send card needs args {agent, native_id, text}")
        if kind == "session.start" and not args.get("cwd"):
            raise CardError("a session.start card needs args {harness, cwd}")
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
    return card["kind"] + ":" + re.sub(r"\W+", " ", card["summary"].lower()).strip()


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
        rid = "r_" + uuid.uuid4().hex[:12]
        self.store.round_add(rid, h["room"], int(seq), sender, {})
        task = asyncio.get_running_loop().create_task(self._run(rid, h["room"], text))
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
    async def _run(self, rid: str, room: str, text: str) -> None:
        lock = self._locks.setdefault(room, asyncio.Lock())
        async with lock:                      # one round per huddle at a time
            try:
                await self._round(rid, room, text)
            except Exception as e:  # noqa: BLE001 - a round failing never breaks the huddle
                log.warning("huddle %s: round %s failed: %s", room, rid, e)
                err = str(e)[:300]
                self.store.round_set(rid, state="failed", error=err, ended=time.time())
                with contextlib.suppress(Exception):
                    self.svc._send(room, self.chair_identity(),
                                   "(The chair could not finish this round: " + err + ")", quiet=True)

    def _transcript(self, room: str) -> str:
        lines = []
        for m in self.svc.chat.tail(room, CONTEXT_MESSAGES):
            lines.append(f"[{m['sender']}] {m['text']}")
        text = "\n".join(lines)
        return text[-CONTEXT_CHARS:]

    async def _round(self, rid: str, room: str, text: str) -> None:
        h = self.store.get(room)
        roster = self.roster(h)
        by_id = {r["id"]: r for r in roster}
        max_turns = int(self.setting("max_role_turns", 6))
        mentioned = [m.lower() for m in MENTION.findall(text or "")]
        direct = [r for r in dict.fromkeys(mentioned) if r in by_id]
        plan: dict = {"direct": bool(direct), "calls": [], "answered": False}
        if direct:
            plan["calls"] = [{"role": r, "ask": text} for r in direct[:max_turns]]
        else:
            plan.update(await self._chair_turn(rid, room, h, roster, text))
        self.store.round_set(rid, plan=plan)
        calls = [c for c in plan["calls"] if c["role"] in by_id][:max_turns]
        if not calls:
            self.store.round_set(rid, state="done", ended=time.time())
            return
        replies = [{"role": c["role"], "ask": c["ask"], "state": "running"} for c in calls]
        self.store.round_set(rid, state="roles", replies=replies)
        if not plan["direct"]:
            lines = [f"@{c['role']}: {c['ask']}" for c in calls]
            self.svc._send(room, self.chair_identity(), "\n".join(lines),
                           [f"role:{c['role']}" for c in calls], quiet=True)
        transcript = self._transcript(room)
        results = await asyncio.gather(*(self._role_turn(rid, room, by_id[c["role"]], c["ask"],
                                                         transcript, replies, i)
                                         for i, c in enumerate(calls)))
        answered = [r for r in results if r]
        if len(answered) >= 2:
            self.store.round_set(rid, state="closing")
            await self._close(rid, room, h, roster, text, answered)
        self.store.round_set(rid, state="done", ended=time.time())

    def _chair_messages(self, home: Any, cfg: dict, roster: list[dict], room: str,
                        rules: str, extra: str = "", memory: str = "") -> list[dict]:
        if roster:
            lines = ["Roles in this huddle (people write @<id>):"]
            for r in roster:
                lines.append(f"- @{r['id']} ({r['name']}): {r.get('charter') or ''}".rstrip())
                if r.get("concerns"):
                    lines.append("  concerns: " + "; ".join(r["concerns"][:8]))
        else:
            lines = ["No roles are in this huddle: you answer the person yourself."]
        system = home.system_prompt(cfg, CHAIR_WHERE, memory) + "\n\n" + "\n".join(lines) \
            + "\n\n" + rules
        user = "The huddle so far, oldest first:\n" + self._transcript(room)
        if extra:
            user += "\n\n" + extra
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def _chair_turn(self, rid: str, room: str, h: dict, roster: list[dict],
                          text: str) -> dict:
        home = self.home()
        cfg = home.config()
        cfg["tools"] = False
        state: dict = {"calls": [], "answer": None, "cards": 0}
        ids = {r["id"] for r in roster}
        max_turns = int(self.setting("max_role_turns", 6))

        async def handler(name: str, args: dict) -> str:
            if name == "call_on":
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
            if name == "answer":
                state["answer"] = str(args.get("text") or "").strip()
                return json.dumps({"ok": True})
            if name == "propose":
                return json.dumps(await self._propose(rid, room, self.chair_identity(), args))
            if name == "knowledge_search":
                return await home._run_tool(name, args, room)
            return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

        tools = ([CALL_ON] if roster else []) + [ANSWER, PROPOSE]
        # The chair is the house agent: what it remembers about the asker and
        # the shared facts, and (with home.tools) the knowledge search, as in
        # its chat replies.
        asker = (self.store.round(rid) or {}).get("asker")
        memory = ""
        with contextlib.suppress(Exception):
            if home.may_use_memory(home.chat_principal(asker)):
                memory = (await home.recall_for(text, home.person_of(asker), room))["text"]
        search = bool(home.settings.get("tools")) and home._has_knowledge() and \
            home.may_read_knowledge(home.chat_principal(asker))
        if search:
            tools.append(KNOWLEDGE)
        msgs = self._chair_messages(home, cfg, roster, room, CHAIR_RULES,
                                    "Respond to the newest message.", memory)
        res = await home.tool_turn(msgs, cfg, tools, handler, via="huddle-chair", max_calls=6)
        self.journal("huddle.chair", {"round": rid}, {"ok": True, "result": {
            "model": res.get("model"), "called": [c["role"] for c in state["calls"]],
            "tools": res.get("tools_used")}}, room, home.identity, home.audit())
        answer = state["answer"] or ("" if state["calls"] else (res.get("content") or "").strip())
        if answer:
            sent = self.svc._send(room, home.identity, scrub(answer)[:8000])
            self._attach(rid, home.identity, sent["seq"])
        return {"calls": state["calls"], "answered": bool(answer), "model": res.get("model")}

    async def _role_turn(self, rid: str, room: str, role: dict, ask: str, transcript: str,
                         replies: list[dict], i: int) -> str:
        roles = self.roles()
        chars = int(self.setting("reply_chars", 1200))
        guide = (f"You are answering in a huddle the house agent chairs. Keep your reply under "
                 f"{chars} characters. When something should be done, call propose with the "
                 "exact call (an action card the person approves); do not describe it at length.")
        ident = f"role:{role['id']}"

        async def extra(role_doc: dict, name: str, args: dict) -> str:
            return json.dumps(await self._propose(rid, room, ident, args, role=role_doc))

        try:
            res = await roles.turn(role, ask, "The huddle so far:\n" + transcript,
                                   self.chair_identity(), extra_tools=[PROPOSE],
                                   extra_handler=extra, guide=guide, via=f"huddle-{role['id']}",
                                   preread=True)
            text = (res.get("answer") or "").strip() or "(no answer)"
            if len(text) > chars:
                text = text[:chars - 1].rstrip() + "…"
            sent = self.svc._send(room, ident, scrub(text))
            replies[i].update(state="done", seq=sent["seq"], model=res.get("model"))
            self._attach(rid, ident, sent["seq"])
            out = f"[{role['name']}] {text}"
        except Exception as e:  # noqa: BLE001 - one role failing never sinks the round
            err = str(e)[:300]
            replies[i].update(state="failed", error=err)
            with contextlib.suppress(Exception):
                self.svc._send(room, ident, f"({role['name']} could not answer: {err})", quiet=True)
            out = ""
        self.store.round_set(rid, replies=replies)
        return out

    async def _close(self, rid: str, room: str, h: dict, roster: list[dict], text: str,
                     answered: list[str]) -> None:
        home = self.home()
        cfg = home.config()
        cfg["tools"] = False

        async def handler(name: str, args: dict) -> str:
            if name == "propose":
                return json.dumps(await self._propose(rid, room, self.chair_identity(), args))
            return json.dumps({"ok": False, "error": f"unknown tool {name!r}"})

        cards = [c["summary"] for c in self.store.cards(room, rid)]
        extra = "The roles' answers this round:\n" + "\n\n".join(answered)
        if cards:
            extra += "\n\nCards already proposed:\n" + "\n".join(f"- {c}" for c in cards)
        msgs = self._chair_messages(home, cfg, roster, room, CLOSE_RULES, extra)
        res = await home.tool_turn(msgs, cfg, [PROPOSE], handler, via="huddle-close", max_calls=3)
        body = (res.get("content") or "").strip()
        if body:
            sent = self.svc._send(room, home.identity, scrub(body)[:8000])
            self.store.round_set(rid, close_seq=sent["seq"])
            self._attach(rid, home.identity, sent["seq"])

    # -- cards -------------------------------------------------------------------------
    def _attach(self, rid: str, by: str, seq: int) -> None:
        """Cards ``by`` proposed this round before its message was posted
        belong to that message."""
        for c in self.store.cards(self.store.round(rid)["room"], rid):
            if c["by"] == by and not c["seq"]:
                self.store.card_set(c["id"], seq=seq)

    async def _propose(self, rid: str | None, room: str, by: str, raw: dict,
                       role: dict | None = None, seq: int | None = None) -> dict:
        try:
            card = validate_card(raw)
        except CardError as e:
            return {"ok": False, "error": str(e)}
        same = _key(card)
        for c in self.store.cards(room, rid) if rid else []:
            if _key(c) == same:
                return {"ok": True, "card": c["id"], "duplicate": True}
        card.update(id="c_" + uuid.uuid4().hex[:12], room=room, round=rid, seq=seq, by=by,
                    state="proposed")
        if role is not None and role.get("autonomy") == "act" and card.get("call"):
            roles = self.roles()
            call = card["call"]
            if not roles.scope_refusal(role, call["cap"], None):
                reply = await roles.role_call(role, call["worker"], call["cap"], call["args"])
                card["state"] = "done" if reply.get("ok", True) and "error" not in reply else "failed"
                card["result"] = _short(reply)
        self.store.card_add(card)
        return {"ok": True, "card": card["id"], "state": card["state"]}

    async def decide(self, cid: str, action: str, by: str, principal: Any = None) -> dict:
        """A person approves or dismisses a card. Approving runs its call as
        ``by`` (``principal`` for the policy), journaled with the card and
        huddle ids."""
        card = self.store.card(cid)
        if card is None:
            raise ValueError("no such card")
        if not _is_person(by):
            raise PermissionError("only a person approves or dismisses action cards")
        if action == "dismiss":
            if not self.store.card_set(cid, expect=("proposed",), state="dismissed",
                                       decided_by=by, decided=time.time()):
                raise ValueError(f"that card is already {card['state']}")
            return {"card": cid, "state": "dismissed"}
        if action != "approve":
            raise ValueError("action: approve or dismiss")
        if not self.store.card_set(cid, expect=("proposed",), state="approved", decided_by=by,
                                   decided=time.time()):
            raise ValueError(f"that card is already {card['state']}")
        if not card.get("call"):              # a note or a question: approving acknowledges it
            self.store.card_set(cid, state="done", result={"ok": True})
            return {"card": cid, "state": "done"}
        reply = await self._run_call(card, by, principal)
        ok = bool(reply.get("ok", True)) and not reply.get("error") and not reply.get("denied")
        state = "done" if ok else "failed"
        self.store.card_set(cid, state=state, result=_short(reply))
        return {"card": cid, "state": state, "result": _short(reply)}

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
        try:
            if client is None:
                reply = await asyncio.wait_for(node.dispatch(call["cap"], call["args"], by),
                                               CALL_TIMEOUT)
            else:
                reply = await client.call(cap=call["cap"], args=call["args"], target=wid,
                                          timeout=CALL_TIMEOUT, identity=by, principal=principal)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": f"no reply within {CALL_TIMEOUT:.0f}s"}
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        reply = scrub(reply) if isinstance(reply, dict) else {"ok": True, "result": reply}
        self.journal("huddle.card", args, {"ok": reply.get("ok", True), "result": _short(reply)},
                     card["room"], by)
        return reply


def _short(reply: dict) -> dict:
    """A reply small enough to keep on the card."""
    text = json.dumps(reply, default=str)
    if len(text) <= 2000:
        return reply
    return {"ok": reply.get("ok", True), "truncated": text[:2000]}
