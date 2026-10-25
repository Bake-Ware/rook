"""The blanket holds (advanced security; ``docs/design/permissions.md`` 3.11),
moved here from rook/hub/approvals.py. :func:`apply` is what
``Approvals.apply`` did before the change-management reset; core calls it
only while the plugin is on.

Why a call is held, for agent-like principals only (``approvals.principals``):

* the trust map's ``guarded`` entries (ask first) and ``sacred`` ones;
* an operator ``hold`` rule or cell in the policy;
* the built-in default, ``approvals.hold`` (``tier:admin``, ``tag:destructive``),
  unless a policy ``allow`` rule at least as specific as the default matches;
* exec and above on a worker that runs on the hub's own machine;
* the spread limit: the call would be the agent's ``spread_max + 1``-th
  distinct machine for exec/admin/destructive calls within the window, and no
  spread grant is active for it.

``approvals.enabled`` off keeps only the trust map's holds, as before.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from ....core import authz as core_authz

log = logging.getLogger("rook.hub.plugins.security.holds")

_PROCEEDS = ("allow", "would_deny", "off", "error_allow")


def default_score(selectors: tuple, cap: str, tier: str, tags: tuple) -> tuple[int, str] | None:
    """``(cap specificity, selector)`` of the best ``approvals.hold``
    selector matching the call, or None."""
    from ...policy import PolicyError, _CapSel
    best = None
    for spec in selectors:
        try:
            sel = _CapSel(spec)
        except PolicyError:
            log.warning("approvals.hold: bad selector %r ignored", spec)
            continue
        score = sel.match(cap, tier, tags)
        if score is not None and (best is None or score > best[0]):
            best = (score, spec)
    return best


def _reason(spec: str) -> str:
    if spec.startswith("tier:"):
        return f"tier {spec[5:]}"
    if spec.startswith("tag:"):
        return f"tag {spec[4:]}"
    return f"cap {spec}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def countable(cap: str, tier: str, tags: tuple, target: Any) -> bool:
    """Spread counts exec/admin and destructive calls to a worker other than
    the hub itself."""
    if target is None or not getattr(target, "id", None):
        return False
    if getattr(target, "is_rook", False) or str(getattr(target, "name", "")).lower() == "rook":
        return False
    return core_authz.tier_rank(tier) >= core_authz.tier_rank("exec") or "destructive" in tags


# -- hub-host workers -------------------------------------------------------------

def hub_host_id(ap: Any) -> str | None:
    """The host id fact of the hub's own node, so we can spot workers that
    run on the hub's machine (they can edit approvals.db, policy.json and
    settings directly, so their exec+ calls are held)."""
    node = getattr(ap._client(), "_local", None)
    facts = getattr(getattr(node, "facts", None), "hw", None) or {}
    hid = facts.get("host_id")
    return str(hid) if hid else None


def hub_host_workers(ap: Any, cfg: Any = None) -> dict:
    """What the Approvals page shows: the live workers treated as running
    on the hub's machine, how each was decided, and any detection gap."""
    cfg = cfg or ap.config()
    sel = [s.lower() for s in cfg.hub_host_workers]
    if "off" in sel:
        return {"mode": "off", "workers": [], "note": "hub-host holds are off"}
    explicit = {s for s in sel if s and s != "auto"}
    auto = "auto" in sel or not explicit
    roster = getattr(ap._client(), "workers", {}) or {}
    hub_id = hub_host_id(ap) if auto else None
    out = []
    for wid, w in roster.items():
        name = str(w.get("name") or wid)
        if "is_hub" in (w.get("roles") or ()):
            continue
        why = None
        if name.lower() in explicit or wid.lower() in explicit:
            why = "named in approvals.hub_host_workers"
        elif auto and hub_id and (w.get("facts") or {}).get("host_id") == hub_id:
            why = "same machine id as the hub"
        if why:
            out.append({"worker": name, "id": wid, "why": why})
    note = None
    if auto and hub_id is None:
        note = ("the hub announces no machine id, so co-located workers can't be "
                "detected; name them in approvals.hub_host_workers")
    elif auto and hub_id and not out:
        note = "no live worker runs on the hub's machine"
    return {"mode": "auto" if auto else "list", "workers": out, "note": note,
            "detected": hub_id is not None}


