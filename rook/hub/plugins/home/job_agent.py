"""The house agent working a job step: an opt-in tool set and a bounded loop.

Chat and ``home.ask`` never see these tools; only a job's ``agent`` step
(docs/design/jobs.md 6) hands them to the model, through
:meth:`HomeAgent.work_job_step`. The tools do nothing themselves: each one is
a call on a *backend* that the jobs plugin supplies
(:class:`rook.hub.plugins.jobs.agent_kinds.JobAgentBackend`). The backend
acts on behalf of the job's identity, applies the step's tool scope and the
job's guardrails to every call, masks results and journals everything, so the
policy chain is home -> job -> job identity and the agent can never do more
than the job itself could.

The loop is bounded: at most ``max_calls`` tool calls (then only ``finish``
is offered), a fixed number of model rounds, and the caller's time budget.
The agent ends with ``finish(verdict, text)``; a final answer without it is
read for a ``VERDICT: ok|failed`` line or a JSON ``{"verdict": ...}``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

#: Most characters of one tool result handed back to the model.
RESULT_CHARS = 6000
#: Most characters of the run context put in the first message.
CONTEXT_CHARS = 16000
DEFAULT_MAX_CALLS = 20
MAX_CALLS = 100
VERDICTS = ("ok", "failed")
_VERDICT_LINE = re.compile(r"\bVERDICT\s*:\s*(ok|failed)\b[ \t:.-]*(.*)", re.I)


def _fn(name: str, description: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


#: The opt-in job tool set, by name. ``finish`` is always offered.
TOOLS: dict[str, dict] = {
    "rook_workers": _fn(
        "rook_workers", "List the live workers you may call: name, os and the caps your scope allows."),
    "rook_call": _fn(
        "rook_call", "Call a cap on a worker (worker \"rook\" is the hub) on behalf of the job. "
        "Returns {ok, state, result|error}. Use {{secret:name}} for credentials, never values.",
        {"worker": {"type": "string"}, "cap": {"type": "string"}, "args": {"type": "object"}},
        ["worker", "cap"]),
    "rook_tool": _fn(
        "rook_tool", "Call a hub MCP tool such as rook_task (the task deck), rook_knowledge or "
        "rook_jobs, with its usual arguments.",
        {"tool": {"type": "string"}, "args": {"type": "object"}}, ["tool"]),
    "job_run": _fn(
        "job_run", "Read this job run's step records so far (masked). step: one step id, or omit "
        "for all.", {"step": {"type": "string"}}),
    "notify_bake": _fn(
        "notify_bake", "Send Bake a short message: via notify (phone notification, default), "
        "voice (spoken on the phone) or telegram.",
        {"text": {"type": "string"}, "via": {"enum": ["notify", "voice", "telegram"]},
         "title": {"type": "string"}}, ["text"]),
    "ask_bake": _fn(
        "ask_bake", "Ask Bake a question out loud on the phone and wait for his answer (he can "
        "also type it in the notification). Returns {reply} or no answer. A reply is what the mic "
        "heard: confirm before anything risky.",
        {"question": {"type": "string"}}, ["question"]),
    "finish": _fn(
        "finish", "End your work on this step with a verdict: ok (done, criteria met) or failed, "
        "and a short summary.",
        {"verdict": {"enum": list(VERDICTS)}, "text": {"type": "string"}}, ["verdict", "text"]),
}


class JobBackend(Protocol):
    """What the jobs plugin provides. Every method returns JSON-able data
    (already masked) and never raises for an ordinary refusal."""

    def tool_names(self) -> list[str]: ...
    async def workers(self) -> Any: ...
    async def call(self, worker: str, cap: str, args: dict) -> Any: ...
    async def tool(self, name: str, args: dict) -> Any: ...
    async def run_state(self, step: str | None) -> Any: ...
    async def notify(self, text: str, via: str, title: str) -> Any: ...
    async def ask(self, question: str) -> Any: ...


WHERE = ("You are working one step of a scheduled Rook job, not chatting. You act on behalf of "
         "the job: every tool call is checked against what the job may do, and journaled. "
         "Use the tools to do the work and check results; do not invent results. Reach Bake "
         "only when the job needs him (a blocker, a decision, a result he asked for). "
         "When you are done, call finish with verdict ok or failed and a one or two sentence "
         "summary.")


def parse_verdict(text: str) -> tuple[str | None, str]:
    """A verdict from a final answer that did not call ``finish``: a JSON
    object with ``verdict``, or a ``VERDICT: ok|failed <text>`` line."""
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            d = json.loads(t)
            v = str(d.get("verdict") or "").lower()
            if v in VERDICTS:
                return v, str(d.get("text") or "")
        except (ValueError, AttributeError):
            pass
    m = None
    for m in _VERDICT_LINE.finditer(t):
        pass
    if m is not None:
        return m.group(1).lower(), (m.group(2).strip() or t[: m.start()].strip())
    return None, t


def _args(raw: Any) -> dict:
    try:
        out = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _dump(obj: Any, limit: int = RESULT_CHARS) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + f"…[truncated {len(text) - limit} chars]"


async def dispatch(backend: JobBackend, name: str, args: dict) -> Any:
    if name not in backend.tool_names():
        return {"ok": False, "error": f"tool {name!r} is not available to this step"}
    if name == "rook_workers":
        return await backend.workers()
    if name == "rook_call":
        cargs = args.get("args") if isinstance(args.get("args"), dict) else {}
        return await backend.call(str(args.get("worker") or ""), str(args.get("cap") or ""), cargs)
    if name == "rook_tool":
        targs = args.get("args") if isinstance(args.get("args"), dict) else {}
        return await backend.tool(str(args.get("tool") or ""), targs)
    if name == "job_run":
        return await backend.run_state(str(args["step"]) if args.get("step") else None)
    if name == "notify_bake":
        via = str(args.get("via") or "notify")
        return await backend.notify(str(args.get("text") or ""), via, str(args.get("title") or ""))
    if name == "ask_bake":
        return await backend.ask(str(args.get("question") or ""))
    return {"ok": False, "error": f"unknown tool {name!r}"}


async def run_loop(cli: Any, messages: list[dict], backend: JobBackend, *, max_calls: int,
                   max_tokens: int | None = None, temperature: float | None = None) -> dict:
    """The bounded tool loop. Returns ``{verdict, text, calls, tools_used,
    model, rounds}``; ``verdict`` is None when the agent never gave one."""
    max_calls = max(0, min(int(max_calls), MAX_CALLS))
    names = [n for n in backend.tool_names() if n in TOOLS and n != "finish"]
    msgs = list(messages)
    used: list[str] = []
    calls = 0
    model = ""
    # Every round with tools spends at least one call; two more rounds let
    # the agent answer after its budget is gone.
    for rounds in range(1, max_calls + 3):
        offer = [TOOLS[n] for n in names] if calls < max_calls else []
        res = await cli.chat(msgs, max_tokens=max_tokens, temperature=temperature,
                             tools=offer + [TOOLS["finish"]])
        model = res.get("model") or model
        tool_calls = res.get("tool_calls") or []
        if not tool_calls:
            verdict, text = parse_verdict(res.get("content") or "")
            return {"verdict": verdict, "text": cli._scrub(text), "calls": calls, "tools_used": used,
                    "model": cli._scrub(model), "rounds": rounds}
        msgs.append({"role": "assistant", "content": res.get("content") or None, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = str(fn.get("name") or "")
            args = _args(fn.get("arguments"))
            if name == "finish":
                verdict = str(args.get("verdict") or "").lower()
                return {"verdict": verdict if verdict in VERDICTS else "failed",
                        "text": cli._scrub(str(args.get("text") or ""))[:2000], "calls": calls,
                        "tools_used": used, "model": cli._scrub(model), "rounds": rounds}
            if calls >= max_calls:
                out: Any = {"ok": False, "error": f"tool budget used up ({max_calls} calls); call finish"}
            else:
                calls += 1
                used.append(name)
                try:
                    out = await dispatch(backend, name, args)
                except Exception as e:  # noqa: BLE001 - a tool error goes back to the model
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            msgs.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                         "content": cli._scrub(_dump(out))})
    return {"verdict": None, "text": "the agent kept calling tools without finishing", "calls": calls,
            "tools_used": used, "model": cli._scrub(model), "rounds": max_calls + 2}


def first_message(prompt: str, context: Any) -> str:
    body = prompt.strip()
    if context:
        body += "\n\nRun context (masked; secrets show as {{secret:name}}):\n" + _dump(context, CONTEXT_CHARS)
    return body
