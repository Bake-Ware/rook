"""screen-android: the screen of the phone the Rook Android app runs on (brief AP5), shipped as a
plugin installed by default. screen.on, screen.keep_on, screen.status and screen.capture are 0.5's
(origin/castling rook/plugins/screen-android): the same names, arguments and replies.

screen.capture: calling it is all it takes (Bake, 26 November: "allow the agent to initiate screen
permissions"). Where the app's accessibility service is on (Android 11 and later), the picture is the
service's screenshot: no prompt, nothing to renew. Where it is off, the cap brings up Android's own
screen capture prompt on the phone itself, waits for the person, and answers with the picture; the
capture session that "Start" begins is kept (Android 14 makes each grant single-use), so later calls
ask nothing until the person stops it. The reply is 0.5's file reply, {name, mime, bytes, data} with
data in base64, and `via` says which way it was taken: "accessibility" or "media_projection". Apps
that block screenshots come out as Android gives them (an error, or black).

screen.on turns the screen on over the lock screen (dismiss_keyguard also brings up the PIN pad;
phone.unlock types the PIN). screen.keep_on holds it on for `minutes`, or with none until
screen.keep_on off. Opening a screen from the background needs Rook to display over other apps unless
its accessibility service is on; the first call that needs that asks for it (the app's Bridge).

Nothing here bounds a call (Bake, 17 November): 0.5's hold_ms default and its 100 to 60000 bounds,
screen.keep_on's 30-minute default and 1440 maximum, quality's 1 to 100 and every manifest timeout
are gone (a quality Android's JPEG does not take is refused as Android says). 0.5's passive keep-awake,
which held the screen for 10 idle minutes after every screen-driving call, is not here: such a call
turns the screen on if it is off, and the phone's own screen timeout applies after it.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import base64
import json

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

QUALITY = 85        # 0.5's JPEG quality when none is given


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _whole(args, name, low):
    value = args.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < low):
        raise ValueError(f"{name} must be a whole number, {low} or more")
    return value


def _app(name):
    return jclass("systems.bake.rook.core.worker." + name)


def _said(e):
    """A Java exception's own message."""
    return str(e.getMessage()) if hasattr(e, "getMessage") else str(e)


def _on(dismiss, hold_ms):
    refused = _app("Bridge").wake("screen.on", dismiss, hold_ms or 0)
    if refused is not None:
        raise ValueError(str(refused))
    return {"dismiss_keyguard": dismiss}


async def on(args):
    _check(args, "screen.on", ("dismiss_keyguard", "hold_ms"))
    return await asyncio.to_thread(_on, _flag(args, "dismiss_keyguard", False), _whole(args, "hold_ms", 1))


def _keep_on(on, minutes):
    awake = _app("Awake")
    if not on:
        return json.loads(str(awake.release()))
    _app("Bridge").awake("screen.keep_on")
    return json.loads(str(awake.hold(minutes or 0)))


async def keep_on(args):
    _check(args, "screen.keep_on", ("on", "minutes"), ("on",))
    return await asyncio.to_thread(_keep_on, _flag(args, "on", True), _whole(args, "minutes", 1))


async def status(args):
    _check(args, "screen.status", ())
    return await asyncio.to_thread(lambda: json.loads(str(_app("Awake").status())))


def _capture(quality):
    bridge, hands = _app("Bridge"), _app("Hands")
    bridge.awake("screen.capture")
    sdk = int(jclass("android.os.Build$VERSION").SDK_INT)
    try:
        if sdk >= 30 and hands.on():
            data, via = hands.screenshot(quality), "accessibility"
        else:
            refused = bridge.capture("screen.capture")
            if refused is not None:
                raise ValueError(str(refused))
            data, via = _app("Capture").jpeg(quality), "media_projection"
            if data is None:
                raise ValueError("the screen capture was stopped on the phone before the picture was taken; call again")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(_said(e)) from None
    data = bytes(data)
    return {"name": "screen.jpg", "mime": "image/jpeg", "bytes": len(data),
            "data": base64.b64encode(data).decode("ascii"), "via": via}


async def capture(args):
    _check(args, "screen.capture", ("quality",))
    quality = args.get("quality", QUALITY)
    if not isinstance(quality, int) or isinstance(quality, bool):
        raise ValueError("quality must be a whole number (JPEG quality, 0 to 100)")
    return await asyncio.to_thread(_capture, quality)


CAPS = {} if jclass is None else {
    "screen.on": Cap(
        "Turn the screen on, over the lock screen; dismiss_keyguard also brings up the PIN pad (it never bypasses a "
        "secure lock; phone.unlock types the PIN); hold_ms keeps the waking screen that long.",
        {"type": "object", "additionalProperties": False, "properties": {
            "dismiss_keyguard": {"type": "boolean", "default": False}, "hold_ms": {"type": "integer", "minimum": 1}}},
        on),
    "screen.keep_on": Cap(
        "Keep the screen on (so it never locks) for minutes, or with none until on=false; on=false lets it go now. Returns screen.status.",
        {"type": "object", "additionalProperties": False, "required": ["on"], "properties": {
            "on": {"type": "boolean"}, "minutes": {"type": "integer", "minimum": 1}}},
        keep_on),
    "screen.status": Cap(
        "The screen's state: screen_on, locked, keep-on (on, mode, until, remaining_s), the last screen-driving cap, the screen timeout.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
    "screen.capture": Cap(
        "The screen as a JPEG {name, mime, bytes, data (base64), via}: the accessibility service's screenshot, else "
        "Android's screen capture, whose prompt comes up on the phone the first time.",
        {"type": "object", "additionalProperties": False, "properties": {
            "quality": {"type": "integer", "description": "JPEG quality, 0 to 100; 85 when not given."}}},
        capture),
}
