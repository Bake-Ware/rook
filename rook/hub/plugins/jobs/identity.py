"""Who a job run acts as, and who is calling the jobs caps
(docs/design/jobs.md 7).

A job's ``identity`` block::

    {"mode": "creator" | "key" | "vault" | "fallback",
     "ref": "<key or user>" | "<vault secret name>",
     "fallback": "<identity>" | {"mode": ..., "ref": ...}}

* ``creator`` (default): the principal that created (or last took over) the
  job, stored on the job as ``owner_info``.
* ``key``: a named API key or user: ``token:<agent_id>``, ``human:<id>``, a
  key id, an agent id or a key's unique name.
* ``vault``: a vault secret holding a Rook API key (``name`` or
  ``{{secret:name}}``). The key is read at run time to find its principal and
  is never stored.
* ``fallback``: the run always acts as the fallback identity.

``fallback`` (an identity string: ``creator``, a key reference or
``{{secret:name}}``; or a ``{mode, ref}`` object) is used when the primary
identity is revoked or disabled. Without one, the hub setting
``job.default_fallback`` applies. When nothing usable is left the run ends
``blocked`` and the scheduler pauses the job (``enabled: false``, reason
``identity_revoked``).

Every run acts with the job in its on-behalf-of chain (``job:<id>``) and
carries the job's guardrails (:class:`.guardrails.JobGuard`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ....core.context import current_identity
from ...policy import Principal
from .principals import Directory, Found, secret_name

MODES = ("creator", "key", "vault", "fallback")
SUPPORTED_MODES = MODES
SYSTEM_OWNER = {"id": "system:jobs", "kind": "system", "role": "system", "groups": [], "label": "jobs"}
PAUSE_REASON = "identity_revoked"


class IdentityRevoked(PermissionError):
    """The identity (and any fallback) was revoked or disabled: the run is
    blocked and the job paused."""


class IdentityUnavailable(PermissionError):
    """The identity cannot be resolved on this hub right now (no token store,
    no vault): the run is blocked; the job stays enabled."""


@dataclass(frozen=True)
class RunIdentity:
    mode: str
    id: str                 # the principal id the run acts as
    display: str            # envelope / journal identity: job:<id>/<principal>
    principal: Principal
    note: str = ""          # e.g. why the fallback was used
    guard: Any = field(default=None, compare=False, repr=False)  # .guardrails.JobGuard


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


def is_admin(info: dict | None) -> bool:
    """The operator / hub admin: band owners, operator-role tokens, the
    shared static token, the dashboard password and in-process hub code
    (the same principals ``require_hub_admin`` lets through)."""
    if not info:
        return False
    p = principal_of(info)
    return bool(p.verified) and p.fail_open()


def principal_of(info: dict, via: tuple = ()) -> Principal:
    return Principal(str(info.get("id") or SYSTEM_OWNER["id"]), str(info.get("kind") or "unknown"),
                     str(info.get("role") or ""), tuple(info.get("groups") or ()), via=via,
                     verified=bool(info.get("verified", True)), label=str(info.get("label") or ""))


# -- specs -------------------------------------------------------------------------

def parse_spec(spec: Any) -> tuple[str, str | None]:
    """An identity (string or ``{mode, ref}``) as ``(mode, ref)``; raises
    ValueError for a bad one."""
    if isinstance(spec, dict):
        mode = spec.get("mode") or "creator"
        ref = spec.get("ref")
    elif isinstance(spec, str) and spec.strip():
        s = spec.strip()
        if s == "creator":
            mode, ref = "creator", None
        elif s.startswith("{{") or s.startswith("vault:"):
            mode, ref = "vault", s[6:] if s.startswith("vault:") else s
        else:
            mode, ref = "key", s
    else:
        raise ValueError("an identity is \"creator\", a key or user reference, \"{{secret:name}}\" "
                         "or {\"mode\", \"ref\"}")
    if mode not in ("creator", "key", "vault"):
        raise ValueError(f"mode must be creator, key or vault, not {mode!r}")
    if mode == "key" and (not isinstance(ref, str) or not ref.strip()):
        raise ValueError("a key identity needs ref (token:<agent_id>, human:<id>, a key id or name)")
    if mode == "vault" and secret_name(ref) is None:
        raise ValueError("a vault identity needs ref: a secret name or {{secret:name}}")
    return mode, (ref.strip() if isinstance(ref, str) else None)


def check_block(ident: Any) -> list[str]:
    """Validation problems with a job's ``identity`` block."""
    if not isinstance(ident, dict):
        return [f"identity.mode: one of {', '.join(MODES)}"]
    errs = [f"identity.{k}: unknown field (use mode, ref, fallback)"
            for k in ident if k not in ("mode", "ref", "fallback")]
    mode = ident.get("mode", "creator")
    if mode not in MODES:
        return errs + [f"identity.mode: one of {', '.join(MODES)}"]
    if mode in ("key", "vault"):
        try:
            parse_spec({"mode": mode, "ref": ident.get("ref")})
        except ValueError as e:
            errs.append(f"identity.ref: {e}")
    if ident.get("fallback") is not None:
        try:
            parse_spec(ident["fallback"])
        except ValueError as e:
            errs.append(f"identity.fallback: {e}")
    return errs


