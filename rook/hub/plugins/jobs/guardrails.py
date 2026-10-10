"""Job guardrails: which caps a job's steps may call (docs/design/jobs.md 7).

Guardrails are permission-policy rules (:mod:`rook.hub.policy`), evaluated
with the job as a principal (``job:<id>``) in the on-behalf-of chain. Three
layers, each compiled into a :class:`~rook.hub.policy.Policy` and evaluated
by the policy engine:

1. the job's own ``guardrails.deny`` list: a match blocks;
2. the job's own ``guardrails.allow`` list (set by the operator only): a
   match allows, even over a default deny;
3. the defaults: the hub setting ``job.guardrails`` (``{"deny": [...],
   "allow": [...]}``) for jobs with ``inherit: true`` (the default), else
   the copy the job saved (``guardrails.base``). The most specific rule wins,
   so the default ``allow: ["secret.set"]`` beats ``deny: ["tier:admin"]``.

Then the hub's own policy: an operator rule naming ``job:*`` or ``job:<id>``
that denies the call (in ``enforce`` mode) blocks it too.

A guardrail entry is a cap selector, as in a policy rule: an exact cap
(``worker.update``), a glob (``selfupdate.*``), ``tier:admin``,
``tag:destructive``, or ``<cap>:<action>`` for action-style caps
(``job.write:delete``, ``job.write:*``). An object
``{"cap": <selector>, "on": <target selector>, "note": "..."}`` limits it to
some workers. Each call is checked as its cap and, when its args carry an
``action``, as ``<cap>:<action>`` too; either being blocked blocks it.

Steps are checked twice: :func:`check_step` before the step (statically, as
far as the step names its cap and worker; the executor ends a refused step
``blocked`` and never retries it), and :class:`GuardedRuntime` /
the hub-tool ``invoke`` at each call, with the worker it actually resolved
to (a fan-out checks every worker; a hub tool checks every cap it calls).
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from ....core import authz as core_authz
from ...policy import Policy, PolicyError, Principal, Target

log = logging.getLogger("rook.hub.plugins.jobs.guardrails")


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""
    rule: str = ""


ALLOW = Verdict(True)


class Blocked(PermissionError):
    """A call inside a step was refused by a guardrail (the step ends
    ``blocked``)."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.reason or "blocked by a job guardrail")
        self.verdict = verdict


#: The default deny list (setting ``job.guardrails``). Real cap names from the
#: worker and hub plugins, plus patterns for caps that do not exist yet.
DEFAULT_DENY = [
    # every admin-risk cap (permissions.md 2: secret.get, persona.set, ...)
    "tier:admin",
    # deauth, re-band and enrollment changes
    "worker.deauth", "band.deauth", "band.admin", "member.admin", "token.admin",
    "worker.reconfigure", "worker.config_apply", "worker.config_revert", "worker.config_confirm",
    "worker.enrollment_prepare", "worker.enrollment_move_prepare", "worker.enrollment_finish",
    "worker.enrollment_prove", "enrollment.*",
    # self-update and worker updates
    "selfupdate.*", "worker.update", "worker.apply", "worker.check", "worker.ota_begin",
    "worker.hold", "worker.restart", "worker.plugin.enable", "worker.plugin.disable",
    "customcap.add", "customcap.remove", "settings.apply_worker",
    # hub service restart or deploy
    "hub.restart", "hub.restart_*", "hub.deploy", "hub.deploy_*", "hub.update", "service.restart*",
    "*.deploy",
    # policy and guardrail edits
    "policy.set", "settings.set", "settings.reset", "guidance.write",
    "job.write:set_guardrails", "job.write:settings",
    # permanent deletes
    "secret.delete", "persona.delete", "chat.delete", "msg.clear", "deluge.remove",
    "job.write:delete", "*.delete", "*.purge", "*:delete", "*:purge",
]
#: Allowed over the deny list: vault writes. ``exec`` caps (``shell.exec``)
#: are not denied at all.
DEFAULT_ALLOW = ["secret.set"]
DEFAULT_GUARDRAILS = {"deny": DEFAULT_DENY, "allow": DEFAULT_ALLOW}

_LAYERS = ("job-deny", "job-allow", "default")
_DEFAULT_RULE = "default:"


# -- documents ----------------------------------------------------------------------

def _entry(e: Any) -> dict:
    if isinstance(e, str):
        return {"cap": e.strip()}
    if isinstance(e, dict):
        return {k: e[k] for k in ("cap", "on", "note") if k in e}
    raise PolicyError(f"guardrail entries are cap selectors or {{cap, on}} objects, not {e!r}")


def _key(e: dict) -> str:
    return e["cap"] if set(e) - {"note"} == {"cap"} else f"{e['cap']}@{json.dumps(e.get('on'))}"


