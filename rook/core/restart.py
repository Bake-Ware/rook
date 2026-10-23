"""Restarting a hub process from the dashboard (Settings > Restart).

The hub never gains root (no sudoers rule, helper unit or polkit), so it
restarts the way a crash would: the process stops cleanly and exits with
:data:`EXIT_CODE`, and its service manager starts it again. That works for
systemd units with ``Restart=always`` or ``Restart=on-failure`` (what
scripts/install-hub.sh and ``rook hub deploy`` write). Anywhere else (a hub
run by hand, Docker, launchd) exiting would just stop it, so :func:`supervised`
says why and the restart is refused. ``ROOK_RESTART_SUPERVISED=1`` vouches for
a supervisor this module can't see (a Docker restart policy, runit).

Each process's main loop registers how to stop itself with :func:`on_restart`
and, once its loop has ended, exits with :data:`EXIT_CODE` if :func:`requested`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger("rook.core.restart")

#: EX_TEMPFAIL: a non-zero status, so ``Restart=on-failure`` restarts too.
EXIT_CODE = 75
#: Seconds between the request and the stop, so the reply gets out first.
DELAY = 0.5
RESTARTING_POLICIES = ("always", "on-failure")

_stoppers: list[Callable[[], object]] = []
_requested = False


def _unit() -> tuple[str, bool] | None:
    """``(unit, user_manager)`` of the systemd service this process runs in."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        path = line.split(":", 2)[-1]
        parts = [p for p in path.split("/") if p]
        services = [p for p in parts if p.endswith(".service")]
        if services:
            return services[-1], any(p.startswith("user@") for p in parts)
    return None


def supervised() -> str | None:
    """``None`` when exiting with :data:`EXIT_CODE` brings this process back,
    otherwise why it wouldn't."""
    if os.environ.get("ROOK_RESTART_SUPERVISED", "").strip().lower() in ("1", "true", "yes"):
        return None
    if not os.environ.get("INVOCATION_ID"):
        return ("this hub is not run by systemd, so nothing would start it again; restart it "
                "the way it was started (set ROOK_RESTART_SUPERVISED=1 if a supervisor such as "
                "a Docker restart policy brings it back)")
    unit = _unit()
    if unit is None:
        return "could not tell which systemd unit runs this hub; restart it with systemctl"
    name, user = unit
    argv = ["systemctl", *(["--user"] if user else []), "show", name, "--property=Restart",
            "--value"]
    try:
        policy = subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not read {name}'s restart policy ({e}); restart it with systemctl"
    if policy not in RESTARTING_POLICIES:
        return (f"{name} has Restart={policy or 'no'}, so systemd would not start it again; "
                f"restart it with systemctl, or set Restart=on-failure in the unit")
    return None


def on_restart(stop: Callable[[], object]) -> Callable[[], None]:
    """Register how this process stops itself; returns the unregister call."""
    _stoppers.append(stop)

    def unregister() -> None:
        if stop in _stoppers:
            _stoppers.remove(stop)
    return unregister


def available() -> bool:
    """True when this process registered a way to stop for a restart."""
    return bool(_stoppers)


def request(actor: str = "") -> None:
    """Stop this process for a restart, shortly (the caller's reply goes first).
    Callers check :func:`supervised` first."""
    global _requested
    if not _stoppers:
        raise RuntimeError("this process can't restart itself")
    _requested = True
    log.warning("restart requested%s: stopping, the service manager starts it again",
                f" by {actor}" if actor else "")

    def stop_all() -> None:
        for stop in list(_stoppers):
            try:
                result = stop()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                log.exception("restart: a stop step failed")
    asyncio.get_running_loop().call_later(DELAY, stop_all)


def requested() -> bool:
    return _requested


def exit_code() -> int:
    """What the process exits with once its loop has ended."""
    return EXIT_CODE if _requested else 0
