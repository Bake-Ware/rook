"""Per-call audit attribution for the band MCP — who made this call.

Every MCP tool call is attributed from the bearer token the transport already
authenticated. There is no second authorization layer: no task/attempt IDs, no
approvals, no band lookup, no capability allowlist. This module only answers
"who is this?", and it keeps two failure modes strictly apart:

* **The token is missing or not valid** (unknown, revoked, expired) →
  :class:`Unauthenticated`. The caller denies *that one call*. This is the same
  decision the existing token layer (``StoreTokenVerifier`` → HTTP 401) makes;
  the per-call re-check only closes the window where a token is revoked
  mid-session, and makes sure an unidentifiable call can never execute.

* **The checker itself cannot work** (auth context unreadable, token store
  raised, no request to read) → an *unverified* :class:`Attribution`. The call
  proceeds and the caller raises an alert. A broken checker must never turn
  into a blanket denial of every legitimate caller — that is exactly how the
  349e3eb knowledge guard bricked the band.

``resolve`` never consults bands, workers or capabilities, so nothing about
fleet state can change its answer.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Callable

log = logging.getLogger("rook.band_mcp.attribution")


class Unauthenticated(Exception):
    """The call carried no valid bearer token. Deny this call only."""


@dataclass(frozen=True)
class Attribution:
    identity: str            # display identity stamped on band calls / chat
    kind: str                # "agent" | "shared" | "unverified"
    label: str | None = None
    agent_id: str | None = None
    key_id: str | None = None
    verified: bool = True
    reason: str | None = None  # why attribution is unverified
    # Compound agent identity (docs/DESIGN-agent-work-system.md §1):
    # <token>.<client>.<host>@<dir with / as .>, all observed, never supplied.
    token: str | None = None
    client: str | None = None
    host: str | None = None
    dir: str | None = None
    actor: str | None = None

    def audit(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def norm(value: str | None, fallback: str) -> str:
    """Lowercase letters and digits only: 'Claude Code' -> 'claudecode'."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower()) or fallback


def dir_part(path: str | None) -> str | None:
    """'/home/you/rook' -> '.home.you.rook' (safe characters only)."""
    if not path:
        return None
    return re.sub(r"[^A-Za-z0-9._-]", "", path.rstrip("/").replace("/", ".")) or None


def compound(att: "Attribution", client: str | None, host: str | None,
             dir_: str | None) -> "Attribution":
    """Fill the compound identity fields. Unverified callers stay 'unverified'."""
    from dataclasses import replace
    if not att.verified:
        return replace(att, actor="unverified")
    token = norm(att.label, "static")
    c, h, d = norm(client, "unknown"), norm(host, "web"), dir_part(dir_)
    name = f"{token}.{c}.{h}" + (f"@{d}" if d else "")
    return replace(att, token=token, client=c, host=h, dir=dir_ or None, actor=name)


# Attribution of the MCP tool call currently executing (set per call).
current: ContextVar[Attribution | None] = ContextVar("rook_attribution", default=None)


def _unverified(reason: str) -> Attribution:
    return Attribution(identity="unverified", kind="unverified",
                       verified=False, reason=reason)


def _bearer(request: Any) -> str:
    header = (request.headers.get("authorization") or "").strip()
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def resolve(store: Any,
            get_token: Callable[[], str | None],
            get_request: Callable[[], Any],
            host: str = "") -> Attribution:
    """Attribute one call. Raises :class:`Unauthenticated` only when a token
    was actually examined and found missing or invalid; any failure of the
    machinery itself returns an unverified attribution instead."""
    try:
        raw = get_token()
    except Exception as e:  # noqa: BLE001 — checker failure, not a bad token
        return _unverified(f"auth context unreadable: {type(e).__name__}")
    if not raw:
        # The SDK normally carries the verified token in a contextvar. If it
        # is absent, fall back to the request's own header rather than guess.
        try:
            request = get_request()
        except Exception as e:  # noqa: BLE001
            return _unverified(f"request unreadable: {type(e).__name__}")
        if request is None:
            return _unverified("no auth context and no HTTP request")
        raw = _bearer(request)
        if not raw:
            raise Unauthenticated("missing bearer token")
    try:
        principal = store.principal_for(raw)
    except Exception as e:  # noqa: BLE001
        return _unverified(f"token lookup failed: {type(e).__name__}")
    if principal is None:
        raise Unauthenticated("invalid, revoked or expired bearer token")
    label = principal.get("label") or "api"
    return Attribution(
        identity=f"agent:{label}_{host}" if host else f"agent:{label}",
        kind=principal.get("kind") or "agent", label=label,
        agent_id=principal.get("agent_id"), key_id=principal.get("key_id"))


class Alerter:
    """Loud, rate-limited alert for calls let through unverified.

    Logs at ERROR with a greppable ``ROOK AUDIT ALERT`` prefix. The journal row
    is written by the caller. Telegram delivery is deliberately not wired yet;
    add a ``notify`` callable here when it is. Never raises.
    """

    def __init__(self, interval: float = 60.0,
                 notify: Callable[[str], None] | None = None) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._notify = notify

    def __call__(self, tool: str, attribution: Attribution) -> None:
        try:
            reason = attribution.reason or "unknown"
            now = time.monotonic()
            if now - self._last.get(reason, -1e9) < self._interval:
                self._suppressed[reason] = self._suppressed.get(reason, 0) + 1
                return
            skipped = self._suppressed.pop(reason, 0)
            self._last[reason] = now
            msg = (f"ROOK AUDIT ALERT: MCP call {tool!r} ran with UNVERIFIED "
                   f"attribution ({reason}); let through, not denied. "
                   f"{skipped} similar call(s) suppressed in the last "
                   f"{self._interval:.0f}s.")
            log.error(msg)
            if self._notify:
                self._notify(msg)
        except Exception:  # noqa: BLE001 — alerting must never affect the call
            log.exception("audit alert failed")
