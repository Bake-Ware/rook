"""Worker-side defense in depth (``docs/design/permissions.md`` 3.5 E2, 3.6).

The worker does not hold the hub's policy. A *ticket* attached to a call is
the hub's decision; the worker checks that it is authentic (signed by an op
key that holds a valid ``is_hub`` grant for this band, chained to a trusted
root), fresh, unreplayed and bound to this exact call. Whether a ticket is
*needed* depends on the worker's enforcement mode and its own tier table
(declared tier floored by the built-in table):

=============== ============================================================
``off``         nothing checked (build-167 behaviour)
``audit``       default: verified, never refused; unticketed exec/admin
                calls are logged loudly in ``audit.jsonl``
``enforce-admin`` admin needs a valid ticket
``enforce-exec``  exec + admin need a valid ticket
``enforce-all``   everything but ``caps.describe`` and ``info.ping``
=============== ============================================================

The mode comes from ``ROOK_AUTHZ_MODE`` (settable remotely through a
``worker.config_apply`` env push, locally as the recovery hatch).

Grants are learned from hub announces (``grants`` + ``asig``: the hub proves
it holds the grant key) or inlined in a ticket (``ticket.grant``) for the
first call after a key change.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..core import authz

log = logging.getLogger("rook.worker.authz")

MODES = ("off", "audit", "enforce-admin", "enforce-exec", "enforce-all")
_ALWAYS_OPEN = frozenset({"caps.describe", "info.ping"})
_MAX_KEYS = 8


def current_mode() -> str:
    mode = os.environ.get("ROOK_AUTHZ_MODE", "audit").strip().lower() or "audit"
    return mode if mode in MODES else "audit"


def needs_ticket(mode: str, cap: str, tier: str) -> bool:
    if mode in ("off", "audit"):
        return False
    if mode == "enforce-all":
        return cap not in _ALWAYS_OPEN
    if mode == "enforce-exec":
        return authz.tier_rank(tier) >= authz.tier_rank("exec")
    return tier == "admin"  # enforce-admin


class Guard:
    """Ticket verification state for one worker on one band."""

    def __init__(self, worker_id: str, band_hex: str | None,
                 anchors: list[str] | None = None, mode: str | None = None) -> None:
        self.worker_id = worker_id
        self.band = band_hex
        self.anchors = list(anchors if anchors is not None else authz.default_anchors())
        self.mode = mode if mode in MODES else current_mode()
        self.keys: dict[str, dict] = {}          # op kid -> verified is_hub grant
        self._seq: dict[str, int] = {}            # op kid -> last announce seq
        self.revoked: set[str] = set()
        self._rev_seq = -1
        self.replay = authz.ReplayCache()

    # -- readiness ---------------------------------------------------------
    def readiness(self) -> dict:
        """Announce ``authz`` block (5.1): the hub gates features on this."""
        return {"v": 1, "mode": self.mode,
                "anchors": [authz.key_id(a) for a in self.anchors],
                "kids": sorted(self.keys)}

    # -- learning grants ---------------------------------------------------
    def _accept_grant(self, grant: dict, now: float) -> bool:
        ok, why = authz.verify_grant(grant, self.anchors, band=self.band, now=now,
                                     revoked=self.revoked)
        if not ok or grant.get("role") != "is_hub":
            log.debug("grant rejected: %s", why)
            return False
        kid = grant["sub"]["kid"]
        self.keys[kid] = grant
        if len(self.keys) > _MAX_KEYS:  # keep the freshest grants only
            for old in sorted(self.keys, key=lambda k: self.keys[k].get("exp", 0))[:-_MAX_KEYS]:
                self.keys.pop(old, None)
        return True

    def on_announce(self, msg: dict, now: float | None = None) -> bool:
        """Learn a hub op key from an announce carrying ``grants``. Only an
        announce signed by the grant's key counts (proof of possession), and
        its ``seq`` must increase. Returns True if a key was (re)learned."""
        if not self.anchors or not isinstance(msg.get("grants"), list):
            return False
        now = now if now is not None else time.time()
        rev = msg.get("revocations")
        if rev is not None and authz.verify_revocations(rev, self.anchors) \
                and rev["seq"] > self._rev_seq:
            self._rev_seq = rev["seq"]
            self.revoked = set(rev.get("serials") or [])
            for kid, g in list(self.keys.items()):
                if g.get("serial") in self.revoked:
                    self.keys.pop(kid, None)
        held = authz.held_roles(msg, self.anchors, band=self.band, now=now,
                                revoked=self.revoked)
        grant = held.get("is_hub")
        if grant is None:
            return False
        kid = grant["sub"]["kid"]
        try:
            seq = int(msg.get("seq"))
        except (TypeError, ValueError):
            return False
        if seq <= self._seq.get(kid, -1):
            return False
        self._seq[kid] = seq
        return self._accept_grant(grant, now)

    # -- checking calls ----------------------------------------------------
    def check(self, msg: dict, cap: str, tier: str, args: Any,
              now: float | None = None) -> dict:
        """Verify the envelope's ticket. Returns ``{verified, reason, kid, p,
        via, tier, refuse}`` where ``refuse`` is an error string when the
        mode requires a ticket this call does not validly carry."""
        now = now if now is not None else time.time()
        ticket = msg.get("ticket")
        info: dict[str, Any] = {"verified": False, "reason": "no ticket", "tier": tier}
        if self.mode == "off":
            info["reason"] = "mode off"
            return info
        if isinstance(ticket, dict):
            info["kid"] = ticket.get("kid")
            info["p"] = ticket.get("p")
            info["via"] = ticket.get("via") or []
            inline = ticket.get("grant")
            if ticket.get("kid") not in self.keys and isinstance(inline, dict) \
                    and (inline.get("sub") or {}).get("kid") == ticket.get("kid"):
                self._accept_grant(inline, now)
            if not self.anchors:
                info["reason"] = "no trust anchor"
            else:
                ok, why = authz.verify_ticket(
                    ticket, cap=cap, target=self.worker_id, msg_id=str(msg.get("id") or ""),
                    args=args, keys=self.keys, now=now, replay=self.replay)
                info["verified"], info["reason"] = ok, why
                # The ticket's tier is the hub's view; never trust it below ours.
                if ok and authz.tier_rank(ticket.get("tier")) < authz.tier_rank(tier):
                    info["verified"], info["reason"] = False, "ticket tier below cap tier"
        if needs_ticket(self.mode, cap, tier) and not info["verified"]:
            info["refuse"] = (f"denied by worker: {tier} requires a hub ticket "
                              f"(mode {self.mode}; {info['reason']})")
        elif not info["verified"] and authz.tier_rank(tier) >= authz.tier_rank("exec"):
            log.info("ROOK AUTHZ AUDIT: unticketed %s call %s (%s)", tier, cap, info["reason"])
        return info


def require_hub_order(what: str) -> str | None:
    """``None`` if the call being dispatched carries a verified hub ticket
    (the hub's signed order, bound to this worker, message and exact args),
    else the refusal text. Used for changes that repoint a worker at another
    hub or PSK (permissions 6.3). ``ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT=1`` in
    the worker's own environment is the local-root escape hatch."""
    from ..core.context import call_ticket
    if os.environ.get("ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT", "") == "1":
        return None
    info = call_ticket.get()
    if info and info.get("verified"):
        return None
    reason = (info or {}).get("reason") or "no ticket"
    return (f"{what} needs a hub-signed order (a valid call ticket); refused: {reason}. "
            f"Send it through a hub that holds the band's signing key.")