def is_hub_host(ap: Any, target: Any, cfg: Any) -> bool:
    sel = [s.lower() for s in cfg.hub_host_workers]
    if "off" in sel or target is None or getattr(target, "is_rook", False):
        return False
    explicit = {s for s in sel if s and s != "auto"}
    name = str(getattr(target, "name", "") or "").lower()
    tid = str(getattr(target, "id", "") or "").lower()
    if name in explicit or tid in explicit:
        return True
    if "auto" in sel or not explicit:
        hub_id = hub_host_id(ap)
        return bool(hub_id and (getattr(target, "facts", {}) or {}).get("host_id") == hub_id)
    return False


# -- deciding to hold -------------------------------------------------------------

def apply(ap: Any, d: Any, p: Any, policy: Any, target: Any, cfg: Any, agent: bool) -> Any:
    """The blanket holds for a policy decision ``d`` (not denied) of
    principal ``p``: ``d``, possibly turned into a hold (or a hold turned into
    an allow where holds do not apply)."""
    from ...approvals import own_resource
    # The trust map: a change to something sacred waits for the person, one
    # to something guarded asks first, even with approvals.enabled off. A gap
    # (unlisted, balanced) falls through to the holds below.
    ruling = ap.trust_ruling(d, target) if agent and not own_resource.get() else None
    trust_hold = None
    if ruling is not None:
        d = dataclasses.replace(d, trust_rule=ruling.rule, trust=ruling.cite)
        if ruling.tier in ("ask", "person") and not (ruling.unlisted
                                                     and ruling.level == "balanced"):
            trust_hold = dataclasses.replace(d, decision="hold", hold_kind="trust",
                                             hold_reason=ruling.cite, rule=ruling.rule)
    if not cfg.enabled:
        # Off: no blanket holds, operator hold rules included; the trust map still holds.
        if trust_hold is not None:
            return trust_hold
        return ap._unhold(d, "approvals off") if d.held else d
    if not agent:
        return ap._unhold(d, "never held") if d.held else d
    count = countable(d.cap, d.tier, d.tags, target)
    if count:
        d.spread_target = str(target.id)
        if trust_hold is not None:
            trust_hold.spread_target = d.spread_target
    if d.held:
        return d  # an operator hold rule or cell
    if trust_hold is not None:
        return trust_hold
    best = default_score(cfg.hold, d.cap, d.tier, d.tags)
    if best is not None and not own_resource.get():
        rule = policy.best_rule(p, d.cap, d.tier, d.tags, target)
        if not (rule is not None and rule[1].action == "allow" and rule[0] >= (0, best[0], 0)):
            return dataclasses.replace(d, decision="hold", hold_kind="default",
                                       hold_reason=_reason(best[1]))
    # Exec and above on a worker that runs on the hub's own machine: that
    # worker can rewrite the hub's files (approvals.db, policy.json,
    # settings), so it is a hold unless an operator allow rule names it.
    if core_authz.tier_rank(d.tier) >= core_authz.tier_rank("exec") \
            and not own_resource.get() and is_hub_host(ap, target, cfg):
        rule = policy.best_rule(p, d.cap, d.tier, d.tags, target)
        if not (rule is not None and rule[1].action == "allow" and rule[0] >= (4, 0, 0)):
            return dataclasses.replace(d, decision="hold", hold_kind="hub-host",
                                       hold_reason="runs on the hub's machine")
    if count and cfg.spread_max > 0:
        n = ap.spread.count_with(p.id, str(target.id), cfg.spread_window_secs)
        if n > cfg.spread_max and ap.queue.active_grant(p.id) is None:
            minutes = max(1, round(cfg.spread_window_secs / 60))
            return dataclasses.replace(
                d, decision="hold", hold_kind="spread",
                hold_reason=f"spread: {_ordinal(n)} machine in {minutes} minutes")
    if d.decision in _PROCEEDS:
        ap.proceeded(d)
    return d
