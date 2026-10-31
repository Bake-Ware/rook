"""hid.*: gestures and text through systems.bake.rook.HidAccessibilityService."""

from rook.kit import android
from rook.kit._types import parse_duration
from rook.kit.android import screen
from rook.kit.ctx import KitError

KEYS = ("back", "home", "recents")


def _hid(cap):
    screen.awake(cap)
    return screen.hid()


def _done(ok, what):
    if not ok:
        raise KitError(f"{what} didn't go through (gestures need Android 7+)")
    return {}


def status(ctx):
    return {"backend": "android-accessibility", "ready": bool(android.app("HidAccessibilityService").isEnabled())}


def type_text(ctx, text, enter):
    b = _hid("hid.type")
    if enter:   # the focused field, replaced, then the keyboard's enter
        android.result(b.typeInto(None, None, False, -1, text, False, True))
        return {}
    if not b.typeText(text):
        raise KitError("no focused field to type into")
    return {}


def click(ctx, button, x=None, y=None):
    if x is None or y is None:
        raise KitError("a touch screen needs x and y")
    if button != "left":
        raise KitError("a touch screen has only the left button")
    return _done(_hid("hid.click").tap(x, y), "the tap")


def drag(ctx, x1, y1, x2, y2, duration, button):
    if button != "left":
        raise KitError("a touch screen has only the left button")
    ms = int(parse_duration(duration) * 1000)
    if not 50 <= ms <= 10000:
        raise KitError("duration is 50ms to 10s")
    return _done(_hid("hid.drag").swipe(x1, y1, x2, y2, ms), "the swipe")


def key(ctx, key, mods):
    name = key.strip().lower()
    if name not in KEYS or mods:
        raise KitError(f"this phone presses only {', '.join(KEYS)} (no modifiers)")
    b = _hid("hid.key")
    return _done(getattr(b, name)(), name)