def specs(ident: dict | None) -> list[tuple[str, str | None]]:
    """The identities a block names (primary, then fallback)."""
    ident = ident or {}
    out = []
    mode = ident.get("mode") or "creator"
    if mode in ("key", "vault"):
        out.append(parse_spec({"mode": mode, "ref": ident.get("ref")}))
    if ident.get("fallback") is not None:
        out.append(parse_spec(ident["fallback"]))
    return out


# -- resolution ------------------------------------------------------------------------

def _one(mode: str, ref: str | None, job: dict, owner_info: dict | None,
         directory: Directory | None) -> dict:
    if mode == "creator":
        info = owner_info or SYSTEM_OWNER
        if directory is not None:
            st = directory.status(info)
            if not st.active:
                raise IdentityRevoked(st.reason or f"{info.get('id')} was revoked")
        return info
    if directory is None:
        raise IdentityUnavailable(f"{mode} identities cannot be checked on this hub")
    if mode == "key":
        try:
            found = directory.lookup(ref)
        except ValueError as e:
            raise IdentityUnavailable(str(e)) from None
        if found is None:
            if directory.tokens is None and not str(ref).startswith("human:"):
                raise IdentityUnavailable("this hub cannot check API keys (no token store)")
            raise IdentityRevoked(f"no API key or user {ref!r} (revoked or deleted)")
    else:
        try:
            found = directory.from_vault(ref, f"job:{job.get('id')}")
        except (LookupError, ValueError) as e:
            raise IdentityUnavailable(str(e)) from None
    if not found.active:
        raise IdentityRevoked(found.reason or f"{ref} was revoked")
    return found.info


def resolve_identity(job: dict, owner_info: dict | None, *, directory: Directory | None = None,
                     settings: Callable[[str], Any] | None = None, guardrails: Any = None) -> RunIdentity:
    """The identity a run of ``job`` acts as. Raises :class:`IdentityRevoked`
    (pause the job) or :class:`IdentityUnavailable` (block this run) when
    there is none usable."""
    ident = job.get("identity") or {}
    mode = ident.get("mode") or "creator"
    if mode not in MODES:
        raise IdentityUnavailable(f"unknown identity mode {mode!r}")
    fb = ident.get("fallback")
    if fb is None and settings is not None:
        fb = settings("default_fallback") or None
    note = ""
    used = mode
    try:
        if mode == "fallback":
            if fb is None:
                raise IdentityUnavailable("identity mode fallback needs identity.fallback "
                                          "(or the hub setting job.default_fallback)")
            info = _one(*parse_spec(fb), job, owner_info, directory)
        else:
            info = _one(mode, ident.get("ref") if mode != "creator" else None, job, owner_info, directory)
    except IdentityRevoked as e:
        if fb is None or mode == "fallback":
            raise
        try:
            info = _one(*parse_spec(fb), job, owner_info, directory)
        except ValueError as bad:
            raise IdentityRevoked(f"{e}; the fallback is not usable ({bad})") from None
        except PermissionError as e2:
            raise IdentityRevoked(f"{e}; fallback: {e2}") from None
        used, note = "fallback", f"primary identity unusable ({e}); ran as the fallback"
    except ValueError as e:
        raise IdentityUnavailable(str(e)) from None
    pid = str(info.get("id") or SYSTEM_OWNER["id"])
    jid = f"job:{job.get('id')}"
    principal = principal_of(info, via=(jid,))
    guard = None
    if guardrails is not None:
        from .guardrails import JobGuard
        guard = JobGuard(guardrails, job, via=(pid,))
    return RunIdentity(used, pid, f"{jid}/{pid}", principal, note, guard)


# -- who may set an identity -------------------------------------------------------------

def authorize(ident: dict | None, who: dict, directory: Directory | None,
              previous: dict | None = None) -> None:
    """Raise PermissionError unless ``who`` may give a job this identity
    block: the operator may use any key or vault identity; anyone else only
    a key that *is* them (or that they own) and a vault secret holding their
    own key. Identities unchanged from ``previous`` are not re-checked."""
    if is_admin(who):
        return
    old = set()
    try:
        old = set(specs(previous))
    except ValueError:
        pass
    me = str(who.get("id") or "")
    for mode, ref in specs(ident):
        if mode == "creator" or (mode, ref) in old:
            continue
        found: Found | None = None
        if directory is not None:
            try:
                found = (directory.lookup(ref) if mode == "key"
                         else directory.from_vault(ref, f"{me} (setting a job identity)"))
            except (LookupError, ValueError):
                found = None
        if found is not None and found.info and (found.info.get("id") == me or (found.owner and found.owner == me)):
            continue
        what = f"API key or user {ref!r}" if mode == "key" else f"the key in vault secret {secret_name(ref)!r}"
        raise PermissionError(f"denied: only the operator or the principal itself may run a job as {what}")
