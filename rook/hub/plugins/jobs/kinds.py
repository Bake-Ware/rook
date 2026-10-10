"""The built-in step kinds: cap, fanout, tool, wait, notify, join, noop.

Each registers itself with :func:`.steps.register_step_kind`. ``join`` is
settled by the executor (it waits on other branches); its entry here carries
the validator and schema only.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .cron import parse_duration, parse_instant
from .expr import compile_expr
from .steps import (StepContext, StepResult, apply_success_rules, exit_code_of,
                    register_step_kind, step_kind)

#: Seconds between attempts to find an offline worker (until the step times out).
OFFLINE_RETRY = 5.0
#: Calls get this much less than the step's timeout, so a slow reply comes
#: back as the call's own timeout (with its detail) rather than the step's.
CALL_MARGIN = 1.0
VIAS = {"voice": "voice.speak", "notify": "notify.post", "telegram": "notify.send"}
FILTER_KEYS = ("has_cap", "os", "names", "tags")


# -- worker selection -------------------------------------------------------------

def _caps(entry: dict) -> list:
    return list(entry.get("caps") or [])


def _name(wid: str, entry: dict) -> str:
    return str(entry.get("name") or wid[:8])


def _tags(entry: dict) -> set:
    tags = entry.get("tags") or (entry.get("facts") or {}).get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return {str(t).lower() for t in tags if t}


def matches(entry: dict, flt: dict, cap: str) -> bool:
    """Whether a roster entry passes a fanout / ``worker.filter`` filter."""
    if (flt.get("has_cap") or cap) not in _caps(entry):
        return False
    if flt.get("os") and str((entry.get("facts") or {}).get("os") or "").lower() \
            != str(flt["os"]).lower():
        return False
    names = flt.get("names")
    if names and str(entry.get("name") or "").lower() not in {str(n).lower() for n in names}:
        return False
    tags = flt.get("tags")
    if tags and not {str(t).lower() for t in tags} <= _tags(entry):
        return False
    return True


def resolve_worker(roster: dict, spec: Any, cap: str, hub_id: str) -> tuple[str, str] | None:
    """``(worker id, name)`` for a step's ``worker``, or ``None`` when no live
    worker fits (offline: the caller retries until its timeout). Raises
    ValueError for a spec that can never work (an ambiguous name)."""
    if isinstance(spec, str):
        if spec.lower() == "rook":
            return (hub_id, "rook") if hub_id in roster else None
        if spec in roster:
            return spec, _name(spec, roster[spec])
        named = [wid for wid, e in roster.items() if str(e.get("name") or "").lower() == spec.lower()]
        if len(named) > 1:
            raise ValueError(f"worker name {spec!r} is ambiguous ({len(named)} live workers); use its id")
        return (named[0], _name(named[0], roster[named[0]])) if named else None
    if isinstance(spec, dict):
        flt = spec.get("filter") if isinstance(spec.get("filter"), dict) else {}
        found = sorted((_name(wid, e).lower(), wid) for wid, e in roster.items() if matches(e, flt, cap))
        if found:
            wid = found[0][1]
            return wid, _name(wid, roster[wid])
        return None
    raise ValueError("worker must be a name, \"rook\", {\"any_with_cap\": true} or {\"filter\": {...}}")


def result_from_reply(reply: Any) -> StepResult:
    """A cap reply as a step result: ``denied`` -> blocked, ``ok: false``
    (the reply's or the result's own) -> failure, else success."""
    if not isinstance(reply, dict):
        return StepResult("failure", error="malformed reply")
    if reply.get("denied"):
        return StepResult("blocked", output=reply.get("denied"), error=str(reply.get("error") or "denied"))
    if not reply.get("ok"):
        return StepResult("failure", error=str(reply.get("error") or "call failed"))
    result = reply.get("result")
    if isinstance(result, dict) and result.get("ok") is False:
        return StepResult("failure", output=result, error=str(result.get("error") or "the cap said ok: false"),
                          exit_code=exit_code_of(result))
    return StepResult("success", output=result, exit_code=exit_code_of(result))


async def call_cap(ctx: StepContext, cap: str, raw_args: dict, spec: Any) -> StepResult:
    """Find the worker (retrying while it is offline), render args, call,
    journal (placeholders, not values) and turn the reply into a result."""
    rt = ctx.runtime
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.timeout
    while True:
        found = resolve_worker(rt.roster(), spec, cap, rt.hub_id)
        if found:
            break
        await rt.sleep(min(OFFLINE_RETRY, max(0.05, deadline - loop.time())))
    wid, name = found
    shown = ctx.template(raw_args)
    args = ctx.render(raw_args, via=f"job {ctx.job.get('name')} step {ctx.step_id}: {cap} on {name}")
    remaining = deadline - loop.time()
    wait = max(0.01, remaining - min(CALL_MARGIN, remaining / 10))
    try:
        reply = await rt.call(cap, args, wid, wait, ctx.identity)
    except asyncio.TimeoutError:
        rt.journal(cap, name, ctx.identity, shown, {"ok": False, "timeout": True,
                                                   "error": f"no reply within {wait:.0f}s"})
        raise
    reply = rt.mask(reply, ctx.used)
    rt.journal(cap, name, ctx.identity, shown, reply)
    res = result_from_reply(reply)
    res.extra["worker"] = name
    return res


# -- cap ---------------------------------------------------------------------------

def _check_worker(spec: Any) -> list:
    if isinstance(spec, str) and spec.strip():
        return []
    if isinstance(spec, dict):
        if spec.get("any_with_cap") is True and set(spec) == {"any_with_cap"}:
            return []
        if isinstance(spec.get("filter"), dict) and set(spec) == {"filter"}:
            return _check_filter(spec["filter"], "worker.filter")
    return ['worker must be a name, "rook", {"any_with_cap": true} or {"filter": {...}}']


def _check_filter(flt: Any, where: str = "filter") -> list:
    if not isinstance(flt, dict):
        return [f"{where} must be an object"]
    errs = [f"{where}: unknown key {k!r} (use {', '.join(FILTER_KEYS)})" for k in flt if k not in FILTER_KEYS]
    for k in ("names", "tags"):
        if k in flt and not (isinstance(flt[k], list) and all(isinstance(x, str) for x in flt[k])):
            errs.append(f"{where}.{k} must be a list of strings")
    for k in ("has_cap", "os"):
        if k in flt and not isinstance(flt[k], str):
            errs.append(f"{where}.{k} must be a string")
    return errs


def _check_cap_fields(step: dict) -> list:
    errs = []
    if not isinstance(step.get("cap"), str) or not step["cap"].strip():
        errs.append("cap is required (e.g. shell.exec)")
    if "args" in step and not isinstance(step["args"], dict):
        errs.append("args must be an object")
    return errs


def check_cap(step: dict) -> list:
    return _check_cap_fields(step) + _check_worker(step.get("worker"))


async def run_cap(ctx: StepContext, step: dict) -> StepResult:
    return await call_cap(ctx, step["cap"], step.get("args") or {}, step.get("worker"))


WORKER_SCHEMA = {"oneOf": [
    {"type": "string", "description": "worker name or id; \"rook\" is the hub"},
    {"type": "object", "properties": {"any_with_cap": {"const": True}}, "required": ["any_with_cap"]},
    {"type": "object", "properties": {"filter": {"$ref": "#/$defs/filter"}}, "required": ["filter"]}]}


# -- fanout ------------------------------------------------------------------------

def _join_rule(rule: Any) -> list:
    if rule in (None, "all", "any"):
        return []
    if isinstance(rule, dict) and set(rule) == {"at_least"} and isinstance(rule["at_least"], int) \
            and not isinstance(rule["at_least"], bool) and rule["at_least"] >= 1:
        return []
    return ['join must be "all", "any" or {"at_least": N}']


def rule_met(rule: Any, ok: int, total: int) -> bool:
    if rule in (None, "all"):
        return ok == total
    if rule == "any":
        return ok >= 1
    return ok >= int(rule["at_least"])


def check_fanout(step: dict) -> list:
    return _check_cap_fields(step) + _check_filter(step.get("filter") or {}) + _join_rule(step.get("join"))


async def run_fanout(ctx: StepContext, step: dict) -> StepResult:
    rt = ctx.runtime
    cap, flt = step["cap"], step.get("filter") or {}
    roster = rt.roster()
    targets = sorted((_name(wid, e), wid) for wid, e in roster.items() if matches(e, flt, cap))
    if not targets:
        return StepResult("failure", error=f"no live worker matches the filter for {cap}", checked=True)
    shown = ctx.template(step.get("args") or {})
    args = ctx.render(step.get("args") or {}, via=f"job {ctx.job.get('name')} step {ctx.step_id}: {cap} fanout")
    wait = max(0.01, ctx.timeout - min(CALL_MARGIN, ctx.timeout / 10))

    async def one(name: str, wid: str) -> tuple[str, StepResult]:
        try:
            reply = await rt.call(cap, args, wid, wait, ctx.identity)
        except asyncio.TimeoutError:
            rt.journal(cap, name, ctx.identity, shown, {"ok": False, "timeout": True})
            return name, StepResult("hang", error=f"no reply within {wait:.0f}s")
        except Exception as e:  # noqa: BLE001 - one worker's error is that worker's outcome
            return name, StepResult("failure", error=str(e))
        reply = rt.mask(reply, ctx.used)
        rt.journal(cap, name, ctx.identity, shown, reply)
        return name, apply_success_rules(step, result_from_reply(reply))

    results = await asyncio.gather(*(one(n, w) for n, w in targets))
    per = {n: {"state": r.state, "exit_code": r.exit_code,
               **({"error": r.error} if r.error else {"output": r.output})} for n, r in results}
    ok = sum(1 for _, r in results if r.ok)
    rule = step.get("join")
    if rule_met(rule, ok, len(results)):
        state = "success"
    elif any(r.state == "hang" for _, r in results):
        state = "hang"
    elif any(r.state == "blocked" for _, r in results) and ok == 0:
        state = "blocked"
    else:
        state = "failure"
    return StepResult(state, output=per, checked=True,
                      error=None if state == "success" else f"{ok}/{len(results)} workers succeeded",
                      extra={"workers": len(results), "succeeded": ok})


# -- tool --------------------------------------------------------------------------

def check_tool(step: dict) -> list:
    errs = []
    if not isinstance(step.get("tool"), str) or not step["tool"].startswith("rook_"):
        errs.append("tool must name a hub MCP tool such as rook_task")
    if "args" in step and not isinstance(step["args"], dict):
        errs.append("args must be an object (the tool's arguments, e.g. {\"action\": \"deck\"})")
    return errs


async def run_tool(ctx: StepContext, step: dict) -> StepResult:
    args = ctx.render(step.get("args") or {})
    reply = await ctx.runtime.tool(step["tool"], args, ctx.identity)
    if isinstance(reply, str):
        try:
            reply = json.loads(reply)
        except ValueError:
            pass
    reply = ctx.runtime.mask(reply, ctx.used)
    if isinstance(reply, dict) and "ok" in reply:
        if reply.get("ok"):
            return StepResult("success", output=reply.get("result"), exit_code=exit_code_of(reply.get("result")))
        return StepResult("failure", output=reply, error=str(reply.get("error") or "tool failed"))
    return StepResult("success", output=reply)


# -- wait --------------------------------------------------------------------------

def check_wait(step: dict) -> list:
    has_for, has_until = "for" in step, "until" in step
    if has_for == has_until:
        return ['wait needs exactly one of "for" (a duration) or "until" (a zone-aware time)']
    try:
        if has_for:
            parse_duration(step["for"])
        else:
            parse_instant(step["until"])
    except ValueError as e:
        return [str(e)]
    return []


async def run_wait(ctx: StepContext, step: dict) -> StepResult:
    rt = ctx.runtime
    delay = parse_duration(step["for"]) if "for" in step else max(0.0, parse_instant(step["until"]) - rt.clock())
    await rt.sleep(delay)
    return StepResult("success", output={"waited": round(delay, 3)})


# -- notify ------------------------------------------------------------------------

def check_notify(step: dict) -> list:
    errs = []
    if step.get("via", "notify") not in VIAS:
        errs.append(f"via must be one of {', '.join(VIAS)}")
    if not isinstance(step.get("text"), str) or not step["text"].strip():
        errs.append("text is required")
    if "worker" in step:
        errs += _check_worker(step["worker"])
    return errs


async def run_notify(ctx: StepContext, step: dict) -> StepResult:
    via = step.get("via", "notify")
    cap = VIAS[via]
    title = step.get("title") or f"Rook job {ctx.job.get('name')}"
    if via == "telegram":
        return await call_cap(ctx, cap, {"text": step["text"], "channel": "telegram"}, "rook")
    args = {"title": title, "text": step["text"]} if via == "notify" else {"text": step["text"], "wait": False}
    spec = step.get("worker") or ctx.settings("notify_worker") or {"any_with_cap": True}
    return await call_cap(ctx, cap, args, spec)


# -- join and noop -------------------------------------------------------------------

def check_join(step: dict) -> list:
    cond = step.get("condition", "all")
    if cond in ("all", "any"):
        return []
    if isinstance(cond, dict):
        return _join_rule(cond)
    if isinstance(cond, str):
        try:
            compile_expr(cond)
        except ValueError as e:
            return [f"condition: {e}"]
        return []
    return ['condition must be "all", "any", {"at_least": N} or an expression']


async def run_join(ctx: StepContext, step: dict) -> StepResult:  # pragma: no cover - the executor settles joins
    raise RuntimeError("join steps are settled by the executor")


async def run_noop(ctx: StepContext, step: dict) -> StepResult:
    return StepResult("success")


def register_builtins() -> None:
    builtins = (
        ("cap", run_cap, check_cap, {
            "worker": {"$ref": "#/$defs/worker"}, "cap": {"type": "string"},
            "args": {"type": "object"}}),
        ("fanout", run_fanout, check_fanout, {
            "cap": {"type": "string"}, "args": {"type": "object"}, "filter": {"$ref": "#/$defs/filter"},
            "join": {"$ref": "#/$defs/rule"}}),
        ("tool", run_tool, check_tool, {
            "tool": {"type": "string", "pattern": "^rook_"}, "args": {"type": "object"}}),
        ("wait", run_wait, check_wait, {
            "for": {"$ref": "#/$defs/duration"},
            "until": {"type": "string", "description": "ISO 8601 with offset"}}),
        ("notify", run_notify, check_notify, {
            "via": {"enum": list(VIAS)}, "text": {"type": "string"}, "title": {"type": "string"},
            "worker": {"$ref": "#/$defs/worker"}}),
        ("join", run_join, check_join, {
            "condition": {"oneOf": [{"enum": ["all", "any"]}, {"$ref": "#/$defs/rule"},
                                    {"type": "string", "description": "expression, e.g. steps.a.ok and not steps.b.ok"}]}}),
        ("noop", run_noop, None, {}),
    )
    for name, run, check, schema in builtins:
        if step_kind(name) is None:
            register_step_kind(name, run, validate=check, schema=schema)


register_builtins()
