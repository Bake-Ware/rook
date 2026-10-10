"""The job document: defaults, validation on save, and its JSON schema.

docs/design/jobs.md 4-5 is the contract. :func:`normalize` fills defaults
(every step gets a timeout, ``on`` lists, etc.) and :func:`validate` returns
every problem at once as ``"<path>: <message>"`` strings, so an editor can
show them inline. A graph is valid when every step is reachable from
``entry``, no branch names a missing step, every kind is registered, and every
join has incoming branches (and is not inside a loop: a join settles once per
run).
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .kinds import WORKER_SCHEMA  # importing kinds registers the built-in step kinds
from .cron import Cron, parse_duration, parse_instant, valid_zone
from .expr import compile_expr, step_refs
from .identity import MODES, SUPPORTED_MODES
from .steps import CONTRACT_KINDS, OUTCOMES, step_kind, step_kinds

STEP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
DEFAULT_TIMEOUT = "5m"
MAX_TIMEOUT = 7 * 86400
MAX_RETRIES = 10
MAX_STEPS = 200
TRIGGER_KINDS = ("cron", "at", "after", "manual")
AFTER_ON = ("success", "finish", "failure")
OVERLAP_MODES = ("queue", "skip", "parallel")
MISSED_MODES = ("run_once", "skip", "all")
TOP_KEYS = ("id", "name", "description", "enabled", "owner", "identity", "triggers", "overlap",
            "missed", "retention_days", "access", "guardrails", "alerts", "entry", "steps", "vars")
STEP_KEYS = ("kind", "timeout", "on", "success", "retry", "allow_failure", "description")


class ValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
        self.errors = errors


def normalize(doc: dict) -> dict:
    """A copy of ``doc`` with defaults filled in. Never raises on bad
    shapes; :func:`validate` reports them."""
    job = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    job.setdefault("description", "")
    job.setdefault("enabled", True)
    job.setdefault("identity", {"mode": "creator"})
    job.setdefault("triggers", [{"kind": "manual"}])
    ov = job.setdefault("overlap", {})
    if isinstance(ov, dict):
        ov.setdefault("mode", "queue")
        ov.setdefault("max_queue", None)
    ms = job.setdefault("missed", {})
    if isinstance(ms, dict):
        ms.setdefault("mode", "run_once")
        ms.setdefault("grace", "10m")
    job.setdefault("retention_days", None)
    job.setdefault("access", {"read": "*", "edit": "*", "run": "*"})
    job.setdefault("guardrails", {"inherit": True, "allow": [], "deny": []})
    job.setdefault("alerts", {"on_failure": [], "on_success": []})
    job.setdefault("vars", {})
    steps = job.get("steps")
    if isinstance(steps, dict):
        for step in steps.values():
            if not isinstance(step, dict):
                continue
            step.setdefault("timeout", DEFAULT_TIMEOUT)
            on = step.get("on")
            if on is None:
                step["on"] = {}
            elif isinstance(on, dict):
                step["on"] = {k: ([v] if isinstance(v, str) else v) for k, v in on.items()}
            if isinstance(step.get("retry"), dict):
                step["retry"].setdefault("max", 0)
                step["retry"].setdefault("delay", "0s")
    return job


def _duration(value: Any, path: str, errs: list, *, positive: bool = False, maximum: float | None = None):
    try:
        d = parse_duration(value)
    except ValueError as e:
        errs.append(f"{path}: {e}")
        return None
    if positive and d <= 0:
        errs.append(f"{path}: must be more than zero")
    if maximum is not None and d > maximum:
        errs.append(f"{path}: at most {int(maximum)}s")
    return d


def _triggers(job: dict, errs: list, warns: list, now: float | None) -> None:
    trigs = job.get("triggers")
    if not isinstance(trigs, list):
        errs.append("triggers: must be a list")
        return
    for i, t in enumerate(trigs):
        p = f"triggers[{i}]"
        if not isinstance(t, dict) or t.get("kind") not in TRIGGER_KINDS:
            errs.append(f"{p}.kind: one of {', '.join(TRIGGER_KINDS)}")
            continue
        kind = t["kind"]
        if t.get("tz") is not None and not valid_zone(t["tz"]):
            errs.append(f"{p}.tz: unknown time zone {t['tz']!r}")
        if kind == "cron":
            try:
                c = Cron(t.get("expr", ""))
                if now is not None and c.next_fire(now, t.get("tz") if valid_zone(t.get("tz") or "")
                                                   else "UTC") is None:
                    errs.append(f"{p}.expr: never fires")
            except ValueError as e:
                errs.append(f"{p}.expr: {e}")
        elif kind == "at":
            try:
                when = parse_instant(t.get("when"))
                if now is not None and when <= now:
                    warns.append(f"{p}.when: in the past; it will not fire")
            except ValueError as e:
                errs.append(f"{p}.when: {e}")
        elif kind == "after":
            _duration(t.get("every"), f"{p}.every", errs, positive=True)
            if t.get("on", "success") not in AFTER_ON:
                errs.append(f"{p}.on: one of {', '.join(AFTER_ON)}")


def _policy_blocks(job: dict, errs: list) -> None:
    ov = job.get("overlap")
    if not isinstance(ov, dict) or ov.get("mode") not in OVERLAP_MODES:
        errs.append(f"overlap.mode: one of {', '.join(OVERLAP_MODES)}")
    else:
        mq = ov.get("max_queue")
        if mq is not None and (not isinstance(mq, int) or isinstance(mq, bool) or mq < 0):
            errs.append("overlap.max_queue: null (no limit) or a number >= 0")
    ms = job.get("missed")
    if not isinstance(ms, dict) or ms.get("mode") not in MISSED_MODES:
        errs.append(f"missed.mode: one of {', '.join(MISSED_MODES)}")
    else:
        _duration(ms.get("grace"), "missed.grace", errs)
    rd = job.get("retention_days")
    if rd is not None and (not isinstance(rd, int) or isinstance(rd, bool) or not 1 <= rd <= 3650):
        errs.append("retention_days: null (the hub setting) or 1-3650")
    ident = job.get("identity")
    if not isinstance(ident, dict) or ident.get("mode", "creator") not in MODES:
        errs.append(f"identity.mode: one of {', '.join(MODES)}")
    elif ident.get("mode", "creator") not in SUPPORTED_MODES:
        errs.append(f"identity.mode: {ident['mode']!r} is not available on this hub yet (creator only)")
    for key in ("access", "guardrails", "alerts", "vars"):
        if not isinstance(job.get(key), dict):
            errs.append(f"{key}: must be an object")
    alerts = job.get("alerts") if isinstance(job.get("alerts"), dict) else {}
    check = step_kind("notify").validate
    for key in ("on_failure", "on_success"):
        items = alerts.get(key, [])
        if not isinstance(items, list):
            errs.append(f"alerts.{key}: a list of notify specs ({{via, text}})")
            continue
        for i, a in enumerate(items):
            for e in (check(a) if isinstance(a, dict) else ["must be an object"]):
                errs.append(f"alerts.{key}[{i}]: {e}")


def _step(sid: str, step: Any, ids: set, errs: list) -> None:
    p = f"steps.{sid}"
    if not STEP_ID.match(sid):
        errs.append(f"{p}: step ids are letters, digits, _ and -, starting with a letter or _")
    if not isinstance(step, dict):
        errs.append(f"{p}: must be an object")
        return
    kind = step.get("kind")
    k = step_kind(kind) if isinstance(kind, str) else None
    if k is None:
        if kind in CONTRACT_KINDS:
            errs.append(f"{p}.kind: {kind!r} steps are not available on this hub yet")
        else:
            errs.append(f"{p}.kind: unknown kind {kind!r} (use {', '.join(step_kinds())})")
    _duration(step.get("timeout"), f"{p}.timeout", errs, positive=True, maximum=MAX_TIMEOUT)
    on = step.get("on")
    if not isinstance(on, dict):
        errs.append(f"{p}.on: an object of success/failure/hang -> [step ids]")
    else:
        for key, targets in on.items():
            if key not in OUTCOMES:
                errs.append(f"{p}.on.{key}: outcome keys are {', '.join(OUTCOMES)}")
                continue
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                errs.append(f"{p}.on.{key}: a list of step ids")
                continue
            for t in targets:
                if t not in ids:
                    errs.append(f"{p}.on.{key}: no step {t!r}")
    succ = step.get("success")
    if succ is not None:
        if not isinstance(succ, dict):
            errs.append(f"{p}.success: must be an object")
        else:
            codes = succ.get("exit_codes")
            if codes is not None and not (isinstance(codes, list) and all(
                    isinstance(c, int) and not isinstance(c, bool) for c in codes)):
                errs.append(f"{p}.success.exit_codes: a list of integers")
            for key in ("match", "not_match"):
                if succ.get(key) is not None:
                    try:
                        re.compile(succ[key])
                    except (re.error, TypeError) as e:
                        errs.append(f"{p}.success.{key}: bad regex ({e})")
    retry = step.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            errs.append(f"{p}.retry: must be an object")
        else:
            mx = retry.get("max", 0)
            if not isinstance(mx, int) or isinstance(mx, bool) or not 0 <= mx <= MAX_RETRIES:
                errs.append(f"{p}.retry.max: 0-{MAX_RETRIES}")
            _duration(retry.get("delay", "0s"), f"{p}.retry.delay", errs, maximum=86400)
    if "allow_failure" in step and not isinstance(step["allow_failure"], bool):
        errs.append(f"{p}.allow_failure: true or false")
    if k is not None and k.validate is not None:
        errs.extend(f"{p}: {e}" for e in k.validate(step))
    if kind == "wait" and "for" in step:
        try:
            if parse_duration(step["for"]) >= parse_duration(step.get("timeout")):
                errs.append(f"{p}.timeout: must be longer than the wait")
        except ValueError:
            pass


def edges(steps: dict) -> dict[str, set]:
    out: dict[str, set] = {sid: set() for sid in steps}
    for sid, step in steps.items():
        on = step.get("on") if isinstance(step, dict) else None
        for targets in (on or {}).values() if isinstance(on, dict) else ():
            for t in targets if isinstance(targets, list) else ():
                if t in out:
                    out[sid].add(t)
    return out


def incoming(steps: dict) -> dict[str, set]:
    out: dict[str, set] = {sid: set() for sid in steps}
    for src, dsts in edges(steps).items():
        for d in dsts:
            out[d].add(src)
    return out


def _reach(graph: dict[str, set], start: str) -> set:
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo.extend(graph.get(n, ()))
    return seen


def _graph(job: dict, errs: list) -> None:
    steps = job.get("steps")
    if not isinstance(steps, dict) or not steps:
        errs.append("steps: at least one step")
        return
    if len(steps) > MAX_STEPS:
        errs.append(f"steps: at most {MAX_STEPS}")
    ids = set(steps)
    for sid, step in steps.items():
        _step(sid, step, ids, errs)
    entry = job.get("entry")
    if entry not in ids:
        errs.append(f"entry: no step {entry!r}")
        return
    graph = edges(steps)
    reached = _reach(graph, entry)
    for sid in sorted(ids - reached):
        errs.append(f"steps.{sid}: not reachable from entry {entry!r}")
    inc = incoming(steps)
    for sid, step in steps.items():
        if not isinstance(step, dict) or step.get("kind") != "join":
            continue
        if not inc[sid]:
            errs.append(f"steps.{sid}: a join needs incoming branches")
        if sid == entry:
            errs.append(f"steps.{sid}: the entry cannot be a join")
        if any(sid in _reach(graph, nxt) for nxt in graph[sid]):
            errs.append(f"steps.{sid}: a join cannot be inside a loop")
        cond = step.get("condition", "all")
        if isinstance(cond, str) and cond not in ("all", "any"):
            try:
                for ref in step_refs(compile_expr(cond)):
                    if ref not in ids:
                        errs.append(f"steps.{sid}.condition: no step {ref!r}")
            except ValueError:
                pass  # reported by the kind's validator
        if isinstance(cond, dict) and isinstance(cond.get("at_least"), int) \
                and cond["at_least"] > len(inc[sid]):
            errs.append(f"steps.{sid}.condition: at_least {cond['at_least']} but only "
                        f"{len(inc[sid])} incoming branches")


def validate(doc: dict, *, now: float | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a normalized job."""
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(doc, dict):
        return ["the job must be a JSON object"], []
    for key in doc:
        if key not in TOP_KEYS:
            errs.append(f"{key}: unknown field")
    if not isinstance(doc.get("name"), str) or not NAME.match(doc.get("name") or ""):
        errs.append("name: required; letters, digits, space . _ -, at most 80")
    if not isinstance(doc.get("description", ""), str):
        errs.append("description: must be a string")
    if not isinstance(doc.get("enabled", True), bool):
        errs.append("enabled: true or false")
    _triggers(doc, errs, warns, now)
    _policy_blocks(doc, errs)
    _graph(doc, errs)
    return errs, warns


