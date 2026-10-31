"""Questions for a person (castling-caps C4): ``approval.ask``.

Any agent, including an external MCP agent, asks the person a question
through the one approval path: the question waits beside the held calls
(``approval.list``, the Approvals page, ``GET /holds``) and is answered with
``approval.decide {hold, answer}``. No call is attached, so nothing runs on
an answer: the answer goes back to the asker (its waiting ``approval.ask``,
or ``approval.get`` later). ``approval.asked`` lets the house agent (with the
agents package) get it to the person the way that fits; core alone leaves it
on the Approvals page. Like holds, questions don't expire and are kept in
memory (a hub restart drops them).

Who answers: the person it was asked for (an agent acting for them relays
their words, and is recorded as who answered), or an owner when nobody was
behind the asker. The asker never answers its own question.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from .identity import Caller

MAX_QUESTION = 2000
MAX_OPTIONS = 10
MAX_ANSWER = 2000
KEEP_ANSWERED = 500          # answered questions kept for approval.get


@dataclass
class Ask:
    id: str
    asker: Caller
    question: str
    options: list
    why: str
    created: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    answer: str | None = None
    by: str | None = None
    answered: float | None = None

    @property
    def person(self) -> str | None:
        return self.asker.person

    def view(self) -> dict:
        out = {"hold": self.id, "kind": "ask", "question": self.question, "why": self.why,
               "person": self.asker.person, "agent": self.asker.agent, "created": self.created}
        if self.options:
            out["options"] = list(self.options)
        if self.answered is not None:
            out.update(answer=self.answer, by=self.by, answered=self.answered)
        return out


class Asks:
    def __init__(self, emit: Callable[[str, dict, str], Any], is_owner: Callable[[str | None], bool],
                 clock: Callable[[], float] = time.time):
        self.emit, self.is_owner, self.clock = emit, is_owner, clock
        self.open: dict[str, Ask] = {}
        self.answered: OrderedDict[str, Ask] = OrderedDict()

    @staticmethod
    def owns(name: Any) -> bool:
        return isinstance(name, str) and name.startswith("q_")

    async def ask(self, caller: Caller, why: str, question: Any, options: Any = None, wait: Any = 0) -> dict:
        if not (isinstance(question, str) and question.strip()) or len(question) > MAX_QUESTION:
            return {"ok": False, "error": f"bad args: question is text (at most {MAX_QUESTION} characters)"}
        options = list(options or [])
        if len(options) > MAX_OPTIONS or not all(isinstance(o, str) and o.strip() for o in options) \
                or len(set(options)) != len(options):
            return {"ok": False, "error": f"bad args: options are up to {MAX_OPTIONS} different answers"}
        if not (caller.person or caller.agent):
            return {"ok": False, "error": "denied: a question needs someone asking"}
        a = Ask(f"q_{secrets.token_hex(8)}", caller, question.strip(), options, why or "", self.clock())
        self.open[a.id] = a
        self.emit("approval.asked", {"hold": a.id, "question": a.question, "options": a.options, "why": a.why,
                                     "person": a.person, "agent": caller.agent}, "core")
        return await self.wait(a, wait)

    async def wait(self, a: Ask, wait: Any) -> dict:
        try:
            seconds = max(0.0, float(wait or 0))
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds and a.answered is None:
            try:
                await asyncio.wait_for(asyncio.shield(a.done.wait()), seconds)
            except asyncio.TimeoutError:
                pass
        return {"ok": True, **self.result(a)}

    def result(self, a: Ask) -> dict:
        out = {"hold": a.id, "answered": a.answered is not None}
        if a.answered is not None:
            out.update(answer=a.answer, by=a.by)
        return out

    def find(self, name: str) -> Ask | None:
        return self.open.get(name) or self.answered.get(name)

    def visible(self, caller: Caller, a: Ask) -> bool:
        return self.is_owner(caller.person) or a.asker == caller or \
            (caller.person is not None and a.person == caller.person)

    def view(self, caller: Caller) -> list[dict]:
        return [a.view() for a in self.open.values() if self.visible(caller, a)]

    def get(self, caller: Caller, name: Any) -> dict:
        a = self.find(name) if self.owns(name) else None
        if a is None or not self.visible(caller, a):
            return {"ok": False, "error": f"unknown question: {name}"}
        return {"ok": True, **a.view()}

    def may_answer(self, caller: Caller, a: Ask) -> bool:
        by_agent = caller.agent is not None
        if by_agent and caller.agent == a.asker.agent:
            return False                          # never one's own question
        if a.person is not None:
            return caller.person == a.person      # the person, or an agent relaying for them
        return not by_agent and self.is_owner(caller.person)

    def decide(self, caller: Caller, name: str, answer: Any) -> dict:
        a = self.open.get(name)
        if a is None:
            done = self.answered.get(name)
            return {"ok": False, "error": f"{name} was answered already" if done and self.visible(caller, done)
                    else f"unknown hold: {name}"}
        by = caller.agent or caller.person or "?"
        if not self.may_answer(caller, a):
            return {"ok": False, "error": f"{by} may not answer {name}: it was asked of "
                                          + (a.person or "the owners")}
        if not (isinstance(answer, str) and answer.strip()) or len(answer) > MAX_ANSWER:
            return {"ok": False, "error": f"bad request: answer is text (at most {MAX_ANSWER} characters)"}
        answer = answer.strip()
        if a.options and answer not in a.options:
            return {"ok": False, "error": "bad request: answer one of: " + ", ".join(a.options)}
        if caller.agent and caller.person:
            by = f"{caller.agent} for {caller.person}"
        a.answer, a.by, a.answered = answer, by, self.clock()
        del self.open[name]
        self.answered[name] = a
        while len(self.answered) > KEEP_ANSWERED:
            self.answered.popitem(last=False)
        a.done.set()
        self.emit("approval.answered", {"hold": a.id, "answer": answer, "by": by, "person": a.person,
                                        "agent": a.asker.agent}, "core")
        return {"ok": True, **self.result(a)}

    def close(self) -> None:
        """Hub stopping: wake every waiting asker (unanswered)."""
        for a in self.open.values():
            a.done.set()
