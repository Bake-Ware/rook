"""The ``huddle`` step kind (docs/design/huddles.md 6): one round of a chaired
huddle, its synthesis delivered.

``{"kind": "huddle", "prompt": "What's on deck today?", "roles": ["pm", "qa"],
"deliver": "telegram", "title": "Standup", "huddle": "<room>"}`` posts the
prompt (templated) into the huddle as the job (``job:<id>``), which starts a
round: the chair calls on roles, they answer, the chair closes. The step waits
for the round (its timeout), and its text is the chair's synthesis (or its
answer, or the roles' replies when only one answered). ``deliver`` (optional:
``telegram``, ``voice`` or ``notify``, as the ``notify`` step) sends that text
with the number of action cards waiting; the cards stay in the huddle for a
person to approve.

``huddle`` reuses an existing chaired huddle; without it each run starts one
titled ``title`` (default the job's name) with ``roles`` (default
``huddle.default_roles``). A weekday 08:30 standup is a cron job with one
such step.
"""
from __future__ import annotations

import time
from typing import Any

from .kinds import VIAS, _check_worker, call_cap, notify_call
from .steps import StepContext, StepResult, register_step_kind, step_kind

PROMPT_MAX = 8000
DELIVER_MAX = 3500
KEYS = ("prompt", "roles", "deliver", "title", "huddle", "worker")


def check_huddle(step: dict) -> list:
    errs = []
    prompt = step.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errs.append("prompt is required (what the person would ask the huddle)")
    elif len(prompt) > PROMPT_MAX:
        errs.append(f"prompt: at most {PROMPT_MAX} characters")
    elif "{{secret:" in prompt:
        errs.append("prompt: never put {{secret:…}} in a huddle")
    roles = step.get("roles")
    if roles is not None and not (isinstance(roles, list)
                                  and all(isinstance(r, str) and r.strip() for r in roles)):
        errs.append("roles: a list of role ids (default: huddle.default_roles)")
    if step.get("deliver") not in (None, *VIAS):
        errs.append(f"deliver: one of {', '.join(VIAS)} (or leave it out)")
    for k in ("title", "huddle"):
        if k in step and not isinstance(step[k], str):
            errs.append(f"{k}: a string")
    if "worker" in step:
        errs += _check_worker(step["worker"])
    return errs


def huddles(ctx: StepContext) -> Any:
    """The huddle service, or None (tests set ``runtime.huddles``)."""
    found = getattr(ctx.runtime, "huddles", None)
    if found is not None:
        return found
    node = getattr(ctx.runtime, "node", None)
    plugin = node.plugin("huddle") if node is not None and hasattr(node, "plugin") else None
    return getattr(plugin, "service", None)


def _outcome(svc: Any, room: str, rnd: dict) -> tuple[str, list[dict]]:
    """The round's synthesis (or answer, or replies) and its open cards."""
    msgs = [m for m in svc.chat.tail(room, 60) if m["seq"] > int(rnd.get("trigger_seq") or 0)]
    chair = svc.chair.chair_identity()
    text = ""
    if rnd.get("close_seq"):
        text = next((m["text"] for m in msgs if m["seq"] == rnd["close_seq"]), "")
    if not text:
        said = [m for m in msgs if m["sender"] == chair and not m["text"].startswith("@")]
        roles = [m for m in msgs if str(m["sender"]).startswith("role:")]
        text = said[-1]["text"] if said else "\n\n".join(
            f"{m['sender'].split(':', 1)[1]}: {m['text']}" for m in roles)
    cards = [c for c in svc.store.cards(room, rnd["id"]) if c["state"] == "proposed"]
    return text.strip(), cards


async def run_huddle(ctx: StepContext, step: dict) -> StepResult:
    svc = huddles(ctx)
    if svc is None or getattr(svc, "chair", None) is None:
        return StepResult("failure", error="huddles are off on this hub (the huddle plugin)")
    job = f"job:{ctx.job.get('id') or ctx.job.get('name') or '?'}"
    prompt = ctx.template(step["prompt"])
    try:
        room = step.get("huddle")
        if not room:
            title = ctx.template(step.get("title") or ctx.job.get("name") or "Huddle")
            room = (await svc.start(job, f"{title} {time.strftime('%Y-%m-%d %H:%M')}",
                                    step.get("roles")))["room"]
        posted = await svc.post(job, room, prompt)
    except (ValueError, KeyError, PermissionError) as e:
        return StepResult("failure", error=str(e)[:500])
    rid = posted.get("round")
    if not rid:
        return StepResult("failure", error="the post started no round", output={"huddle": room})
    rnd = await svc.chair.wait(rid, max(5.0, ctx.timeout - 10.0))
    state = rnd.get("state")
    if state not in ("done", "failed"):
        return StepResult("hang", error="the round did not end within the step's timeout",
                          output={"huddle": room, "round": rid, "state": state})
    text, cards = _outcome(svc, room, rnd)
    output = {"huddle": room, "round": rid, "state": state, "text": text,
              "cards": [{"id": c["id"], "kind": c["kind"], "by": c["by"], "summary": c["summary"]}
                        for c in cards]}
    if state == "failed" or not text:
        return StepResult("failure", output=output, error=rnd.get("error") or "nobody answered",
                          extra={"huddle": room})
    via = step.get("deliver")
    if via:
        body = text[:DELIVER_MAX]
        if cards:
            body += f"\n\n{len(cards)} action card(s) wait for you in the huddle: " + "; ".join(
                c["summary"] for c in cards[:5])
        title = ctx.template(step.get("title") or ctx.job.get("name") or "Huddle")
        cap, args = notify_call(via, body, title)
        spec = "rook" if via == "telegram" else (step.get("worker") or ctx.settings("notify_worker")
                                                 or {"any_with_cap": True})
        sent = await call_cap(ctx, cap, args, spec)
        output["delivered"] = {"via": via, "ok": sent.ok, **({"error": sent.error} if not sent.ok else {})}
        if not sent.ok:
            return StepResult("failure", output=output, error=f"delivery failed: {sent.error}",
                              text=text, extra={"huddle": room})
    return StepResult("success", output=output, text=text, extra={"huddle": room, "round": rid})


SCHEMA = {
    "prompt": {"type": "string"},
    "roles": {"type": "array", "items": {"type": "string"},
              "description": "role ids (default: huddle.default_roles)"},
    "deliver": {"enum": list(VIAS), "description": "send the synthesis (optional)"},
    "title": {"type": "string"}, "huddle": {"type": "string", "description": "reuse this huddle"},
    "worker": {"$ref": "#/$defs/worker"},
}


def register() -> None:
    if step_kind("huddle") is None:
        register_step_kind("huddle", run_huddle, validate=check_huddle, schema=SCHEMA)


register()
