"""Step kinds: the registry, the step context and the shared result rules.

A step kind is a handler ``async (ctx: StepContext, step: dict) -> StepResult``
registered with :func:`register_step_kind`, optionally with a validator
(``(step) -> [error, ...]``) and a JSON-schema fragment for its own fields.
The executor never special-cases a kind except ``join``, which it settles
itself. The built-in kinds live in :mod:`.kinds`; ``agent`` and ``ask``
(docs/design/jobs.md 6) are reserved for a later workstream, which registers
them here the same way::

    from rook.hub.plugins.jobs.steps import register_step_kind
    register_step_kind("ask", run_ask, validate=check_ask, schema={...})

Handlers report the raw outcome; :func:`apply_success_rules` then applies the
step's ``success`` block (exit codes, ``match`` / ``not_match``), the
executor applies ``timeout`` (a timeout is ``hang``) and ``retry``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Every kind the job contract names. A kind in this list but not registered
#: is "not available on this hub yet" rather than unknown.
CONTRACT_KINDS = ("cap", "fanout", "tool", "agent", "wait", "notify", "ask", "join", "noop")
OUTCOMES = ("success", "failure", "hang")
#: Step results; ``blocked`` comes from the guardrail check, never retried.
STATES = ("success", "failure", "hang", "blocked")


@dataclass
class StepResult:
    state: str                       # success | failure | hang | blocked
    output: Any = None               # what the run record keeps (masked, truncated)
    error: str | None = None
    exit_code: int | None = None
    text: str | None = None          # what match / not_match look at (default: output as text)
    extra: dict = field(default_factory=dict)  # kind-specific fields kept on the record (e.g. reply)
    checked: bool = False            # the handler applied the success rules itself (fanout)

    @property
    def ok(self) -> bool:
        return self.state == "success"


Handler = Callable[["StepContext", dict], Awaitable[StepResult]]
Validator = Callable[[dict], list]


@dataclass(frozen=True)
class StepKind:
    name: str
    run: Handler
    validate: Validator | None = None
    schema: dict | None = None       # JSON-schema properties for the kind's own fields


_KINDS: dict[str, StepKind] = {}


def register_step_kind(name: str, run: Handler, *, validate: Validator | None = None,
                       schema: dict | None = None, replace: bool = False) -> StepKind:
    """Add a step kind. ``replace=True`` swaps an existing one (tests)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name or ""):
        raise ValueError(f"bad step kind name {name!r}")
    if name in _KINDS and not replace:
        raise ValueError(f"step kind {name!r} is already registered")
    kind = StepKind(name, run, validate, schema)
    _KINDS[name] = kind
    return kind


def unregister_step_kind(name: str) -> None:
    _KINDS.pop(name, None)


def step_kind(name: str) -> StepKind | None:
    return _KINDS.get(name)


def step_kinds() -> list[str]:
    return sorted(_KINDS)


# -- context -------------------------------------------------------------------

_TEMPLATE = re.compile(r"\{\{\s*(run|job|vars)\.([A-Za-z0-9_.-]+)\s*\}\}")


@dataclass
class StepContext:
    """What a handler gets. ``runtime`` is the hub transport
    (:class:`.runtime.Runtime`); ``render`` resolves ``{{run.*}}``,
    ``{{job.*}}``, ``{{vars.*}}`` and then ``{{secret:name}}`` placeholders at
    execution time and remembers the secret values used, so the executor can
    mask them out of whatever the step returns."""

    job: dict
    run: dict
    step_id: str
    identity: Any                    # .identity.RunIdentity
    runtime: Any
    settings: Callable[[str], Any] = lambda _k: None
    timeout: float = 300.0
    attempt: int = 1
    records: dict = field(default_factory=dict)   # step id -> record so far (read-only use)
    used: dict = field(default_factory=dict)      # secret name -> value used by this step

    def template_scope(self) -> dict:
        from .cron import iso
        run = {k: self.run.get(k) for k in ("id", "missed", "trigger")}
        started = self.run.get("started")
        run["started"] = iso(started) if isinstance(started, (int, float)) else started
        return {"run": run,
                "job": {"id": self.job.get("id"), "name": self.job.get("name")},
                "vars": {**(self.job.get("vars") or {}), **(self.run.get("vars") or {})}}

    def template(self, obj: Any) -> Any:
        scope = self.template_scope()

        def sub(m: "re.Match") -> str:
            cur: Any = scope.get(m.group(1))
            for part in m.group(2).split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return m.group(0)
            return cur if isinstance(cur, str) else json.dumps(cur)

        def walk(o: Any) -> Any:
            if isinstance(o, str):
                return _TEMPLATE.sub(sub, o)
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(v) for v in o]
            return o
        return walk(obj)

    def render(self, obj: Any, via: str = "") -> Any:
        """Templates, then secrets. Raises KeyError for an unknown secret."""
        out = self.template(obj)
        out, used = self.runtime.substitute(out, self.identity.display,
                                            via or f"job {self.job.get('name')} step {self.step_id}")
        self.used.update(used or {})
        return out


# -- success rules ---------------------------------------------------------------

def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("stdout", "text", "output"):
            if isinstance(value.get(key), str):
                return value[key]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def exit_code_of(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("exit_code", "returncode", "rc", "code"):
            v = value.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


def apply_success_rules(step: dict, res: StepResult) -> StepResult:
    """A success the handler reported can still fail on the step's exit
    codes (default ``[0]`` when the result carries one) or its regexes."""
    if res.state != "success" or res.checked:
        return res
    rules = step.get("success") or {}
    code = res.exit_code
    if code is not None and code not in (rules.get("exit_codes") or [0]):
        res.state, res.error = "failure", f"exit code {code}"
        return res
    text = res.text if res.text is not None else as_text(res.output)
    if rules.get("match") and not re.search(rules["match"], text):
        res.state, res.error = "failure", f"output does not match {rules['match']!r}"
    elif rules.get("not_match") and re.search(rules["not_match"], text):
        res.state, res.error = "failure", f"output matches {rules['not_match']!r}"
    return res
