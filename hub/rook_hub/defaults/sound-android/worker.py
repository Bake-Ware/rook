"""sound-android: volume, ringer and Do Not Disturb of the phone the Rook Android app runs on (brief
AP2), shipped as a plugin installed by default. sound.status, sound.set and sound.dnd are 0.5's
(origin/castling rook/plugins/sound-android): the same names, arguments and replies. 0.5 bounded
step to 30 either way; that is not kept (Bake, 17 November).

Changing Do Not Disturb, and silencing the ringer, need Android's Do Not Disturb access for the app
(android.permission.ACCESS_NOTIFICATION_POLICY), a switch the person turns on in Android's settings.
Where Android refuses a change for want of it, the cap asks for it through the app's bridge (the
settings page opens, or a notification that opens it when the app is not in front), waits for the
person, and makes the change once they have turned it on. Refused, the error says how to turn it on.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

STREAMS = {"call": 0, "system": 1, "ring": 2, "media": 3, "alarm": 4, "notification": 5}   # AudioManager.STREAM_*
RINGER = {0: "silent", 1: "vibrate", 2: "normal"}
DND = {0: "unknown", 1: "off", 2: "priority", 3: "total", 4: "alarms"}   # INTERRUPTION_FILTER_*
ACCESS = "android.permission.ACCESS_NOTIFICATION_POLICY"


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _choice(args, name, choices, default=None):
    value = args.get(name, default)
    if value is not None and value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}")
    return value


def _number(args, name, least=None, most=None):
    value = args.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                              or (least is not None and value < least) or (most is not None and value > most)):
        span = f" from {least} to {most}" if least is not None else ""
        raise ValueError(f"{name} must be a whole number{span}")
    return value


def _managers():
    Context = jclass("android.content.Context")
    context = jclass("systems.bake.rook.core.worker.Bridge").context()
    return (cast(jclass("android.media.AudioManager"), context.getSystemService(Context.AUDIO_SERVICE)),
            cast(jclass("android.app.NotificationManager"), context.getSystemService(Context.NOTIFICATION_SERVICE)))


def level_index(percent, lowest, highest):
    """A volume percent -> a stream index in [lowest, highest]."""
    return max(lowest, min(highest, round(percent * highest / 100.0)))


def _state(audio, notifications):
    streams = {}
    for name in ("media", "ring", "notification", "alarm", "call"):
        code = STREAMS[name]
        now, highest = int(audio.getStreamVolume(code)), int(audio.getStreamMaxVolume(code))
        streams[name] = {"pct": round(now * 100 / highest) if highest else 0, "level": now, "max": highest}
    return {"streams": streams, "ringer": RINGER.get(int(audio.getRingerMode()), "unknown"),
            "dnd": DND.get(int(notifications.getCurrentInterruptionFilter()), "unknown"),
            "dnd_access": bool(notifications.isNotificationPolicyAccessGranted())}


def _changed(cap, change):
    """Make `change(audio, notifications)`. Where Android refuses it for want of Do Not Disturb access
    (a SecurityException), ask the person for that access, and make it again once they have given it."""
    audio, notifications = _managers()
    try:
        change(audio, notifications)
    except Exception as e:
        if "SecurityException" not in type(e).__name__ and "Not allowed" not in str(e):
            raise
        refused = jclass("systems.bake.rook.core.worker.Bridge").ask(ACCESS, cap)
        if refused is not None:
            raise ValueError(str(refused)) from None
        change(audio, notifications)
    return _state(audio, notifications)


async def status(args):
    _check(args, "sound.status", ())
    return await asyncio.to_thread(lambda: _state(*_managers()))


def _set(stream, level, step, mute, ringer):
    code = STREAMS[stream]
    sdk = int(jclass("android.os.Build$VERSION").SDK_INT)

    def change(audio, notifications):
        if level is not None:
            lowest = int(audio.getStreamMinVolume(code)) if sdk >= 28 else 0
            audio.setStreamVolume(code, level_index(level, lowest, int(audio.getStreamMaxVolume(code))), 0)
        for _ in range(abs(step or 0)):
            audio.adjustStreamVolume(code, 1 if step > 0 else -1, 0)          # ADJUST_RAISE, ADJUST_LOWER
        if mute is not None:
            audio.adjustStreamVolume(code, -100 if mute else 100, 0)          # ADJUST_MUTE, ADJUST_UNMUTE
        if ringer is not None:
            audio.setRingerMode({word: mode for mode, word in RINGER.items()}[ringer])
    return _changed("sound.set", change)


async def set_sound(args):
    _check(args, "sound.set", ("level", "step", "mute", "stream", "ringer"))
    level, step = _number(args, "level", 0, 100), _number(args, "step")
    mute = args.get("mute")
    if mute is not None and not isinstance(mute, bool):
        raise ValueError("mute must be true or false")
    stream = _choice(args, "stream", list(STREAMS), "media")
    ringer = _choice(args, "ringer", list(RINGER.values()))
    if level is None and not step and mute is None and ringer is None:
        raise ValueError("give level, step, mute or ringer")
    return await asyncio.to_thread(_set, stream, level, step, mute, ringer)


async def dnd(args):
    _check(args, "sound.dnd", ("mode",), ("mode",))
    mode = _choice(args, "mode", ["off", "priority", "alarms", "total"])
    filters = {"off": 1, "priority": 2, "total": 3, "alarms": 4}
    return await asyncio.to_thread(_changed, "sound.dnd",
                                   lambda audio, notifications: notifications.setInterruptionFilter(filters[mode]))


CAPS = {} if jclass is None else {
    "sound.status": Cap(
        "Each stream's volume (media, ring, notification, alarm, call: pct, level, max), the ringer, "
        "Do Not Disturb (off, priority, alarms, total) and dnd_access (whether Rook may change it).",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
    "sound.set": Cap(
        "Set a stream's volume (level in percent, or step by that many steps, or mute) and/or the ringer; "
        "silent asks for Do Not Disturb access the first time. Returns sound.status.",
        {"type": "object", "additionalProperties": False, "properties": {
            "level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Volume in percent."},
            "step": {"type": "integer", "description": "Move the volume by this many steps (+3, -2)."},
            "mute": {"type": "boolean"},
            "stream": {"type": "string", "enum": list(STREAMS), "default": "media"},
            "ringer": {"type": "string", "enum": list(RINGER.values())}}},
        set_sound),
    "sound.dnd": Cap(
        "Set Do Not Disturb: off, priority, alarms (alarms only) or total (silence); asks for Do Not Disturb "
        "access the first time. Returns sound.status.",
        {"type": "object", "additionalProperties": False, "required": ["mode"],
         "properties": {"mode": {"type": "string", "enum": ["off", "priority", "alarms", "total"]}}},
        dnd),
}
