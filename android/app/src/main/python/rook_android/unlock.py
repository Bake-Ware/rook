"""PIN unlock through the accessibility service (device.unlock, passive keep-awake).

Wakes the screen, surfaces the PIN bouncer and taps the digits at keypad
positions computed from the live screen size. This doesn't bypass anything: it
enters the real PIN the way a finger would, so it only works with the correct
PIN and where the OS lets the accessibility service tap on the keyguard
(verified on Samsung / One UI).

The PIN is either passed per call or the one saved in the app's Settings
screen. A saved PIN stays in Kotlin (systems.bake.rook.UnlockPin, Keystore
encrypted): Python only asks it to type the digits, and the result never
carries digits or tap positions.
"""

from __future__ import annotations

import asyncio
import json

from rook_android.androidctx import app_context, jclass, cast


def _cls(name: str):
    return jclass(f"systems.bake.rook.{name}")


def _keypad(w: int, h: int) -> tuple[list[int], list[int]]:
    """Samsung/One UI portrait PIN bouncer: 3 columns; rows 1-3, 4-6, 7-9, then 0."""
    return ([round(0.25 * w), round(0.5 * w), round(0.75 * w)],
            [round(0.641 * h), round(0.718 * h), round(0.795 * h), round(0.872 * h)])


def _center(b) -> tuple[int, int]:
    return (int(b[0]) + int(b[2])) // 2, (int(b[1]) + int(b[3])) // 2


def _live_keypad(Hid) -> tuple[list[int], list[int]] | None:
    """Keypad from the bouncer's own key nodes (…:id/key0-9), which One UI moves
    around (centered vs. bottom); None when the pad isn't on screen."""
    try:
        nodes = json.loads(str(Hid.screenNodes(True, 3000) or "[]"))
    except Exception:
        return None
    keys = {}
    for n in nodes:
        nid = str(n.get("id", ""))
        tail = nid.rsplit("/", 1)[-1]
        if len(tail) == 4 and tail.startswith("key") and tail[3].isdigit() and n.get("bounds"):
            keys[tail[3]] = _center(n["bounds"])
    if not all(str(d) in keys for d in range(10)):
        return None
    return ([keys["1"][0], keys["2"][0], keys["3"][0]],
            [keys["1"][1], keys["4"][1], keys["7"][1], keys["0"][1]])


def _press_ok(Hid) -> bool:
    """Tap the bouncer's OK key (PINs without auto-confirm need it)."""
    try:
        for n in json.loads(str(Hid.screenNodes(True, 3000) or "[]")):
            if str(n.get("id", "")).endswith(":id/key_enter_text") and n.get("bounds"):
                return bool(Hid.tap(*_center(n["bounds"])))
    except Exception:
        pass
    return False


def pin_set() -> bool:
    ctx = app_context()
    try:
        return bool(ctx is not None and _cls("UnlockPin").isSet(ctx))
    except Exception:
        return False


async def unlock(pin: str | None = None, hold_ms: int = 9000, pre_swipe: bool = True,
                 automatic: bool = False) -> dict:
    """Unlock with ``pin``, or with the stored PIN when ``pin`` is None."""
    ctx = app_context()
    if ctx is None:
        return {"ok": False, "error": "not an Android host"}
    stored = pin is None
    if not stored:
        pin = str(pin).strip()
        if not pin or not pin.isdigit():
            return {"ok": False, "error": "pin must be digits only"}
    UnlockPin = _cls("UnlockPin")
    if stored and not UnlockPin.isSet(ctx):
        return {"ok": False, "error": "no unlock PIN stored: set it in the Rook app "
                                      "(Settings > App > Device unlock PIN) or pass pin"}
    try:
        Hid = _cls("HidAccessibilityService")
        if not Hid.isEnabled():
            return {"ok": False, "error": "accessibility (HID) not enabled — grant it in the app"}
        Settings = jclass("android.provider.Settings")
        if not Settings.canDrawOverlays(ctx):
            return {"ok": False, "error": "grant 'Display over other apps' first"}

        # Live screen size → keypad coordinate model.
        Context = jclass("android.content.Context")
        wm = cast(jclass("android.view.WindowManager"), ctx.getSystemService(Context.WINDOW_SERVICE))
        DisplayMetrics = jclass("android.util.DisplayMetrics")
        m = DisplayMetrics()
        wm.getDefaultDisplay().getRealMetrics(m)
        w, h = int(m.widthPixels), int(m.heightPixels)

        # Wake the screen only. The wake window shows over the keyguard, so while it
        # is up it covers the PIN pad (and hides it from the node tree): keep it short
        # and let it close before surfacing the pad.
        Intent = jclass("android.content.Intent")
        ComponentName = jclass("android.content.ComponentName")
        i = Intent()
        i.setComponent(ComponentName(ctx, "systems.bake.rook.WakeActivity"))
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        i.putExtra("dismiss", False)
        i.putExtra("hold_ms", 300)
        ctx.startActivity(i)
        await asyncio.sleep(1.0)

        # Swipe up to bring the pad over the clock; retry once if it isn't there.
        live = _live_keypad(Hid)
        for _ in range(2 if pre_swipe else 0):
            if live:
                break
            try:
                Hid.swipe(int(w * 0.5), int(h * 0.85), int(w * 0.5), int(h * 0.3), 250)
            except Exception:
                pass
            await asyncio.sleep(0.8)
            live = _live_keypad(Hid)
        colx, rowy = live or _keypad(w, h)
        if stored:
            typed = json.loads(str(await asyncio.to_thread(
                UnlockPin.tapStored, ctx, *colx, *rowy, 280)))
            if not typed.get("ok") and not typed.get("taps_ok"):
                return typed
            out = {"ok": True, "pin": "stored", "screen": {"w": w, "h": h},
                   "digits": typed.get("digits"), "taps_ok": typed.get("taps_ok")}
        else:
            pos = {str(d): (colx[(d - 1) % 3], rowy[(d - 1) // 3]) for d in range(1, 10)}
            pos["0"] = (colx[1], rowy[3])
            taps = []
            for ch in pin:
                x, y = pos[ch]
                ok = bool(Hid.tap(int(x), int(y)))
                taps.append({"d": ch, "x": x, "y": y, "ok": ok})
                await asyncio.sleep(0.28)
            out = {"ok": True, "screen": {"w": w, "h": h}, "digits": len(pin), "taps": taps}

        out["keypad"] = "live" if live else "model"
        await asyncio.sleep(1.5)  # 4/6-digit PINs auto-submit
        KeepAwake = _cls("KeepAwake")
        unlocked = not bool(KeepAwake.keyguardLocked(ctx))
        if not unlocked and _press_ok(Hid):
            await asyncio.sleep(1.5)
            unlocked = not bool(KeepAwake.keyguardLocked(ctx))
            out["pressed_ok"] = True
        out["unlocked"] = unlocked
        if stored:
            UnlockPin.noteUnlockResult(ctx, bool(automatic), unlocked)
            if not unlocked and automatic:
                out["note"] = ("still locked; automatic unlock is paused until the PIN is "
                               "saved again or device.unlock succeeds")
        if not unlocked:
            out["note"] = out.get("note") or "still locked: a variable-length PIN may need OK; check ui.text"
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
