"""clock: timers and alarms in the clock app of the phone the Rook Android app runs on (brief AP3), shipped
as a plugin installed by default. clock.timer, clock.alarm and clock.alarms are 0.5's (origin/castling
rook/plugins/clock): the same names, arguments and replies. They are the clock app's own intents
(SET_TIMER, SET_ALARM, SHOW_ALARMS); the app holds com.android.alarm.permission.SET_ALARM, which Android
grants at install.

Nothing here bounds a call (Bake, 17 November): 0.5 capped a timer at a day and a label's length; neither
is kept. start=false only opens the clock app, set up, for a person to start or save, as in 0.5.

Opening the clock app from the background needs Rook to display over other apps on Android 10 and later:
the app's bridge (systems.bake.rook.core.worker.Bridge) asks for that the first time, as AP2's app caps do.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio

from rook_worker import Cap

try:
    from java import jclass, jint     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

EXTRA = "android.intent.extra.alarm."


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _whole(args, name, least, most=None, default=None):
    value = args.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < least or (most is not None and value > most):
        raise ValueError(f"{name} must be a whole number from {least}" + (f" to {most}" if most is not None else " up"))
    return value


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _label(args):
    value = args.get("label", "")
    if not isinstance(value, str):
        raise ValueError("label must be a string")
    return value


def _go(cap, action, extras=None):
    """Open the clock app with `action` and its extras: {app} names the app that took it."""
    bridge = jclass("systems.bake.rook.core.worker.Bridge")
    intent = jclass("android.content.Intent")(action)
    for name, value in (extras or {}).items():
        intent.putExtra(name, value if isinstance(value, (bool, str)) else jint(value))
    out = {}
    try:
        found = bridge.context().getPackageManager().resolveActivity(intent, 0)
        app = str(found.activityInfo.packageName) if found is not None else None
    except Exception:
        app = None
    if app and app != "android":
        out["app"] = app
    refused = bridge.start(intent, cap)
    if refused is not None:
        raise ValueError(str(refused))
    return out


def _labelled(extras, label):
    if label:
        extras[EXTRA + "MESSAGE"] = label
    return extras


async def timer(args):
    _check(args, "clock.timer", ("seconds", "label", "start"), ("seconds",))
    extras = _labelled({EXTRA + "LENGTH": _whole(args, "seconds", 1), EXTRA + "SKIP_UI": _flag(args, "start", True)},
                       _label(args))
    return await asyncio.to_thread(_go, "clock.timer", "android.intent.action.SET_TIMER", extras)


async def alarm(args):
    _check(args, "clock.alarm", ("hour", "minute", "label", "start"), ("hour",))
    extras = _labelled({EXTRA + "HOUR": _whole(args, "hour", 0, 23), EXTRA + "MINUTES": _whole(args, "minute", 0, 59, 0),
                        EXTRA + "SKIP_UI": _flag(args, "start", True)}, _label(args))
    return await asyncio.to_thread(_go, "clock.alarm", "android.intent.action.SET_ALARM", extras)


async def alarms(args):
    _check(args, "clock.alarms", ())
    return await asyncio.to_thread(_go, "clock.alarms", "android.intent.action.SHOW_ALARMS")


CAPS = {} if jclass is None else {
    "clock.timer": Cap(
        "Start a countdown timer of seconds in the phone's clock app; start=false only opens it, set up, for a person to start.",
        {"type": "object", "additionalProperties": False, "required": ["seconds"], "properties": {
            "seconds": {"type": "integer", "minimum": 1}, "label": {"type": "string", "default": ""},
            "start": {"type": "boolean", "default": True}}},
        timer),
    "clock.alarm": Cap(
        "Set an alarm in the phone's clock app at hour:minute (24-hour, phone-local); start=false only opens it, set up, "
        "for a person to save.",
        {"type": "object", "additionalProperties": False, "required": ["hour"], "properties": {
            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "minute": {"type": "integer", "minimum": 0, "maximum": 59, "default": 0},
            "label": {"type": "string", "default": ""}, "start": {"type": "boolean", "default": True}}},
        alarm),
    "clock.alarms": Cap(
        "Open the phone's clock app on its list of alarms.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        alarms),
}
