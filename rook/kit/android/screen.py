"""The phone's screen for its plugins: read it, wait on it, keep it awake, unlock it.

Reading goes through the app's accessibility service
(``systems.bake.rook.HidAccessibilityService``, its ScreenModel summary).
Everything here blocks; plugin code runs in a worker thread, so that is fine.

    b = screen.hid()                       # the service, or KitError when it is off
    before = screen.rev(b)
    ... act ...
    out["screen"] = screen.after(b, before)  # waits for the change to settle, then the summary

Keep-awake: a cap that drives the screen calls ``awake(cap)`` first. The app
restarts its idle timer (Settings: "Keep phone awake while agent is working")
and turns the screen on; when that starts a hold on a locked phone it types
the PIN saved in the app. It never fails the cap.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from rook.kit.ctx import KitError

from . import app, cast, context, jclass

log = logging.getLogger("rook.android.screen")

POLL_S = 0.15
#: After an action: how long to wait for the screen to start changing, and the
#: quiet time that counts as settled, bounded overall by AFTER_MAX_S.
CHANGE_WAIT_S = 2.0
SETTLE_MS = 500
AFTER_MAX_S = 4.0
#: Accessibility events arrive up to ~100 ms late: quiet isn't trusted sooner.
AFTER_MIN_S = 0.3
WAIT_MAX_S = 60.0
WAKE_WAIT_S = 1.5
OFF = "accessibility service not enabled (allow it in the Rook app)"
APP_WAKE = "systems.bake.rook.WakeActivity"


def hid() -> Any:
    b = app("HidAccessibilityService")
    if not b.isEnabled():
        raise KitError(OFF)
    return b


def rev(b: Any) -> str | None:
    try:
        r = b.screenRev()
        return str(r) if r is not None else None
    except Exception:
        return None


def front(b: Any) -> dict:
    """``{package, app}`` of the app in front; {} while no window is up."""
    try:
        got = b.screenApp()
        if got is not None and len(got) == 2:
            return {"package": str(got[0]), "app": str(got[1])}
    except Exception:
        pass
    return {}


def shown(b: Any) -> bool:
    try:
        return b.screenApp() is not None
    except Exception:
        return True


def quiet_ms(b: Any) -> int:
    try:
        return int(b.quietMs())
    except Exception:
        return -1


def locked() -> bool | None:
    try:
        Context = jclass("android.content.Context")
        return bool(context().getSystemService(Context.KEYGUARD_SERVICE).isKeyguardLocked())
    except Exception:
        return None


def summary(b: Any, limit: int = 60, text_limit: int = 40) -> dict:
    """The screen, compactly: app, items to act on (each with ``text`` and its
    centre ``x``, ``y`` for hid.click, as the ui.screen contract has it), the
    other text top to bottom, focus, locked, rev."""
    raw = b.screenSummary(int(limit), int(text_limit))
    if raw is None:
        raise KitError(OFF)
    shot = json.loads(str(raw))
    out = front(b)
    # An app label read a moment apart from another app's items is dropped.
    if shot.get("package") and out.get("package") not in (None, shot["package"]):
        out.pop("app", None)
    if locked():
        out["locked"] = True
    out.update(shot)
    out.setdefault("app", out.get("package", ""))
    out["items"] = [_item(i) for i in shot.get("items") or ()]
    return out


def _item(i: dict) -> dict:
    at = i.get("at") or (None, None)
    out = {"text": i.get("label", ""), **{k: v for k, v in i.items() if k not in ("label", "at")}}
    if at[0] is not None:
        out["x"], out["y"] = int(at[0]), int(at[1])
    return out


def until(check: Callable[[], bool], timeout: float) -> tuple[bool, int]:
    """Poll ``check()`` until true or ``timeout`` seconds (at most WAIT_MAX_S)."""
    start = time.monotonic()
    limit = max(0.0, min(float(timeout), WAIT_MAX_S))
    while True:
        if check():
            return True, int((time.monotonic() - start) * 1000)
        if time.monotonic() - start >= limit:
            return False, int((time.monotonic() - start) * 1000)
        time.sleep(POLL_S)


def settle(b: Any, before: str | None, before_app: str | None = None) -> dict:
    """After an action: wait until the screen differs from ``before`` (or,
    with ``before_app``, another app is in front: a ticking clock changes the
    old screen too), then until it has been quiet SETTLE_MS; never longer than
    AFTER_MAX_S. Returns ``{changed, settled, waited_ms}``."""
    start = time.monotonic()
    elapsed = lambda: time.monotonic() - start   # noqa: E731
    changed = settled = False
    while elapsed() < CHANGE_WAIT_S:
        if before_app is not None:
            pkg = front(b).get("package")
            changed = pkg is not None and pkg != before_app
        else:
            now = rev(b)
            changed = now is not None and now != before and shown(b)
        if changed:
            break
        time.sleep(POLL_S)
    while elapsed() < AFTER_MAX_S:
        q = quiet_ms(b)
        if q < 0 or (q >= SETTLE_MS and elapsed() >= AFTER_MIN_S and shown(b)):
            settled = True
            break
        time.sleep(POLL_S)
    if not changed:
        now = rev(b)
        changed = now is not None and now != before
    return {"changed": changed, "settled": settled, "waited_ms": int(elapsed() * 1000)}


def after(b: Any, before: str | None, before_app: str | None = None) -> dict:
    """The screen once an action's change has settled (a reply's ``screen``)."""
    try:
        state = settle(b, before, before_app)
        return {**summary(b), **state}
    except Exception as e:
        return {"error": getattr(e, "message", None) or f"{type(e).__name__}: {e}"}


# -- keep-awake and unlock ------------------------------------------------------

def awake(cap: str, unlock_after: bool = True) -> dict | None:
    """Passive keep-awake before a screen-driving cap. Never raises."""
    try:
        ctx = context()
        ka = app("KeepAwake")
        state = json.loads(str(ka.activity(ctx, cap)))
        if not state.get("auto"):
            return state
        if state.get("woke"):   # a moment for the display, so a tap right after lands on a lit screen
            for _ in range(int(WAKE_WAIT_S / 0.1)):
                if ka.screenOn(ctx):
                    break
                time.sleep(0.1)
        if unlock_after and (state.get("started") or state.get("woke")) and state.get("locked") \
                and state.get("auto_unlock"):
            try:
                state["unlock"] = {"unlocked": unlock(automatic=True)["unlocked"]}
            except KitError as e:
                state["unlock"] = {"unlocked": False, "error": e.message}
        return state
    except Exception:
        log.exception("keep-awake before %s failed", cap)
        return None


def _keypad(w: int, h: int) -> tuple[list[int], list[int]]:
    """Samsung/One UI portrait PIN bouncer: 3 columns; rows 1-3, 4-6, 7-9, then 0."""
    return ([round(0.25 * w), round(0.5 * w), round(0.75 * w)],
            [round(0.641 * h), round(0.718 * h), round(0.795 * h), round(0.872 * h)])


def wake(dismiss_keyguard: bool, hold_ms: int) -> None:
    """Screen on, over the keyguard (WakeActivity); with ``dismiss_keyguard``
    the PIN pad comes up. Needs 'Display over other apps'."""
    ctx = context()
    if not jclass("android.provider.Settings").canDrawOverlays(ctx):
        raise KitError("allow 'Display over other apps' in the Rook app first")
    Intent = jclass("android.content.Intent")
    i = Intent()
    i.setComponent(jclass("android.content.ComponentName")(ctx, APP_WAKE))
    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    i.putExtra("dismiss", bool(dismiss_keyguard))
    i.putExtra("hold_ms", int(hold_ms))
    ctx.startActivity(i)



def unlock(pin: str | None = None, hold_ms: int = 9000, pre_swipe: bool = True,
           automatic: bool = False) -> dict:
    """Type a PIN on the lock screen the way a finger would: wake, bring up
    the pad, tap the digits at positions from the live screen size. ``pin``
    None uses the one saved in the app, which never leaves Kotlin. The reply
    never carries digits or tap positions. Raises KitError."""
    ctx = context()
    pin_store = app("UnlockPin")
    if pin is None:
        if not pin_store.isSet(ctx):
            raise KitError("no unlock PIN saved: set it in the Rook app (Settings > App > Device unlock PIN)")
    elif not (pin.isdigit() and 4 <= len(pin) <= 16):
        raise KitError("pin must be 4-16 digits")
    b = hid()
    m = jclass("android.util.DisplayMetrics")()
    service_wm = cast("android.view.WindowManager",
                      ctx.getSystemService(jclass("android.content.Context").WINDOW_SERVICE))
    service_wm.getDefaultDisplay().getRealMetrics(m)
    w, h = int(m.widthPixels), int(m.heightPixels)
    wake(True, hold_ms)
    time.sleep(1.3)   # the screen wakes and the bouncer comes up
    if pre_swipe:     # setups that show the clock before the pad
        try:
            b.swipe(int(w * 0.5), int(h * 0.85), int(w * 0.5), int(h * 0.2), 200)
            time.sleep(0.5)
        except Exception:
            pass
    cols, rows = _keypad(w, h)
    if pin is None:
        typed = json.loads(str(pin_store.tapStored(ctx, *cols, *rows, 280)))
        if not typed.get("ok") and not typed.get("taps_ok"):
            raise KitError(str(typed.get("error") or "the saved PIN wasn't typed"))
        taps_ok = bool(typed.get("taps_ok", True))
    else:
        taps_ok = True
        for ch in pin:
            d = int(ch)
            x, y = (cols[1], rows[3]) if d == 0 else (cols[(d - 1) % 3], rows[(d - 1) // 3])
            taps_ok = bool(b.tap(int(x), int(y))) and taps_ok
            time.sleep(0.28)
    time.sleep(1.5)   # 4/6-digit PINs submit themselves
    unlocked = not bool(app("KeepAwake").keyguardLocked(ctx))
    out = {"unlocked": unlocked, "pin": "saved" if pin is None else "given", "taps_ok": taps_ok}
    if pin is None:
        pin_store.noteUnlockResult(ctx, bool(automatic), unlocked)
        if not unlocked and automatic:
            out["note"] = "still locked; automatic unlock is paused until the PIN is saved again or phone.unlock works"
    if not unlocked:
        out.setdefault("note", "still locked: a PIN of another length may need OK; check ui.screen")
    return out


__all__ = ["after", "awake", "front", "hid", "locked", "quiet_ms", "rev", "settle", "shown", "summary",
           "unlock", "until", "wake"]