def _rules(prefix: str, action: str, entries: list) -> list[dict]:
    out, seen = [], set()
    for raw in entries or []:
        e = _entry(raw)
        rid = f"{prefix}:{_key(e)}"
        if rid in seen:
            continue
        seen.add(rid)
        rule = {"id": rid, "who": "*", action: e.get("cap"), "on": e.get("on", "*")}
        if e.get("note"):
            rule["note"] = e["note"]
        out.append(rule)
    return out


def _policy_doc(rules: list[dict]) -> dict:
    return {"version": 1, "mode": "enforce",
            "defaults": {"read": "allow", "write": "allow", "exec": "allow", "admin": "allow"},
            "rules": rules}


def check_list(value: Any, where: str) -> list[str]:
    """Problems with a deny/allow list (``[]`` when it compiles)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{where}: a list of cap selectors"]
    errs = []
    for i, e in enumerate(value):
        try:
            Policy(_policy_doc(_rules("x", "deny", [e])))
        except PolicyError as err:
            errs.append(f"{where}[{i}]: {err}")
    return errs


def check_defaults(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["guardrails: an object {deny: [...], allow: [...]}"]
    errs = [f"guardrails.{k}: unknown field (use deny, allow)" for k in doc if k not in ("deny", "allow")]
    for k in ("deny", "allow"):
        errs += check_list(doc.get(k, []), f"guardrails.{k}")
    return errs


def normalize_defaults(doc: Any) -> dict:
    """``{"deny": [...], "allow": [...]}``; a missing or broken value is the
    built-in default (the guardrails never fail open)."""
    if not isinstance(doc, dict) or check_defaults(doc):
        return copy.deepcopy(DEFAULT_GUARDRAILS)
    return {"deny": list(doc.get("deny") or []), "allow": list(doc.get("allow") or [])}


def check_job_block(g: Any) -> list[str]:
    """Problems with a job's ``guardrails`` block."""
    if not isinstance(g, dict):
        return ["guardrails: must be an object"]
    errs = [f"guardrails.{k}: unknown field (use inherit, allow, deny, base)"
            for k in g if k not in ("inherit", "allow", "deny", "base")]
    if not isinstance(g.get("inherit", True), bool):
        errs.append("guardrails.inherit: true or false")
    for k in ("allow", "deny"):
        errs += check_list(g.get(k, []), f"guardrails.{k}")
    if g.get("base") is not None:
        errs += [e.replace("guardrails.", "guardrails.base.", 1) for e in check_defaults(g["base"])]
    return errs


# -- the engine ----------------------------------------------------------------------

