"""The ``agent`` and ``ask`` step kinds (docs/design/jobs.md 5-6).

``agent`` hands a prompt, plus the run output it is allowed to see, to an
agent:

* ``"home"`` (the default; the hub setting ``job.default_agent`` changes it):
  the house agent (``home`` plugin) works the step with its opt-in job tool
  set (:mod:`rook.hub.plugins.home.job_agent`). :class:`JobAgentBackend`
  carries out each tool call *on behalf of the job*: the call's identity is
  the run's, with ``agent:<name>`` and ``job:<id>`` in its on-behalf-of chain,
  the step's ``tools`` scope narrows it, :func:`.guardrails.check_step` is
  asked for every call (a refusal goes back to the model as ``blocked``),
  and the band's policy applies as for any job call. Results are masked and
  every call, refusals included, is journaled.
* ``{"session": {...}}``: a Claude or Codex session on a worker, poked with
  ``sessions.send`` (``native_id`` given) or started with
  ``work.stream.open`` and typed into (docs/design/sessions.md 3.2, 3.5).

``mode`` ``delegate`` succeeds once the hub has handed the work off (the
house agent then keeps working in the background, bounded by ``budget``);
``wait`` waits for the agent's verdict (``ok`` / ``failed`` plus text) up to
the step's timeout, and the verdict sets the outcome (no verdict in time is
``hang``).

``ask`` speaks a question on a phone with ``voice.speak`` (``reply: true``,
``wait: true``) and waits for the answer; the reply text lands in
``steps.<id>.reply``, and no answer is ``failure``.

Prompts are templated (``{{run.*}}``, ``{{vars.*}}``) but never get secret
values: a prompt may not contain ``{{secret:…}}``, and text sent to a session
has any masked ``{{secret:name}}`` stub defused so nothing substitutes it.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import re
import zlib
from fnmatch import fnmatchcase
from typing import Any

from . import guardrails
from .cron import parse_duration
from .executor import clip
from .kinds import VIAS, _check_worker, call_cap, resolve_worker, run_notify
from .steps import StepContext, StepResult, register_step_kind, step_kind

log = logging.getLogger("rook.hub.plugins.jobs.agent_kinds")

MODES = ("delegate", "wait")
SCOPES = ("job", "read", "none")
CONTEXT_ITEMS = ("run", "job", "vars")
SESSION_AGENTS = ("claude", "codex")
SESSION_KEYS = ("worker", "agent", "native_id", "cwd", "title", "task", "settle", "poll")
AGENT_KEYS = ("agent", "prompt", "context", "mode", "tools", "model", "max_tool_calls", "budget",
              "call_timeout")
DEFAULT_MODE = "delegate"
DEFAULT_BUDGET = "15m"
MAX_BUDGET = 6 * 3600
DEFAULT_CALL_TIMEOUT = 120.0
DEFAULT_POLL = 15.0
DEFAULT_SETTLE = 3.0
ASK_TIMEOUT = 120.0
PROMPT_MAX = 20000
CONTEXT_MAX = 12000
TEXT_MAX = 24000                      # sessions.send's limit
MARKER = "ROOK-VERDICT"
_MARK = re.compile(MARKER + r":\s*(ok|failed)\b[ \t:.-]*([^\r\n]*)", re.I)
_SECRET = re.compile(r"\{\{\s*secret:([A-Za-z0-9._-]+)\s*\}\}")
_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
VERDICT_ASK = (f"\n\nWhen you have finished, end your last message with one line: {MARKER}: "
               "followed by ok or failed, then a one-line summary. A Rook job is waiting for that line.")

#: Delegated house-agent work still running (kept so it is not collected).
_BACKGROUND: set[asyncio.Task] = set()


# -- shared --------------------------------------------------------------------------

def _is_abs(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", path))


def _dur(value: Any, name: str, errs: list, maximum: float | None = None) -> None:
    try:
        d = parse_duration(value)
    except ValueError as e:
        errs.append(f"{name}: {e}")
        return
    if d <= 0:
        errs.append(f"{name}: must be more than zero")
    elif maximum is not None and d > maximum:
        errs.append(f"{name}: at most {int(maximum)}s")


def defuse(text: str) -> str:
    """``{{secret:name}}`` -> ``[secret:name]`` so a later render never
    substitutes a value into text meant for an agent."""
    return _SECRET.sub(lambda m: f"[secret:{m.group(1)}]", text)


def _sub(ctx: StepContext, **changes) -> StepContext:
    return dataclasses.replace(ctx, **changes)


def _record_view(rec: dict | None) -> dict:
    if not rec:
        return {"state": "not run"}
    keep = ("state", "exit_code", "output", "error", "reply", "verdict", "attempts", "worker", "runs")
    return {k: rec[k] for k in keep if rec.get(k) is not None}


def build_context(ctx: StepContext, items: list | None) -> dict:
    """The run data an agent may see, masked: ``run`` (every other step's
    record so far), ``steps.<id>`` (one step), ``job`` (the definition, which
    holds placeholders, never values) and ``vars``."""
    out: dict = {}
    for item in items if items is not None else ["run"]:
        if item == "run":
            out["run"] = {"id": ctx.run.get("id"), "trigger": ctx.run.get("trigger"),
                          "missed": bool(ctx.run.get("missed")),
                          "steps": {sid: _record_view(r) for sid, r in ctx.records.items()
                                    if sid != ctx.step_id}}
        elif item == "job":
            out["job"] = {k: ctx.job.get(k) for k in ("id", "name", "description", "entry", "steps",
                                                      "triggers", "vars")}
        elif item == "vars":
            out["vars"] = ctx.template_scope()["vars"]
        elif isinstance(item, str) and item.startswith("steps."):
            sid = item[len("steps."):]
            out.setdefault("steps", {})[sid] = _record_view(ctx.records.get(sid))
    return ctx.runtime.mask(out, ctx.used)


def agent_identity(identity: Any, agent: str) -> Any:
    """The run's identity with ``agent`` outermost in the on-behalf-of chain
    (``agent:home`` -> ``job:<id>`` -> the job identity). Policy still sees
    the job identity as the principal, so the agent can do no more than the
    job could."""
    p = identity.principal
    principal = dataclasses.replace(p, via=(agent, *tuple(p.via or ())))
    return dataclasses.replace(identity, display=f"{agent}/{identity.display}", principal=principal)


def _decode(enc: str, data: str) -> str:
    if not data:
        return ""
    if enc == "t":
        raw = data.encode("utf-8")
    elif enc == "b":
        raw = base64.b64decode(data)
    elif enc == "z":
        d = zlib.decompressobj()
        raw = d.decompress(base64.b64decode(data), 4 * 1024 * 1024)
    else:
        raise ValueError(f"unknown terminal encoding {enc!r}")
    return raw.decode("utf-8", "replace")


def find_verdict(text: str) -> tuple[str, str] | None:
    """The last ``ROOK-VERDICT: ok|failed <summary>`` in ``text`` (ANSI
    codes stripped)."""
    m = None
    for m in _MARK.finditer(_ANSI.sub("", text or "")):
        pass
    return (m.group(1).lower(), m.group(2).strip()) if m else None


# -- agent: validation -----------------------------------------------------------------

def check_agent_spec(spec: Any) -> list:
    if spec in (None, "home"):
        return []
    if isinstance(spec, str):
        return [f'agent must be "home" or {{"session": {{...}}}}, not {spec!r}']
    if not isinstance(spec, dict) or set(spec) != {"session"} or not isinstance(spec["session"], dict):
        return ['agent must be "home" or {"session": {"worker": ..., "agent": "claude", ...}}']
    s = spec["session"]
    errs = [f"agent.session: unknown key {k!r} (use {', '.join(SESSION_KEYS)})" for k in s
            if k not in SESSION_KEYS]
    if "worker" not in s:
        errs.append("agent.session.worker is required (a worker name or id, or a filter)")
    else:
        errs += [f"agent.session.{e}" for e in _check_worker(s["worker"])]
    if s.get("agent", "claude") not in SESSION_AGENTS:
        errs.append(f"agent.session.agent: one of {', '.join(SESSION_AGENTS)}")
    nid = s.get("native_id")
    if nid is not None and (not isinstance(nid, str) or not nid.strip()):
        errs.append("agent.session.native_id: the session's id (a string)")
    if not nid:
        cwd = s.get("cwd")
        if not isinstance(cwd, str) or not _is_abs(cwd):
            errs.append("agent.session: give native_id (poke a session) or an absolute cwd (start one)")
    for k in ("title", "task"):
        if k in s and not isinstance(s[k], str):
            errs.append(f"agent.session.{k}: a string")
    for k in ("settle", "poll"):
        if k in s:
            _dur(s[k], f"agent.session.{k}", errs, 3600)
    return errs


def _check_context(ctx: Any) -> list:
    if not isinstance(ctx, list):
        return ['context: a list of "run", "job", "vars" or "steps.<id>"']
    bad = [c for c in ctx if not (c in CONTEXT_ITEMS or (isinstance(c, str) and c.startswith("steps.")
                                                         and len(c) > len("steps.")))]
    return [f"context: {c!r} is not one of run, job, vars, steps.<id>" for c in bad]


def check_agent(step: dict) -> list:
    errs = []
    prompt = step.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errs.append("prompt is required")
    elif len(prompt) > PROMPT_MAX:
        errs.append(f"prompt: at most {PROMPT_MAX} characters")
    elif _SECRET.search(prompt):
        errs.append("prompt: never put {{secret:…}} in a prompt; an agent passes the placeholder in "
                    "its own tool calls")
    errs += check_agent_spec(step.get("agent"))
    if "context" in step:
        errs += _check_context(step["context"])
    if step.get("mode", DEFAULT_MODE) not in MODES:
        errs.append(f"mode: one of {', '.join(MODES)}")
    tools = step.get("tools", "job")
    if not (tools in SCOPES or (isinstance(tools, list) and tools
                                and all(isinstance(t, str) and t.strip() for t in tools))):
        errs.append('tools: "job" (default), "read", "none" or a list of cap patterns such as "task.*"')
    if step.get("model") is not None and not isinstance(step.get("model"), str):
        errs.append("model: a model id (string) or null")
    mtc = step.get("max_tool_calls")
    if mtc is not None and (not isinstance(mtc, int) or isinstance(mtc, bool) or not 1 <= mtc <= 100):
        errs.append("max_tool_calls: 1-100")
    if "budget" in step:
        _dur(step["budget"], "budget", errs, MAX_BUDGET)
    if "call_timeout" in step:
        _dur(step["call_timeout"], "call_timeout", errs, 3600)
    return errs


def resolve_agent(ctx: StepContext, step: dict) -> Any:
    """The step's agent, else the hub's ``job.default_agent`` ("home", or a
    session spec as JSON). Raises ValueError for an unusable setting."""
    spec = step.get("agent")
    if spec in (None, ""):
        raw = ctx.settings("default_agent")
        raw = raw.strip() if isinstance(raw, str) else raw
        spec = raw or "home"
        if isinstance(spec, str) and spec.startswith("{"):
            try:
                spec = json.loads(spec)
            except ValueError:
                raise ValueError("the hub setting job.default_agent is not valid JSON") from None
        errs = check_agent_spec(spec)
        if errs:
            raise ValueError(f"the hub setting job.default_agent: {errs[0]}")
    return spec


# -- the house agent's backend ---------------------------------------------------------

class JobAgentBackend:
    """Carries out the house agent's tool calls for one job step, on behalf
    of the job (see the module docstring). Every method returns masked,
    JSON-able data and reports refusals instead of raising."""

    def __init__(self, ctx: StepContext, step: dict, agent: str, budget: float) -> None:
        self.ctx = ctx
        self.step = step
        self.rt = ctx.runtime
        self.agent = agent
        self.identity = agent_identity(ctx.identity, agent)
        self.scope = step.get("tools", "job")
        self.call_timeout = parse_duration(step.get("call_timeout") or DEFAULT_CALL_TIMEOUT)
        self.deadline = asyncio.get_running_loop().time() + budget
        self.actx = _sub(ctx, identity=self.identity, step_id=f"{ctx.step_id}/{agent}")

    # -- limits ------------------------------------------------------------
    def tool_names(self) -> list[str]:
        base = ["job_run", "notify_bake", "ask_bake"]
        return base if self.scope == "none" else ["rook_workers", "rook_call", "rook_tool", *base]

    def _left(self, cap: float) -> float:
        return max(1.0, min(cap, self.deadline - asyncio.get_running_loop().time()))

    def scope_refusal(self, cap: str, declared: Any = None) -> str:
        s = self.scope
        if s == "job":
            return ""
        if s == "none":
            return "this step's tools scope is none"
        if s == "read":
            from ....core.authz import effective_tier
            tier = effective_tier(cap, declared)
            return "" if tier == "read" else f"{cap} is a {tier} cap; this step's tools scope is read"
        if any(fnmatchcase(cap, pat) for pat in s):
            return ""
        return f"{cap} is outside this step's tools scope ({', '.join(s)})"

    def guard(self, synth: dict) -> str:
        """The job's guardrails for one agent action, as a pseudo step."""
        try:
            v = guardrails.check_step(self.ctx.job, {"timeout": self.step.get("timeout"), **synth},
                                      self.identity)
        except Exception as e:  # noqa: BLE001 - when the check fails, refuse
            log.exception("jobs: guardrail check failed for an agent call")
            return f"guardrail check failed: {type(e).__name__}"
        if v.allow:
            return ""
        return (v.reason or "blocked by a guardrail") + (f" (rule {v.rule})" if v.rule else "")

    def _refuse(self, cap: str, worker: str, args: dict, why: str) -> dict:
        self.rt.journal(cap, worker, self.identity, args, {"ok": False, "denied": why, "error": why})
        return {"ok": False, "state": "blocked", "error": why}

    def _policy(self, cap: str) -> str:
        """The band's permission policy for an in-process hub cap (the band
        client applies it to every other call)."""
        node = getattr(self.rt, "node", None)
        authz = getattr(getattr(node, "client", None), "authz", None)
        if authz is None:
            return ""
        try:
            d = authz.check(cap, getattr(node, "worker_id", None), node.entry(),
                            identity=self.identity.display, principal=self.identity.principal, local=True)
        except Exception:  # noqa: BLE001
            log.exception("jobs: policy check failed for %s", cap)
            return f"policy check failed for {cap}"
        return f"denied by policy ({d.rule})" if d.denied else ""

    def _out(self, res: StepResult) -> dict:
        out: dict = {"ok": res.ok, "state": res.state}
        if res.output is not None:
            out["result"] = res.output
        if res.error:
            out["error"] = res.error
        if res.exit_code is not None:
            out["exit_code"] = res.exit_code
        if res.extra.get("worker"):
            out["worker"] = res.extra["worker"]
        if res.extra.get("reply") is not None:
            out["reply"] = res.extra["reply"]
        return self.rt.mask(out, self.ctx.used)

    async def _bounded(self, coro, seconds: float) -> StepResult:
        try:
            return await asyncio.wait_for(coro, seconds)
        except asyncio.TimeoutError:
            return StepResult("hang", error=f"no result within {seconds:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - goes back to the model
            return StepResult("failure", error=f"{type(e).__name__}: {e}"[:300])

    # -- tools -------------------------------------------------------------
    async def workers(self) -> Any:
        out = []
        for wid, e in sorted(self.rt.roster().items(), key=lambda kv: str(kv[1].get("name") or kv[0])):
            tiers = e.get("tiers") or {}
            caps = [c for c in e.get("caps") or [] if not self.scope_refusal(c, tiers.get(c))]
            name = "rook" if wid == self.rt.hub_id else str(e.get("name") or wid[:8])
            out.append({"name": name, "os": (e.get("facts") or {}).get("os"), "caps": caps[:300]})
        return {"workers": out}

    async def call(self, worker: str, cap: str, args: dict) -> Any:
        if not worker or not cap:
            return {"ok": False, "error": "rook_call needs worker and cap"}
        declared = None
        try:
            found = resolve_worker(self.rt.roster(), worker, cap, self.rt.hub_id)
            if found:
                declared = (self.rt.roster().get(found[0]) or {}).get("tiers", {}).get(cap)
        except ValueError:
            pass
        why = self.scope_refusal(cap, declared) or self.guard(
            {"kind": "cap", "worker": worker, "cap": cap, "args": args})
        if why:
            return self._refuse(cap, worker, args, why)
        secs = self._left(self.call_timeout)
        res = await self._bounded(call_cap(_sub(self.actx, timeout=secs), cap, args, worker), secs)
        return self._out(res)

    async def tool(self, name: str, args: dict) -> Any:
        if not name.startswith("rook_"):
            return {"ok": False, "error": "tool must name a hub MCP tool such as rook_task"}
        why = ("this step's tools scope is none" if self.scope == "none"
               else self.guard({"kind": "tool", "tool": name, "args": args}))
        if why:
            return self._refuse(name, "rook", args, why)
        secs = self._left(self.call_timeout)
        try:
            reply = await asyncio.wait_for(self._hub_tool(name, args), secs)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": f"no reply within {secs:.0f}s"}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if isinstance(reply, str):
            try:
                reply = json.loads(reply)
            except ValueError:
                pass
        reply = self.rt.mask(reply, self.ctx.used)
        self.rt.journal(name, "rook", self.identity, args, reply if isinstance(reply, dict)
                        else {"ok": True, "result": clip(reply)})
        return reply

    async def _hub_tool(self, name: str, args: dict) -> Any:
        """A hub plugin's MCP tool, with every cap it calls checked against
        the scope, the guardrails and the policy, run as the agent identity."""
        node = getattr(self.rt, "node", None)
        host = getattr(node, "host", None)
        if host is None:                  # a runtime without hub plugins (tests)
            return await self.rt.tool(name, args, self.identity)
        from ...authz import current_principal

        async def invoke(cap: str, cargs: dict) -> Any:
            why = (self.scope_refusal(cap) or self.guard(
                {"kind": "cap", "worker": "rook", "cap": cap, "args": cargs}) or self._policy(cap))
            if why:
                self._refuse(cap, "rook", cargs, why)
                raise PermissionError(why)
            tok = current_principal.set(self.identity.principal)
            try:
                return await node.invoke(cap, cargs, self.identity.display)
            finally:
                current_principal.reset(tok)

        for plugin in host.plugins:
            hook = getattr(plugin, "mcp_tools", None)
            if not callable(hook):
                continue
            for fn in hook(invoke) or []:
                if getattr(fn, "__name__", "") == name:
                    return await fn(**args)
        raise LookupError(f"no hub tool named {name!r}")

    async def run_state(self, step: str | None) -> Any:
        return build_context(self.ctx, [f"steps.{step}"] if step else ["run"])

    async def notify(self, text: str, via: str, title: str) -> Any:
        if via not in VIAS or not text.strip():
            return {"ok": False, "error": f"notify_bake needs text and via one of {', '.join(VIAS)}"}
        synth = {"kind": "notify", "via": via, "text": text, **({"title": title} if title else {})}
        why = self.guard(synth)
        if why:
            return self._refuse(VIAS[via], "", synth, why)
        secs = self._left(self.call_timeout)
        return self._out(await self._bounded(run_notify(_sub(self.actx, timeout=secs), synth), secs))

    async def ask(self, question: str) -> Any:
        if not question.strip():
            return {"ok": False, "error": "ask_bake needs a question"}
        synth = {"kind": "ask", "text": question}
        why = self.guard(synth)
        if why:
            return self._refuse("voice.speak", "", synth, why)
        secs = self._left(ASK_TIMEOUT)
        return self._out(await self._bounded(run_ask(_sub(self.actx, timeout=secs), synth), secs))


def home_agent(ctx: StepContext) -> Any:
    """The ``home`` plugin, or None (tests set ``runtime.home_agent``)."""
    found = getattr(ctx.runtime, "home_agent", None)
    if found is not None:
        return found
    node = getattr(ctx.runtime, "node", None)
    plugin = getattr(node, "plugin", None)
    return plugin("home") if callable(plugin) else None


# -- agent: running ----------------------------------------------------------------------

async def run_agent(ctx: StepContext, step: dict) -> StepResult:
    try:
        spec = resolve_agent(ctx, step)
    except ValueError as e:
        return StepResult("failure", error=str(e))
    mode = step.get("mode", DEFAULT_MODE)
    prompt = ctx.template(step["prompt"])
    context = build_context(ctx, step.get("context", ["run"]))
    if spec in (None, "home"):
        return await _run_home(ctx, step, mode, prompt, context)
    return await _run_session(ctx, step, spec["session"], mode, prompt, context)


async def _run_home(ctx: StepContext, step: dict, mode: str, prompt: str, context: dict) -> StepResult:
    home = home_agent(ctx)
    if home is None:
        return StepResult("failure", error="the house agent (home plugin) is not loaded on this hub")
    why = home.job_unready()
    if why:
        return StepResult("failure", error=why)
    budget = ctx.timeout if mode == "wait" else parse_duration(step.get("budget") or DEFAULT_BUDGET)
    backend = JobAgentBackend(ctx, step, home.identity, budget)
    work = home.work_job_step(prompt, context, backend, model=step.get("model"),
                              max_calls=step.get("max_tool_calls"), job=str(ctx.job.get("id") or ""),
                              run=str(ctx.run.get("id") or ""), step=ctx.step_id)
    if mode == "delegate":
        task = asyncio.get_running_loop().create_task(_background(work, budget, ctx))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return StepResult("success", output={"agent": home.identity, "mode": "delegate",
                                             "budget_s": round(budget)},
                          extra={"agent": home.identity})
    try:
        out = await work
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - HomeError / LLMError: the model could not be used
        return StepResult("failure", error=str(e)[:500], extra={"agent": home.identity})
    verdict = out.get("verdict")
    output = {"agent": home.identity, "verdict": verdict, "text": out.get("text"),
              "calls": out.get("calls"), "tools": out.get("tools_used")}
    if verdict is None:
        return StepResult("failure", output=output, error="the agent gave no verdict",
                          text=out.get("text"), extra={"agent": home.identity})
    return StepResult("success" if verdict == "ok" else "failure", output=output,
                      error=None if verdict == "ok" else (out.get("text") or "the agent said failed")[:500],
                      text=out.get("text") or "", extra={"agent": home.identity, "verdict": verdict})


async def _background(work, budget: float, ctx: StepContext) -> None:
    try:
        out = await asyncio.wait_for(work, budget)
        log.info("jobs: delegated agent work for %s/%s finished: %s", ctx.run.get("id"), ctx.step_id,
                 out.get("verdict"))
    except asyncio.TimeoutError:
        log.warning("jobs: delegated agent work for %s/%s ran out of budget (%ss)", ctx.run.get("id"),
                    ctx.step_id, round(budget))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("jobs: delegated agent work for %s/%s failed", ctx.run.get("id"), ctx.step_id)


async def cancel_background() -> None:
    """Stop delegated work (hub shutdown)."""
    tasks = list(_BACKGROUND)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def background() -> list[asyncio.Task]:
    return list(_BACKGROUND)


def _session_text(ctx: StepContext, prompt: str, context: dict, mode: str) -> str:
    body = prompt.strip()
    if context:
        ctx_text = json.dumps(context, ensure_ascii=False, default=str)
        if len(ctx_text) > CONTEXT_MAX:
            ctx_text = ctx_text[:CONTEXT_MAX] + "…[truncated]"
        body += f"\n\nRun context from Rook job {ctx.job.get('name')} (masked):\n{ctx_text}"
    if mode == "wait":
        body += VERDICT_ASK
    return defuse(body)[:TEXT_MAX]


def _verdict_result(found: tuple[str, str], session: dict) -> StepResult:
    verdict, text = found
    out = {"session": session, "verdict": verdict, "text": text}
    return StepResult("success" if verdict == "ok" else "failure", output=out, text=text,
                      error=None if verdict == "ok" else (text or "the agent said failed"),
                      extra={"verdict": verdict})


async def _run_session(ctx: StepContext, step: dict, s: dict, mode: str, prompt: str,
                       context: dict) -> StepResult:
    agent = s.get("agent", "claude")
    worker = s["worker"]
    text = _session_text(ctx, prompt, context, mode)
    poll = parse_duration(s.get("poll") or DEFAULT_POLL)
    short = _sub(ctx, timeout=min(ctx.timeout, 60.0))
    if s.get("native_id"):
        nid = s["native_id"]
        base = 0
        if mode == "wait":
            res = await call_cap(short, "sessions.follow", {"agent": agent, "native_id": nid, "tail": 5}, worker)
            if not res.ok:
                return res
            base = 1 + max([m.get("index", -1) for m in (res.output or {}).get("messages") or []
                            if isinstance(m, dict)] or [-1])
        res = await call_cap(ctx, "sessions.send", {"agent": agent, "native_id": nid, "text": text}, worker)
        if not res.ok:
            return res
        session = {"worker": res.extra.get("worker"), "agent": agent, "native_id": nid,
                   "delivery": (res.output or {}).get("delivery")}
        if mode == "delegate":
            return StepResult("success", output={"session": session, "mode": "delegate"},
                              extra={"worker": session["worker"]})
        while True:
            await ctx.runtime.sleep(poll)
            res = await call_cap(short, "sessions.follow", {"agent": agent, "native_id": nid, "tail": 20}, worker)
            if not res.ok:
                continue                   # the host may be busy; the step timeout bounds this
            said = "\n".join(str(m.get("text") or "") for m in (res.output or {}).get("messages") or []
                             if isinstance(m, dict) and m.get("role") == "assistant"
                             and int(m.get("index", -1)) >= base)
            found = find_verdict(said)
            if found:
                return _verdict_result(found, session)
    # Start a new session in a Rook terminal, then type the prompt into it.
    args = {"harness": agent, "cwd": s["cwd"], "title": s.get("title") or f"job {ctx.job.get('name')}"}
    if step.get("model"):
        args["model"] = step["model"]
    if s.get("task"):
        args["task"] = s["task"]
    res = await call_cap(ctx, "work.stream.open", args, worker)
    if not res.ok:
        return res
    tid = (res.output or {}).get("id")
    if not tid:
        return StepResult("failure", output=res.output, error="work.stream.open returned no terminal id")
    session = {"worker": res.extra.get("worker"), "agent": agent, "terminal": tid}
    pinned = session["worker"] or worker      # the same host from here on
    first = await call_cap(short, "work.stream.read", {"id": tid, "cursor": 0, "wait": 10}, pinned)
    cursor = int((first.output or {}).get("next") or 0) if first.ok else 0
    await ctx.runtime.sleep(parse_duration(s.get("settle") or DEFAULT_SETTLE))
    body = text.rstrip("\r\n")
    if "\n" in body:
        body = "\x1b[200~" + body + "\x1b[201~"   # bracketed paste keeps newlines as text
    res = await call_cap(short, "work.stream.write", {"id": tid, "data": body + "\r"}, pinned)
    if not res.ok:
        return StepResult("failure", output={"session": session}, error=f"typing the prompt failed: {res.error}")
    if mode == "delegate":
        return StepResult("success", output={"session": session, "mode": "delegate"},
                          extra={"worker": session["worker"]})
    seen = ""
    while True:
        res = await call_cap(short, "work.stream.read", {"id": tid, "cursor": cursor, "wait": 20}, pinned)
        if not res.ok:
            await ctx.runtime.sleep(poll)
            continue
        out = res.output or {}
        seen = (seen + _decode(str(out.get("enc") or "t"), str(out.get("data") or "")))[-20000:]
        cursor = int(out.get("next") or cursor)
        found = find_verdict(seen)
        if found:
            return _verdict_result(found, session)
        if out.get("eof") or out.get("running") is False:
            return StepResult("failure", output={"session": session},
                              error="the session ended without a verdict")


# -- ask ---------------------------------------------------------------------------------

ASK_KEYS = ("via", "text", "worker", "reply_timeout")


def check_ask(step: dict) -> list:
    errs = []
    if step.get("via", "voice") != "voice":
        errs.append('via: "voice" (the phone speaks the question; Bake answers out loud or types '
                    "in its Reply notification)")
    if not isinstance(step.get("text"), str) or not step["text"].strip():
        errs.append("text is required (the question)")
    if "worker" in step:
        errs += _check_worker(step["worker"])
    rt = step.get("reply_timeout")
    if rt is not None and (not isinstance(rt, int) or isinstance(rt, bool) or not 2 <= rt <= 30):
        errs.append("reply_timeout: 2-30 seconds of listening after the question")
    return errs


async def run_ask(ctx: StepContext, step: dict) -> StepResult:
    """``voice.speak`` with ``reply`` and ``wait`` on the step's worker, else
    ``job.notify_worker``, else the first live worker with the cap."""
    margin = min(5.0, ctx.timeout / 10)
    args = {"text": step["text"], "reply": True, "wait": True,
            "timeout": int(max(5, min(600, ctx.timeout - 2 * margin))),
            "reply_timeout": int(step.get("reply_timeout") or 20)}
    spec = step.get("worker") or ctx.settings("notify_worker") or {"any_with_cap": True}
    res = await call_cap(ctx, "voice.speak", args, spec)
    if not res.ok:
        return res
    out = res.output if isinstance(res.output, dict) else {}
    reply = out.get("reply") if isinstance(out.get("reply"), dict) else None
    text = str((reply or {}).get("text") or "").strip()
    worker = res.extra.get("worker")
    if not text:
        state = out.get("reply_state") or "none"
        return StepResult("failure", output={"reply_state": state, "worker": worker},
                          error=f"no answer (reply_state {state})", extra={"worker": worker})
    return StepResult("success", output={"reply": text, "via": reply.get("via"), "worker": worker},
                      text=text, extra={"reply": text, "worker": worker})


# -- registration ----------------------------------------------------------------------------

AGENT_SCHEMA = {
    "agent": {"oneOf": [
        {"const": "home", "description": "the house agent (default: the hub setting job.default_agent)"},
        {"type": "object", "required": ["session"], "properties": {"session": {
            "type": "object", "required": ["worker"], "properties": {
                "worker": {"$ref": "#/$defs/worker"}, "agent": {"enum": list(SESSION_AGENTS)},
                "native_id": {"type": "string", "description": "poke this session (sessions.send)"},
                "cwd": {"type": "string", "description": "start a new session here (no native_id)"},
                "title": {"type": "string"}, "task": {"type": "string"},
                "settle": {"$ref": "#/$defs/duration"}, "poll": {"$ref": "#/$defs/duration"}}}}}]},
    "prompt": {"type": "string"},
    "context": {"type": "array", "items": {"type": "string"}, "default": ["run"],
                "description": "run | job | vars | steps.<id>"},
    "mode": {"enum": list(MODES), "default": DEFAULT_MODE},
    "tools": {"oneOf": [{"enum": list(SCOPES)}, {"type": "array", "items": {"type": "string"}}],
              "default": "job"},
    "model": {"type": ["string", "null"]},
    "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 100},
    "budget": {"$ref": "#/$defs/duration", "default": DEFAULT_BUDGET,
               "description": "how long delegated house-agent work may run"},
    "call_timeout": {"$ref": "#/$defs/duration", "default": "2m"},
}
ASK_SCHEMA = {
    "via": {"const": "voice", "default": "voice"}, "text": {"type": "string"},
    "worker": {"$ref": "#/$defs/worker"},
    "reply_timeout": {"type": "integer", "minimum": 2, "maximum": 30, "default": 20},
}


def register() -> None:
    if step_kind("agent") is None:
        register_step_kind("agent", run_agent, validate=check_agent, schema=AGENT_SCHEMA)
    if step_kind("ask") is None:
        register_step_kind("ask", run_ask, validate=check_ask, schema=ASK_SCHEMA)


register()
