"""Core v2 rights: the risk/scope/effects enums, grant patterns, the
intersection rule, user presets and the gate verdict rule. Spec sections 4,
8 and 10 of docs/spec/core-v2.md.

A *rights* object is {"grants": [pattern, ...], "max_risk"?: risk,
"effects"?: [effect, ...]}. Every layer that applies to a call (person,
agent, session, plugin ``uses``, the worker's per-hub ceiling, ...) is one
rights object, and a call is allowed only when every layer permits it.
"""
from __future__ import annotations

import re
from typing import Any

RISKS = ("read", "write", "exec", "admin")
SCOPES = ("self", "local", "multi-machine", "multi-user", "external")
EFFECTS = ("destructive", "sensitive", "personal", "physical",
           "sends_as", "spends", "persists", "taints")
# Effects that always hold a call for the person (spec 4.3). Unknown effect
# flags count as destructive.
HARD_STOP_EFFECTS = ("destructive", "sends_as", "spends")

PRESETS = {
    "owner": {"grants": ["*"]},
    "adult": {"grants": ["*"], "max_risk": "admin"},
    "child": {"grants": ["agents.chat.*", "pages.view"], "max_risk": "write", "effects": [],
              "approve_new": "owner"},
    "guest": {"grants": ["agents.chat.*"], "max_risk": "write", "effects": [], "expires": "7d"},
}

VERDICTS = ("allow", "flag", "block")
GATE_REASONS = ("intent_mismatch", "anomaly", "persistence", "exfiltration", "injection",
                "canary", "taint", "policy", "gate_error", "other")


def risk_rank(risk: Any) -> int:
    """Position in read < write < exec < admin; unknown counts as admin."""
    return RISKS.index(risk) if risk in RISKS else len(RISKS) - 1


def _glob(pattern: str) -> re.Pattern:
    return re.compile(".*".join(re.escape(p) for p in pattern.split("*")), re.S)


def matches(pattern: str, cap: str, target: str = "") -> bool:
    """``cap-glob`` or ``cap-glob@target-glob``; ``*`` is the only wildcard
    and matches any run of characters, dots included."""
    name, at, where = pattern.partition("@")
    if not _glob(name).fullmatch(cap):
        return False
    return not at or bool(_glob(where).fullmatch(target or ""))


def valid_rights(r: Any) -> bool:
    if not isinstance(r, dict) or not isinstance(r.get("grants"), list):
        return False
    if not all(isinstance(g, str) and g for g in r["grants"]):
        return False
    if "max_risk" in r and r["max_risk"] not in RISKS:
        return False
    return "effects" not in r or (isinstance(r["effects"], list)
                                  and all(isinstance(e, str) for e in r["effects"]))


def denies(r: dict, call: dict) -> str | None:
    """Why rights ``r`` refuse ``call`` ({"cap", "risk", "effects",
    "target"}): ``grants``, ``risk`` or ``effects``; None when permitted.
    Malformed rights refuse everything (``grants``)."""
    if not valid_rights(r):
        return "grants"
    if not any(matches(g, call["cap"], call.get("target", "")) for g in r["grants"]):
        return "grants"
    if "max_risk" in r and risk_rank(call.get("risk")) > risk_rank(r["max_risk"]):
        return "risk"
    if "effects" in r and not set(call.get("effects") or ()) <= set(r["effects"]):
        return "effects"
    return None


def permits(r: dict, call: dict) -> bool:
    return denies(r, call) is None


def check(layers: list, call: dict) -> dict:
    """The intersection rule: ``layers`` is [[name, rights], ...]; the call is
    allowed only if every layer permits it. Reports the first refusing layer."""
    for name, r in layers:
        why = denies(r, call)
        if why:
            return {"ok": False, "layer": name, "reason": why}
    return {"ok": True}


def preset_rights(preset: dict, plugin_grants: dict | None = None,
                  user_grants: list | None = None) -> dict:
    """A person's rights from their preset: plugins ({plugin: [pattern]}) add
    grants inside their own namespace only, owners add any, and the preset's
    ``max_risk`` and ``effects`` stay the ceiling."""
    added = [g for plugin, pats in (plugin_grants or {}).items() for g in pats
             if g.startswith(plugin + ".")]
    out = {"grants": list(preset["grants"]) + added + list(user_grants or ())}
    for k in ("max_risk", "effects"):
        if k in preset:
            out[k] = preset[k]
    return out


def hard_stop(effects: list) -> bool:
    """True when a call with these effects always waits for the person."""
    return any(e in HARD_STOP_EFFECTS or e not in EFFECTS for e in effects or ())


def gate_decision(results: list) -> dict:
    """Combine gate results at dispatch (spec 10). Shadow-mode gates never
    count; a malformed result or an error counts as a flag; block wins over
    flag, flag over allow. Gates can only restrict."""
    verdict, by = "allow", []
    for r in results:
        if r.get("mode", "enforce") == "shadow":
            continue
        v = r.get("verdict")
        if r.get("error") or v not in VERDICTS:
            v = "flag"
        if v == "allow":
            continue
        if VERDICTS.index(v) > VERDICTS.index(verdict):
            verdict, by = v, []
        if v == verdict:
            by.append(r.get("gate"))
    return {"verdict": verdict, "by": by}