class Guardrails:
    """Evaluates guardrails for jobs. ``settings(name)`` reads ``job.*``
    settings (``guardrails``); ``hub_policy()`` returns the hub's current
    :class:`Policy` or ``None``; ``roster()`` the live roster (for declared
    tiers and static checks); ``hub_tier(cap)`` a hub cap's declared risk."""

    def __init__(self, settings: Callable[[str], Any] = lambda _k: None, *,
                 hub_policy: Callable[[], Any] = lambda: None,
                 roster: Callable[[], dict] = dict, hub_id: Callable[[], str] = lambda: "rook",
                 hub_tier: Callable[[str], Any] = lambda _c: None) -> None:
        self.settings = settings
        self.hub_policy = hub_policy
        self.roster = roster
        self.hub_id = hub_id
        self.hub_tier = hub_tier
        self._cache: dict[str, Policy] = {}
        self._lock = threading.Lock()

    # -- layers --------------------------------------------------------------
    def defaults(self) -> dict:
        try:
            return normalize_defaults(self.settings("guardrails"))
        except Exception:  # noqa: BLE001 - a broken setting is the built-in list
            log.exception("jobs: reading job.guardrails failed; using the built-in defaults")
            return copy.deepcopy(DEFAULT_GUARDRAILS)

    def base_for(self, job: dict, defaults: dict | None = None) -> dict:
        """The defaults layer a job uses: the current defaults when it
        inherits, else its saved copy."""
        g = job.get("guardrails") or {}
        if g.get("inherit", True) is False and isinstance(g.get("base"), dict):
            return normalize_defaults(g["base"])
        return normalize_defaults(defaults) if defaults is not None else self.defaults()

    def _compiled(self, rules: list[dict]) -> Policy:
        key = json.dumps(rules, sort_keys=True)
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            hit = Policy(_policy_doc(rules))
            with self._lock:
                if len(self._cache) > 256:
                    self._cache.clear()
                self._cache[key] = hit
        return hit

    def layers(self, job: dict, defaults: dict | None = None) -> list[tuple[str, Policy]]:
        g = job.get("guardrails") or {}
        base = self.base_for(job, defaults)
        out = []
        if g.get("deny"):
            out.append(("job-deny", self._compiled(_rules("job-deny", "deny", g["deny"]))))
        if g.get("allow"):
            out.append(("job-allow", self._compiled(_rules("job-allow", "allow", g["allow"]))))
        out.append(("default", self._compiled(_rules("default-deny", "deny", base["deny"])
                                              + _rules("default-allow", "allow", base["allow"]))))
        return out

    # -- tiers and targets ----------------------------------------------------
    def tier_hint(self, cap: str, entry: dict | None, is_hub: bool) -> Any:
        tiers = (entry or {}).get("tiers") or {}
        if cap in tiers:
            return tiers[cap]
        if is_hub:
            try:
                t = self.hub_tier(cap)
            except Exception:  # noqa: BLE001
                t = None
            if t:
                return t
        try:
            for e in (self.roster() or {}).values():
                t = (e.get("tiers") or {}).get(cap)
                if t:
                    return t
        except Exception:  # noqa: BLE001
            pass
        return None

    def target(self, target_id: str | None, entry: dict | None) -> Target:
        e = entry or {}
        hub = target_id is not None and target_id == self.hub_id()
        return Target(id=target_id or "*", name=str(e.get("name") or ("rook" if hub else "")),
                      device_id=str(e.get("device_id") or ""), facts=dict(e.get("facts") or {}),
                      roles=frozenset(e.get("roles") or (("is_hub",) if hub else ())),
                      is_rook=hub or "is_hub" in (e.get("roles") or ()),
                      tiers=dict(e.get("tiers") or {}))

    # -- evaluation ------------------------------------------------------------
    def check_call(self, job: dict, cap: str, target_id: str | None = None, entry: dict | None = None,
                   args: Any = None, *, defaults: dict | None = None, via: tuple = ()) -> Verdict:
        """Whether ``job`` may call ``cap`` on ``target_id`` with ``args``."""
        if not cap:
            return ALLOW
        t = self.target(target_id, entry)
        tier = self.tier_hint(cap, entry, t.is_rook)
        names = [cap]
        action = args.get("action") if isinstance(args, dict) else None
        if isinstance(action, str) and action.strip():
            names.append(f"{cap}:{action.strip().lower()}")
        who = Principal(f"job:{job.get('id')}", "job", "", (), via=via)
        # A synthetic ``<cap>:<action>`` name keeps the real cap's tier.
        tiers = [tier] + [core_authz.effective_tier(cap, tier)] * (len(names) - 1)
        layers = self.layers(job, defaults)
        for name, ntier in zip(names, tiers):
            v = self._one(layers, who, name, t, ntier)
            if not v.allow:
                return v
        return self._hub(who, names, tiers, t)

    def _one(self, layers, who: Principal, name: str, t: Target, tier: Any) -> Verdict:
        for layer, pol in layers:
            d = pol.evaluate([who], name, t, declared_tier=tier)
            if not d.rule or d.rule.startswith(_DEFAULT_RULE):
                continue  # no rule in this layer matched
            if layer == "job-allow":
                return ALLOW
            if d.denied:
                return Verdict(False, f"blocked by job guardrail {d.rule} ({name}, {d.tier})", d.rule)
            return ALLOW  # a default allow beat the default denies
        return ALLOW

    def _hub(self, who: Principal, names: list, tiers: list, t: Target) -> Verdict:
        try:
            pol = self.hub_policy()
        except Exception:  # noqa: BLE001
            pol = None
        if pol is None:
            return ALLOW
        for name, tier in zip(names, tiers):
            try:
                d = pol.evaluate([who], name, t, declared_tier=tier)
            except Exception:  # noqa: BLE001 - the authorizer still checks the call
                log.exception("jobs: hub policy evaluation failed for %s", name)
                continue
            if d.denied and d.rule and not d.rule.startswith(_DEFAULT_RULE) \
                    and not d.rule.startswith("invariant:"):
                return Verdict(False, f"blocked by hub policy rule {d.rule} for {who.id} ({name})",
                               f"policy:{d.rule}")
        return ALLOW

    # -- static checks ------------------------------------------------------------
    def step_targets(self, step: dict, roster: dict | None = None) -> list[tuple[str, str | None, dict | None, Any]]:
        """``(cap, target id, roster entry, args)`` a step would call, as far
        as it can be known before it runs. A worker that is not live is
        checked as an unknown worker."""
        from ...authz import hub_cap_for_tool
        from .kinds import VIAS, matches, resolve_worker
        roster = roster if roster is not None else (self.roster() or {})
        hub = self.hub_id()
        kind = step.get("kind")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}

        def one(cap: str, spec: Any) -> list:
            try:
                found = resolve_worker(roster, spec, cap, hub) if spec is not None else None
            except ValueError:
                found = None
            if found:
                return [(cap, found[0], roster.get(found[0]), args)]
            if isinstance(spec, str) and spec.lower() == "rook":
                return [(cap, hub, roster.get(hub), args)]
            return [(cap, None, {"name": spec} if isinstance(spec, str) else None, args)]

        if kind == "cap" and isinstance(step.get("cap"), str):
            return one(step["cap"], step.get("worker"))
        if kind == "fanout" and isinstance(step.get("cap"), str):
            flt = step.get("filter") if isinstance(step.get("filter"), dict) else {}
            hits = [(step["cap"], wid, e, args) for wid, e in sorted(roster.items())
                    if matches(e, flt, step["cap"])]
            return hits or [(step["cap"], None, None, args)]
        if kind == "tool" and isinstance(step.get("tool"), str):
            cap = hub_cap_for_tool(step["tool"], args)
            return [(cap, hub, roster.get(hub), args)] if cap else []
        if kind == "notify":
            via = step.get("via", "notify")
            cap = VIAS.get(via)
            if not cap:
                return []
            if via == "telegram":
                return [(cap, hub, roster.get(hub), {})]
            return one(cap, step.get("worker") or self.settings("notify_worker") or {"any_with_cap": True})
        return []

    def scan_step(self, job: dict, step: dict, *, defaults: dict | None = None,
                  roster: dict | None = None) -> list[dict]:
        """The calls of ``step`` the guardrails would block now."""
        out = []
        for cap, wid, entry, args in self.step_targets(step, roster):
            v = self.check_call(job, cap, wid, entry, args, defaults=defaults)
            if not v.allow:
                out.append({"cap": cap, "target": (entry or {}).get("name") or wid or "?",
                            "rule": v.rule, "reason": v.reason})
        return out

    def scan(self, job: dict, *, defaults: dict | None = None, roster: dict | None = None) -> list[dict]:
        """Every step of ``job`` the guardrails would block, as
        ``{step, cap, target, rule, reason}``."""
        roster = roster if roster is not None else self._roster()
        out = []
        for sid, step in sorted((job.get("steps") or {}).items()):
            if isinstance(step, dict):
                out += [{"step": sid, **b} for b in self.scan_step(job, step, defaults=defaults, roster=roster)]
        return out

    def _roster(self) -> dict:
        try:
            return self.roster() or {}
        except Exception:  # noqa: BLE001
            return {}

    def warnings(self, job: dict) -> list[str]:
        """Save-time warnings for steps that would be blocked now."""
        return [f"steps.{b['step']}: {b['cap']} on {b['target']} would be blocked ({b['rule']})"
                for b in self.scan(job)]


