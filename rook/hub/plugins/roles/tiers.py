"""What a role may do without a person (docs/design/huddles.md 4.5).

An interim action tier, kept by the hub per cap (never by the role): the
precursor of H6's tiers. Every call a role makes is one of

* ``free``: runs at once. Reads in the role's scope (a cap whose tier is
  ``read``), and the role's own workspace (its hub tools, not a cap).
* ``routine``: runs at once for a role whose autonomy is ``routine`` or
  ``act``; a card for one that only proposes. Starting a coding session on
  a branch in a configured code location, reading and following it, and
  sending it more work once it stopped (the role's ``start_session`` /
  ``session_status`` / ``session_send`` tools: the branch, folder and push
  rules are the hub's, see :mod:`.sessions`). The session may open a pull
  request; it can never merge one.
* ``approval``: an action card a person approves. Everything else: any cap
  above ``read`` (merges, deploys, deletes, writes outside the workspace,
  settings, jobs, shell), any call on a protected worker (``role.
  protected_workers``: family devices), and secrets, which no autonomy and no
  setting lowers. A cap the hub does not know is ``exec``
  (:func:`rook.core.authz.effective_tier`), so it is ``approval`` too.

The band's policy (audit or enforce) is checked after this, as before: this
tier holds whatever mode the policy is in.
"""
from __future__ import annotations

import fnmatch

from ....core.authz import is_status_read

FREE = "free"
ROUTINE = "routine"
APPROVAL = "approval"

#: Autonomy levels that may do routine work without a card.
ROUTINE_AUTONOMY = ("routine", "act")

#: Secrets: always a card, for every role, whatever its autonomy or tools
#: say. Not a setting: nothing lowers it.
LOCKED = ("*secret*", "vault.*", "*.vault*", "settings.worker_secret", "*credential*",
          "*password*", "*token*", "auth.*", "enroll*.*")

#: The routine actions, by the hub tool that does them.
ROUTINE_TOOLS = ("start_session", "session_send")
#: Hub tools that only read (the role's own sessions).
FREE_TOOLS = ("session_status",)


def locked(cap: str) -> bool:
    """A secret: never callable without a person, whatever else says so."""
    return any(fnmatch.fnmatchcase(cap.lower(), p) for p in LOCKED)


def is_routine(role: dict) -> bool:
    return (role or {}).get("autonomy") in ROUTINE_AUTONOMY


def cap_tier(role: dict, cap: str, cap_risk: str, worker: str = "",
             protected: tuple | list = ()) -> tuple[str, str]:
    """``(tier, why)`` for a role calling ``cap`` (whose effective tier is
    ``cap_risk``) on ``worker``. Scope (``tools``) is checked before this; a
    call outside it is refused, not carded."""
    if locked(cap):
        return APPROVAL, f"{cap} touches secrets: a person approves every such call"
    if is_status_read(cap):
        # Battery, device info, versions: free on any device, family devices included.
        return FREE, ""
    if worker and any(worker.lower() == str(p).strip().lower() for p in protected or ()):
        return APPROVAL, f"{worker} is a protected device (role.protected_workers): a person " \
                         "approves anything on it"
    if cap_risk == "read":
        return FREE, ""
    return APPROVAL, f"{cap} is a {cap_risk} cap: roles run only reads and routine work " \
                     "without a person"


def tool_tier(role: dict, tool: str) -> tuple[str, str]:
    """``(tier, why)`` for one of the role's session tools."""
    if tool in FREE_TOOLS:
        return FREE, ""
    if tool in ROUTINE_TOOLS:
        if is_routine(role):
            return ROUTINE, ""
        return APPROVAL, (f"role {role.get('id')} only proposes: starting or steering a coding "
                          "session needs a person's approval")
    return APPROVAL, f"{tool} is not a routine action"
