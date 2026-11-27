"""hid-android: taps, swipes, typing and the back, home and recents keys on the phone the Rook Android
app runs on (brief AP5), shipped as a plugin installed by default. hid.status, hid.type, hid.click,
hid.drag and hid.key are 0.5's (origin/castling rook/plugins/hid-android): the same names, arguments
and replies.

The app's accessibility service (systems.bake.rook.core.worker.Hands) does them, without root. The
first call that needs it while it is off takes the person to its page in Android's settings and waits
(the app's Bridge.hands); refused, the error says where to turn it on, and, on Android 13 and later,
how to allow it when Android has greyed it out for an app installed from a file. Each call first turns
the screen on if it is off (Bridge.awake), as 0.5's did; it does not hold it on (screen.keep_on does).

Nothing here bounds a call (Bake, 17 November): 0.5 capped text at 10000 characters, x and y at 20000
and a swipe at 50 ms to 10 s, and gave each manifest timeout; none of that is kept. A swipe's length is
`duration` (0.5's 300 ms when not given: how a swipe is drawn, not a limit). What Android refuses (a
gesture longer than it draws) is reported as Android says it.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import json
import re

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

KEYS = ("back", "home", "recents")
UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _pixel(args, name):
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a whole number of pixels, 0 or more")
    return value


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def seconds(value):
    """A duration: a number of seconds, or text such as 300ms, 1.5s, 1m or 1m30s (0.5's forms)."""
    if isinstance(value, bool):
        raise ValueError(f"duration: not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text)
    if not text or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"duration: not a duration: {value!r} (say 300ms, 1.5s or a number of seconds)")
    return sum(float(n) * UNITS[u] for n, u in parts)


def _bridge():
    return jclass("systems.bake.rook.core.worker.Bridge")


def _hands(cap):
    """The accessibility service for `cap`, asked for if it is off; the screen turned on if it is off."""
    refused = _bridge().hands(cap)
    if refused is not None:
        raise ValueError(str(refused))
    _bridge().awake(cap)
    return jclass("systems.bake.rook.core.worker.Hands")


def _done(ok, what):
    if not ok:
        raise ValueError(f"{what} did not go through: Android cancelled it")
    return {}


async def status(args):
    _check(args, "hid.status", ())
    return {"backend": "android-accessibility", "ready": bool(jclass("systems.bake.rook.core.worker.Hands").on())}


def _type(text, enter):
    hands = _hands("hid.type")
    if enter:       # the focused field, replaced, then the keyboard's enter (Android 11+)
        out = json.loads(str(hands.typeInto(None, None, False, -1, text, False, True)))
        if out.get("ok") is False:
            raise ValueError(out.get("error") or "the field refused the text")
        return {}
    if not hands.typeFocused(text):
        raise ValueError("no focused field to type into")
    return {}


async def type_text(args):
    _check(args, "hid.type", ("text", "enter"), ("text",))
    if not isinstance(args["text"], str):
        raise ValueError("text must be a string")
    return await asyncio.to_thread(_type, args["text"], _flag(args, "enter", False))


def _button(args):
    if args.get("button", "left") != "left":
        raise ValueError("a touch screen has only the left button")


async def click(args):
    _check(args, "hid.click", ("x", "y", "button"))
    if args.get("x") is None or args.get("y") is None:
        raise ValueError("a touch screen needs x and y")
    _button(args)
    x, y = _pixel(args, "x"), _pixel(args, "y")
    return await asyncio.to_thread(lambda: _done(_hands("hid.click").tap(x, y), "the tap"))


async def drag(args):
    _check(args, "hid.drag", ("x1", "y1", "x2", "y2", "duration", "button"), ("x1", "y1", "x2", "y2"))
    _button(args)
    points = [_pixel(args, name) for name in ("x1", "y1", "x2", "y2")]
    ms = round(seconds(args.get("duration", "300ms")) * 1000)
    if ms < 1:
        raise ValueError("duration must be more than 0: a swipe takes some time")
    return await asyncio.to_thread(lambda: _done(_hands("hid.drag").swipe(*points, ms), "the swipe"))


async def key(args):
    _check(args, "hid.key", ("key", "mods"), ("key",))
    name = str(args["key"]).strip().lower()
    if name not in KEYS or args.get("mods"):
        raise ValueError(f"this phone presses only {', '.join(KEYS)} (no modifiers)")
    return await asyncio.to_thread(lambda: _done(_hands("hid.key").press(name), name))


CAPS = {} if jclass is None else {
    "hid.status": Cap(
        "The input backend (android-accessibility) and whether it is on: {backend, ready}.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
    "hid.type": Cap(
        "Type text into the focused field (it replaces what the field holds); enter then presses the keyboard's enter (Android 11+).",
        {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string"},
            "enter": {"type": "boolean", "default": False, "description": "Press Enter after the text."}}},
        type_text),
    "hid.click": Cap(
        "Tap the screen at pixel (x, y), top-left origin (ui.screen gives each item's centre); a touch screen has only the left button.",
        {"type": "object", "additionalProperties": False, "properties": {
            "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}}},
        click),
    "hid.drag": Cap(
        "Swipe from (x1, y1) to (x2, y2) over duration (300ms when not given; a number of seconds, or 250ms, 1.5s).",
        {"type": "object", "additionalProperties": False, "required": ["x1", "y1", "x2", "y2"], "properties": {
            "x1": {"type": "integer", "minimum": 0}, "y1": {"type": "integer", "minimum": 0},
            "x2": {"type": "integer", "minimum": 0}, "y2": {"type": "integer", "minimum": 0},
            "duration": {"type": ["string", "number"], "default": "300ms"},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}}},
        drag),
    "hid.key": Cap(
        "Press back, home or recents (Android's global actions; no modifiers).",
        {"type": "object", "additionalProperties": False, "required": ["key"], "properties": {
            "key": {"type": "string"},
            "mods": {"type": "array", "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "meta"]}, "default": []}}},
        key),
}
