"""Passive keep-awake: screen-driving caps wake the phone and keep it awake.

worker_entry wraps every cap :func:`drives_screen` matches. Before the cap runs,
systems.bake.rook.KeepAwake restarts the idle timer (Settings: "Keep phone awake
while agent is working", default on, 10 idle minutes) and turns the screen on if
it is off. When that call starts the hold and the lock screen is up, it unlocks
with the PIN saved in the app (only if one is saved; never retried after a
failed automatic attempt). None of this ever fails the wrapped cap.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging

from rook_android.androidctx import app_context, jclass

log = logging.getLogger("rook.android.keepawake")

#: Cap namespaces / exact caps that need the screen on and awake.
SCREEN_PREFIXES = ("ui.", "hid.", "screenshot.")
SCREEN_CAPS = frozenset({"device.launch", "device.open_url", "device.intent"})
#: Status reads inside those namespaces that must not wake anything.
NOT_SCREEN = frozenset({"hid.backend"})
#: These manage the screen themselves; they only reset the idle timer.
NO_AUTO_UNLOCK = frozenset({"device.unlock", "device.wake"})

WAKE_WAIT_S = 1.5


def drives_screen(dotpath: str) -> bool:
    if dotpath in NOT_SCREEN:
        return False
    return dotpath.startswith(SCREEN_PREFIXES) or dotpath in SCREEN_CAPS or dotpath in NO_AUTO_UNLOCK


def _bridge():
    return jclass("systems.bake.rook.KeepAwake")


async def _unlock(automatic: bool) -> dict:
    from rook_android.unlock import unlock
    return await unlock(None, automatic=automatic)


async def before(dotpath: str) -> dict | None:
    """Engage passive keep-awake for one screen-driving call. Never raises."""
    ctx = app_context()
    if ctx is None:
        return None
    try:
        ka = _bridge()
        state = json.loads(str(await asyncio.to_thread(ka.activity, ctx, dotpath)))
        if not state.get("auto"):
            return state
        if state.get("woke"):
            # Give the display a moment so a screenshot/tap right after lands on a lit screen.
            for _ in range(int(WAKE_WAIT_S / 0.1)):
                if ka.screenOn(ctx):
                    break
                await asyncio.sleep(0.1)
        if (state.get("started") or state.get("woke")) and state.get("locked") \
                and state.get("auto_unlock") and dotpath not in NO_AUTO_UNLOCK:
            res = await _unlock(automatic=True)
            state["unlock"] = {"unlocked": bool(res.get("unlocked")),
                               **({"error": res["error"]} if res.get("error") else {})}
        return state
    except Exception:
        log.exception("keep-awake before %s failed", dotpath)
        return None


def wrap(dotpath: str, fn):
    """``fn`` with passive keep-awake in front; same signature and cap metadata."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            await before(dotpath)
            return await fn(*args, **kwargs)
    else:
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            await before(dotpath)
            return await asyncio.to_thread(fn, *args, **kwargs)
    return wrapped


def wrap_registry(registry) -> list[str]:
    """Wrap every screen-driving cap already registered. Returns their names."""
    done = []
    for dotpath in registry.list():
        if not drives_screen(dotpath):
            continue
        fn = registry.handler(dotpath)
        if fn is None or getattr(fn, "_rook_keepawake", False):
            continue
        w = wrap(dotpath, fn)
        w._rook_keepawake = True
        registry.register(dotpath, w, replace=True, meta=registry.meta(dotpath))
        done.append(dotpath)
    return done
