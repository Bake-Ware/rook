"""Failed-login throttle shared by every place the hub checks a secret.

Stdlib only. A hub exposed to the internet sees bots guessing the dashboard
password and MCP tokens; this slows them to a crawl without an external
service:

* failures are counted per client address in a sliding window
  (``max_failures`` in ``window`` seconds);
* reaching the limit locks the address out for ``lockout`` seconds, doubling
  with each further lockout up to ``lockout_max``;
* while locked out, callers answer 429 with ``Retry-After`` *without*
  checking the credential, so neither guessing nor timing gets anywhere;
* a successful sign-in clears the address;
* memory is bounded (``max_keys`` addresses, idle entries pruned).

One instance per process (:func:`shared`), so the dashboard and the MCP
server in ``rook hub serve`` count failures from the same address together.
It is guarded by a :class:`threading.Lock` and never awaits, so aiohttp
handlers, Starlette apps and worker threads can all call it.

Client addresses come from :func:`client_keys`: the socket peer, and the
proxy headers (``CF-Connecting-IP``, ``X-Forwarded-For``) only when the peer
is loopback, where cloudflared or a reverse proxy normally sits. IPv6
addresses count per /64, since one host usually holds a whole /64.

Limits come from the environment (core settings ``auth.login_*``, see
docs/operations/settings-reference.md); ``ROOK_LOGIN_MAX_FAILURES=0`` turns
the throttle off.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("rook.core.throttle")

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW = 600.0        # seconds the failures are counted over
DEFAULT_LOCKOUT = 30.0        # first lockout; doubles each time
DEFAULT_LOCKOUT_MAX = 900.0   # 15 minutes
DEFAULT_MAX_KEYS = 10000

#: Environment variables (core settings auth.login_*), name -> default.
ENV = {
    "ROOK_LOGIN_MAX_FAILURES": DEFAULT_MAX_FAILURES,
    "ROOK_LOGIN_WINDOW_SECS": DEFAULT_WINDOW,
    "ROOK_LOGIN_LOCKOUT_SECS": DEFAULT_LOCKOUT,
    "ROOK_LOGIN_LOCKOUT_MAX_SECS": DEFAULT_LOCKOUT_MAX,
}


def _ip(value: Any) -> Optional[ipaddress._BaseAddress]:
    try:
        addr = ipaddress.ip_address(str(value).strip().strip("[]"))
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped if mapped is not None else addr


def _key(addr: ipaddress._BaseAddress) -> str:
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def is_loopback(peer: Any) -> bool:
    addr = _ip(peer)
    return addr is not None and addr.is_loopback


def _forwarded_for(xff: str) -> Optional[str]:
    """The address the nearest proxy saw: the right-most entry that is not
    loopback. Entries left of it are whatever the client sent, so they are
    never used (a client could otherwise pick its own key)."""
    for part in reversed([p for p in xff.split(",") if p.strip()]):
        addr = _ip(part)
        if addr is None:
            return None
        if not addr.is_loopback:
            return _key(addr)
    return None


def client_keys(peer: Any, headers: Any = None) -> tuple[str, ...]:
    """Throttle keys for one request: the socket ``peer`` address, or, when
    the peer is loopback (a local proxy or tunnel), the client address the
    proxy reported in ``CF-Connecting-IP`` and/or ``X-Forwarded-For``. A
    request is locked out when any of its keys is, and a failure counts
    against all of them, so a forged header cannot dodge the address the
    proxy itself appended. Headers from a non-loopback peer are ignored."""
    addr = _ip(peer) if peer else None
    if addr is None:
        return ("unknown",)
    if not addr.is_loopback or headers is None:
        return (_key(addr),)
    keys: list[str] = []
    cf = _ip(headers.get("CF-Connecting-IP") or "")
    if cf is not None:
        keys.append(_key(cf))
    xff = _forwarded_for(headers.get("X-Forwarded-For") or "")
    if xff and xff not in keys:
        keys.append(xff)
    return tuple(keys) or (_key(addr),)


def client_ip(peer: Any, headers: Any = None) -> str:
    """The address :func:`client_keys` puts first (for logs and messages)."""
    return client_keys(peer, headers)[0]


def _as_keys(keys: "str | Iterable[str]") -> tuple[str, ...]:
    return (keys,) if isinstance(keys, str) else tuple(keys)


class _Entry:
    __slots__ = ("failures", "locked_until", "strikes", "last")

    def __init__(self) -> None:
        self.failures: deque = deque()
        self.locked_until = 0.0
        self.strikes = 0          # lockouts so far; each doubles the next
        self.last = 0.0           # last failure or lockout end, for pruning


class LoginThrottle:
    """Per-key failure counter with exponential lockouts. See the module
    docstring. ``max_failures <= 0`` disables it (nothing is ever locked)."""

    def __init__(self, max_failures: int = DEFAULT_MAX_FAILURES,
                 window: float = DEFAULT_WINDOW, lockout: float = DEFAULT_LOCKOUT,
                 lockout_max: float = DEFAULT_LOCKOUT_MAX,
                 max_keys: int = DEFAULT_MAX_KEYS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = int(max_failures)
        self.window = float(window)
        self.lockout = float(lockout)
        self.lockout_max = max(float(lockout_max), self.lockout)
        self.max_keys = max(1, int(max_keys))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._pruned = 0.0

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # -- queries ---------------------------------------------------------------

    def retry_after(self, keys: "str | Iterable[str]") -> int:
        """Whole seconds until every one of ``keys`` may try again; 0 when
        none is locked out."""
        if not self.enabled:
            return 0
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in _as_keys(keys):
                entry = self._entries.get(key)
                if entry is not None and entry.locked_until > now:
                    wait = max(wait, entry.locked_until - now)
        return int(math.ceil(wait)) if wait > 0 else 0

    # -- updates ---------------------------------------------------------------

    def failure(self, keys: "str | Iterable[str]", what: str = "sign-in") -> int:
        """Record one failed attempt for ``keys``. Returns the lockout in
        seconds when this failure starts one (0 otherwise); the warning is
        logged here, once per lockout."""
        if not self.enabled:
            return 0
        now = self._clock()
        started = 0.0
        with self._lock:
            for key in _as_keys(keys):
                entry = self._entries.get(key)
                if entry is None:
                    entry = self._entries[key] = _Entry()
                else:
                    self._entries.move_to_end(key)
                if entry.locked_until > now:
                    continue          # already locked: nothing more to count
                if entry.last and self._forgotten(entry, now):
                    entry = self._entries[key] = _Entry()   # a quiet spell resets strikes
                entry.last = now
                while entry.failures and entry.failures[0] <= now - self.window:
                    entry.failures.popleft()
                entry.failures.append(now)
                # After a lockout has run out, one more failure locks again
                # (for twice as long) until a success or a quiet spell.
                if len(entry.failures) >= self.max_failures or entry.strikes:
                    secs = min(self.lockout * (2 ** min(entry.strikes, 32)), self.lockout_max)
                    entry.strikes += 1
                    entry.locked_until = entry.last = now + secs
                    entry.failures.clear()
                    log.warning("login throttle: %s locked out for %d s after repeated "
                                "failed %s attempts", key, int(secs), what)
                    started = max(started, secs)
            self._trim(now)
        return int(math.ceil(started)) if started else 0

    def success(self, keys: "str | Iterable[str]") -> None:
        """A credential checked out: forget the failures of ``keys``."""
        if not self._entries:
            return
        with self._lock:
            for key in _as_keys(keys):
                self._entries.pop(key, None)

    # -- memory ----------------------------------------------------------------

    def _forgotten(self, entry: _Entry, now: float) -> bool:
        """Idle long enough that it no longer matters: not locked, and no
        failure (or lockout end) for ``max(window, lockout_max)`` seconds."""
        return (entry.locked_until <= now
                and now - entry.last > max(self.window, self.lockout_max))

    def _trim(self, now: float) -> None:
        """Called with the lock held. Prune idle entries once a minute or
        when over the cap; then drop the least recently touched entries."""
        if len(self._entries) > self.max_keys or now - self._pruned > 60:
            self._pruned = now
            for key in [k for k, e in self._entries.items() if self._forgotten(e, now)]:
                del self._entries[key]
        while len(self._entries) > self.max_keys:
            self._entries.popitem(last=False)


def _env_number(name: str, default: float, cast: Callable[[str], float]) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < 0:
        log.warning("%s=%r is negative; using %s", name, raw, default)
        return default
    return value


def from_env() -> LoginThrottle:
    """A throttle with the limits from the environment (``ENV``)."""
    return LoginThrottle(
        max_failures=int(_env_number("ROOK_LOGIN_MAX_FAILURES", DEFAULT_MAX_FAILURES, int)),
        window=_env_number("ROOK_LOGIN_WINDOW_SECS", DEFAULT_WINDOW, float) or DEFAULT_WINDOW,
        lockout=_env_number("ROOK_LOGIN_LOCKOUT_SECS", DEFAULT_LOCKOUT, float) or DEFAULT_LOCKOUT,
        lockout_max=_env_number("ROOK_LOGIN_LOCKOUT_MAX_SECS", DEFAULT_LOCKOUT_MAX, float)
        or DEFAULT_LOCKOUT_MAX,
    )


_shared: Optional[LoginThrottle] = None
_shared_lock = threading.Lock()


def shared() -> LoginThrottle:
    """The process-wide throttle, created from the environment on first use."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = from_env()
    return _shared


def message(seconds: int) -> str:
    """The plain message a locked-out caller sees."""
    return f"Too many attempts, try again in {int(seconds)} seconds."