# -- the per-run guard ------------------------------------------------------------------

class JobGuard:
    """The guardrails bound to one job, carried on the run's identity."""

    def __init__(self, engine: Guardrails, job: dict, via: tuple = ()) -> None:
        self.engine = engine
        self.job = job
        self.via = via

    def call(self, cap: str, target_id: str | None, entry: dict | None, args: Any = None) -> Verdict:
        return self.engine.check_call(self.job, cap, target_id, entry, args, via=self.via)

    def step(self, step: dict) -> Verdict:
        blocks = self.engine.scan_step(self.job, step)
        if blocks:
            b = blocks[0]
            return Verdict(False, b["reason"], b["rule"])
        return ALLOW


def check_step(job: dict, step: dict, identity) -> Verdict:
    """Whether ``identity`` may run ``step`` of ``job`` now (the executor asks
    before every step). Identities built without guardrails (tests, J1
    callers) use the built-in defaults."""
    guard = getattr(identity, "guard", None)
    if guard is None:
        guard = JobGuard(_FALLBACK, job)
    try:
        return guard.step(step)
    except Exception:  # noqa: BLE001 - the per-call check still runs
        log.exception("jobs: static guardrail check failed for job %s", job.get("id"))
        return ALLOW


_FALLBACK = Guardrails()


def guard_of(identity, job: dict) -> JobGuard:
    guard = getattr(identity, "guard", None)
    return guard if guard is not None else JobGuard(_FALLBACK, job)


class GuardedRuntime:
    """Wraps a step's runtime so every band call is checked against the
    job's guardrails with the worker it resolved to. A refused call comes back
    as a ``denied`` reply, which ends the step (or that fan-out worker)
    ``blocked``; it is journaled like any other reply."""

    def __init__(self, inner: Any, job: dict, identity: Any) -> None:
        self._inner = inner
        self._job = job
        self._guard = guard_of(identity, job)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def call(self, cap: str, args: dict, target: str, timeout: float, identity) -> dict:
        try:
            entry = (self._inner.roster() or {}).get(target)
        except Exception:  # noqa: BLE001
            entry = None
        v = self._guard.call(cap, target, entry, args)
        if not v.allow:
            return {"ok": False, "error": v.reason,
                    "denied": {"guardrail": v.rule, "principal": f"job:{self._job.get('id')}"}}
        return await self._inner.call(cap, args, target, timeout, identity)