def check(doc: dict, *, now: float | None = None) -> tuple[dict, list[str]]:
    """Normalize and validate; raises :class:`ValidationError`. Returns the
    normalized job and its warnings."""
    job = normalize(doc)
    errs, warns = validate(job, now=now)
    if errs:
        raise ValidationError(errs)
    return job, warns


# -- schema ------------------------------------------------------------------------

def schema() -> dict:
    """The job JSON schema (draft 2020-12), with each registered step kind's
    own fields. ``describe_schema`` returns it so agents can write jobs."""
    kinds = {}
    for name in step_kinds():
        k = step_kind(name)
        kinds[name] = {"type": "object", "properties": {"kind": {"const": name}, **(k.schema or {})}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Rook job",
        "type": "object",
        "required": ["name", "entry", "steps"],
        "properties": {
            "name": {"type": "string", "pattern": NAME.pattern},
            "description": {"type": "string"},
            "enabled": {"type": "boolean", "default": True},
            "identity": {"type": "object", "properties": {"mode": {"enum": list(SUPPORTED_MODES)}},
                         "default": {"mode": "creator"}},
            "triggers": {"type": "array", "default": [{"kind": "manual"}], "items": {"oneOf": [
                {"type": "object", "required": ["kind", "expr"], "properties": {
                    "kind": {"const": "cron"},
                    "expr": {"type": "string", "description": "5-field cron or @hourly/@daily/@weekly/@monthly"},
                    "tz": {"type": "string", "description": "IANA zone; default the hub's job.timezone"}}},
                {"type": "object", "required": ["kind", "when"], "properties": {
                    "kind": {"const": "at"}, "when": {"type": "string", "description": "ISO 8601 with offset"}}},
                {"type": "object", "required": ["kind", "every"], "properties": {
                    "kind": {"const": "after"}, "every": {"$ref": "#/$defs/duration"},
                    "on": {"enum": list(AFTER_ON), "default": "success"}}},
                {"type": "object", "required": ["kind"], "properties": {"kind": {"const": "manual"}}}]}},
            "overlap": {"type": "object", "properties": {
                "mode": {"enum": list(OVERLAP_MODES), "default": "queue"},
                "max_queue": {"type": ["integer", "null"], "minimum": 0, "default": None,
                              "description": "null = no limit; over a set limit a trigger is recorded as dropped"}}},
            "missed": {"type": "object", "properties": {
                "mode": {"enum": list(MISSED_MODES), "default": "run_once"},
                "grace": {"$ref": "#/$defs/duration", "default": "10m"}}},
            "retention_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650,
                               "description": "null = the hub setting job.retention_days (30)"},
            "access": {"type": "object"},
            "guardrails": {"type": "object"},
            "alerts": {"type": "object", "properties": {
                "on_failure": {"type": "array", "items": {"$ref": "#/$defs/notify"}},
                "on_success": {"type": "array", "items": {"$ref": "#/$defs/notify"}}}},
            "entry": {"type": "string", "description": "id of the first step"},
            "steps": {"type": "object", "propertyNames": {"pattern": STEP_ID.pattern},
                      "additionalProperties": {"$ref": "#/$defs/step"}},
            "vars": {"type": "object", "description": "{{vars.x}} in step args"},
        },
        "$defs": {
            "duration": {"type": ["string", "number"], "description": "e.g. 30s, 5m, 1h30m, 2d, or seconds"},
            "rule": {"oneOf": [{"enum": ["all", "any"]},
                               {"type": "object", "properties": {"at_least": {"type": "integer", "minimum": 1}},
                                "required": ["at_least"]}]},
            "filter": {"type": "object", "properties": {
                "has_cap": {"type": "string"}, "os": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}}}},
            "worker": WORKER_SCHEMA,
            "notify": {"type": "object", "required": ["text"], "properties": {
                "via": {"enum": ["voice", "notify", "telegram"], "default": "notify"},
                "text": {"type": "string"}, "title": {"type": "string"}}},
            "step": {"type": "object", "required": ["kind"], "properties": {
                "kind": {"enum": step_kinds()},
                "timeout": {"$ref": "#/$defs/duration", "default": DEFAULT_TIMEOUT},
                "on": {"type": "object", "properties": {o: {"type": "array", "items": {"type": "string"}}
                                                        for o in OUTCOMES}},
                "success": {"type": "object", "properties": {
                    "exit_codes": {"type": "array", "items": {"type": "integer"}, "default": [0]},
                    "match": {"type": "string"}, "not_match": {"type": "string"}}},
                "retry": {"type": "object", "properties": {
                    "max": {"type": "integer", "minimum": 0, "maximum": MAX_RETRIES},
                    "delay": {"$ref": "#/$defs/duration"}}},
                "allow_failure": {"type": "boolean", "default": False,
                                  "description": "this step failing does not fail the run"}}},
            "kinds": kinds,
        },
        "x-variables": ["{{run.id}}", "{{run.started}}", "{{run.missed}}", "{{job.name}}", "{{job.id}}",
                        "{{vars.<name>}}", "{{secret:<name>}}"],
        "x-join-expressions": "steps.<id>.ok / .state / .exit_code, run.missed, vars.<name>; "
                              "and, or, not, ==, !=, <, >, in",
    }

