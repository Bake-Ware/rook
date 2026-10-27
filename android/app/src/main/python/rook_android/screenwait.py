"""Waiting on the screen and reading it compactly (ui.wait, ui.screen, screen=true).

Agents driving a phone used to act, read the screen, and act again: an
``ui.click`` straight after ``device.launch`` or ``device.open_url`` landed on
the old screen ("no visible node matches"), and every step cost one more call
to see what happened. These helpers let a cap wait for the screen to change and
settle, and hand back the compact summary from HidAccessibilityService
(ScreenModel.kt) in the same reply.

Every Java call runs in a thread (``asyncio.to_thread``): a tree walk can take
tens of milliseconds and the worker's event loop must keep its band link.
"""

from __future__ import annotations

import asyncio
import json
import time

try:
    from java import jclass
    _Hid = jclass("systems.bake.rook.HidAccessibilityService")
except Exception:  # pragma: no cover - only on a Chaquopy host
    _Hid = None

POLL_S = 0.15
#: After an action: how long to wait for the screen to start changing, and the
#: quiet time that counts as settled. Bounded overall by AFTER_MAX_S.
CHANGE_WAIT_S = 2.0
SETTLE_MS = 500
AFTER_MAX_S = 4.0
#: The longest any wait may run (ui.wait timeout, ui.click wait).
WAIT_MAX_S = 60.0


def hid():
    return _Hid


def _clock() -> float:
    return time.monotonic()


async def _call(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def rev(bridge=None) -> str | None:
    b = bridge or _Hid
    if b is None:
        return None
    try:
        r = await _call(b.screenRev)
        return str(r) if r is not None else None
    except Exception:
        return None


async def quiet_ms(bridge=None) -> int:
    b = bridge or _Hid
    try:
        return int(await _call(b.quietMs))
    except Exception:
        return -1


def _locked() -> bool | None:
    try:
        from rook_android.androidctx import app_context
        from rook_android.androidctx import jclass as jc
        ctx = app_context()
        if ctx is None or jc is None:
            return None
        Context = jc("android.content.Context")
        km = ctx.getSystemService(Context.KEYGUARD_SERVICE)
        return bool(km.isKeyguardLocked())
    except Exception:
        return None


async def summary(bridge=None, max_items: int = 60, max_text: int = 40) -> dict:
    """The compact screen: package/app, items to act on, other text, rev."""
    b = bridge or _Hid
    if b is None:
        return {"ok": False, "error": "not an Android host"}
    raw = await _call(b.screenSummary, int(max_items), int(max_text))
    if raw is None:
        return {"ok": False, "error": "accessibility service not enabled (grant it in the app)"}
    out: dict = {}
    try:
        app = await _call(b.screenApp)
        if app is not None and len(app) == 2:
            out = {"package": str(app[0]), "app": str(app[1])}
    except Exception:
        pass
    locked = _locked()
    if locked:
        out["locked"] = True
    out.update(json.loads(str(raw)))
    return out


async def settle_after(before: str | None, bridge=None, max_s: float = AFTER_MAX_S,
                       settle_ms: int = SETTLE_MS) -> dict:
    """After an action: wait until the screen differs from ``before`` (up to
    CHANGE_WAIT_S), then until it has been quiet for ``settle_ms``; never
    longer than ``max_s`` in all. Returns {changed, waited_ms}."""
    b = bridge or _Hid
    start = _clock()
    changed = False
    while _clock() - start < min(CHANGE_WAIT_S, max_s):
        now = await rev(b)
        if now is not None and now != before:
            changed = True
            break
        await asyncio.sleep(POLL_S)
    while _clock() - start < max_s:
        q = await quiet_ms(b)
        if q < 0 or q >= settle_ms:
            break
        await asyncio.sleep(POLL_S)
    if not changed:
        now = await rev(b)
        changed = now is not None and now != before
    return {"changed": changed, "waited_ms": int((_clock() - start) * 1000)}


async def with_screen(out: dict, before: str | None, bridge=None) -> dict:
    """Add ``screen`` (the summary after the screen settles) to an action's reply."""
    try:
        state = await settle_after(before, bridge)
        shot = await summary(bridge)
        shot.pop("ok", None)
        out["screen"] = {**shot, **state}
    except Exception as e:
        out["screen"] = {"error": f"{type(e).__name__}: {e}"}
    return out


async def until(check, timeout: float) -> tuple[bool, int]:
    """Poll ``check()`` (async, returns bool) until true or ``timeout`` seconds."""
    start = _clock()
    limit = max(0.0, min(float(timeout), WAIT_MAX_S))
    while True:
        if await check():
            return True, int((_clock() - start) * 1000)
        if _clock() - start >= limit:
            return False, int((_clock() - start) * 1000)
        await asyncio.sleep(POLL_S)
