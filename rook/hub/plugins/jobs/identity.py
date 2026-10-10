"""Who a job run acts as, and who is calling the jobs caps.

J1 implements the ``creator`` identity only: a run acts as the principal that
created the job, with the job in its on-behalf-of chain (``job:<id>``). The
``key`` / ``vault`` / ``fallback`` modes, revocation and the
edit-resets-identity rule (docs/design/jobs.md 7) replace
:func:`resolve_identity`; everything else calls through it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ....core.context import current_identity
from ...policy import Principal

#: Identity modes the contract names; J1 accepts ``creator`` only.
MODES = ("creator", "key", "vault", "fallback")
SUPPORTED_MODES = ("creator",)
SYSTEM_OWNER = {"id": "system:jobs", "kind": "system", "role": "system", "groups": [], "label": "jobs"}


@dataclass(frozen=True)
class RunIdentity:
    mode: str
    id: str                 # the principal id the run acts as
    display: str            # envelope / journal identity: job:<id>/<principal>
    principal: Principal


def caller() -> dict:
    """The authenticated caller of the current cap call, as stored on a job
    (``owner_info``): the MCP token or dashboard account the bridge put in
    ``current_principal``, else in-process hub code (its own identity, or
    ``system:jobs``)."""
    from ...authz import current_principal
    p = current_principal.get()
    if p is not None:
        return {"id": p.id, "kind": p.kind, "role": p.role, "groups": list(p.groups),
                "label": p.label, "verified": bool(p.verified)}
    ident = current_identity()
    if ident:
        system = ident.startswith("system:")
        return {"id": ident, "kind": "system" if system else "unknown",
                "role": "system" if system else "", "groups": [], "label": ident}
    return dict(SYSTEM_OWNER)


def resolve_identity(job: dict, owner_info: dict | None) -> RunIdentity:
    """The identity a run of ``job`` acts as. Raises ``PermissionError`` when
    there is none usable (J2: revoked keys, fallbacks)."""
    mode = (job.get("identity") or {}).get("mode") or "creator"
    if mode not in SUPPORTED_MODES:
        raise PermissionError(f"identity mode {mode!r} is not available on this hub yet")
    info = owner_info or SYSTEM_OWNER
    pid = str(info.get("id") or SYSTEM_OWNER["id"])
    principal = Principal(pid, str(info.get("kind") or "unknown"), str(info.get("role") or ""),
                          tuple(info.get("groups") or ()), via=(f"job:{job.get('id')}",),
                          verified=bool(info.get("verified", True)),
                          label=str(info.get("label") or ""))
    return RunIdentity(mode, pid, f"job:{job.get('id')}/{pid}", principal)
