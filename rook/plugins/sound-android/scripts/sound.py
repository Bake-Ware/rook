"""sound.*: AudioManager streams and ringer, NotificationManager's interruption filter."""

from rook.kit import android
from rook.kit.ctx import KitError

STREAMS = {"call": 0, "system": 1, "ring": 2, "media": 3, "alarm": 4, "notification": 5}   # AudioManager.STREAM_*
RINGER = {0: "silent", 1: "vibrate", 2: "normal"}
DND = {0: "unknown", 1: "off", 2: "priority", 3: "total", 4: "alarms"}   # INTERRUPTION_FILTER_*
NEEDS_ACCESS = "needs Do Not Disturb access: allow it in the Rook app"


def _managers():
    return (android.service("AUDIO_SERVICE", "android.media.AudioManager"),
            android.service("NOTIFICATION_SERVICE", "android.app.NotificationManager"))


def level_index(pct: int, lo: int, hi: int) -> int:
    """A volume percent -> a stream index in [lo, hi]."""
    return max(lo, min(hi, round(pct * hi / 100.0)))


def _state(am, nm) -> dict:
    streams = {}
    for name in ("media", "ring", "notification", "alarm", "call"):
        code = STREAMS[name]
        cur, hi = int(am.getStreamVolume(code)), int(am.getStreamMaxVolume(code))
        streams[name] = {"pct": round(cur * 100 / hi) if hi else 0, "level": cur, "max": hi}
    return {"streams": streams, "ringer": RINGER.get(int(am.getRingerMode()), "unknown"),
            "dnd": DND.get(int(nm.getCurrentInterruptionFilter()), "unknown"),
            "dnd_access": bool(nm.isNotificationPolicyAccessGranted())}


def _guarded(change):
    """Run ``change(am, nm)``; Android's SecurityException means no DND access."""
    am, nm = _managers()
    try:
        change(am, nm)
    except Exception as e:
        if "Security" in type(e).__name__ or "Not allowed" in str(e):
            raise KitError(NEEDS_ACCESS) from None
        raise
    return _state(am, nm)


def status(ctx):
    return _state(*_managers())


def set_sound(ctx, stream, level=None, step=None, mute=None, ringer=None):
    if level is None and not step and mute is None and ringer is None:
        raise KitError("give level, step, mute or ringer")
    code = STREAMS[stream]

    def change(am, nm):
        if level is not None:
            lo = int(am.getStreamMinVolume(code)) if int(android.jclass("android.os.Build$VERSION").SDK_INT) >= 28 else 0
            am.setStreamVolume(code, level_index(level, lo, int(am.getStreamMaxVolume(code))), 0)
        for _ in range(abs(step or 0)):
            am.adjustStreamVolume(code, 1 if step > 0 else -1, 0)       # ADJUST_RAISE / ADJUST_LOWER
        if mute is not None:
            am.adjustStreamVolume(code, -100 if mute else 100, 0)       # ADJUST_MUTE / ADJUST_UNMUTE
        if ringer is not None:
            am.setRingerMode({v: k for k, v in RINGER.items()}[ringer])
    return _guarded(change)


def dnd(ctx, mode):
    return _guarded(lambda am, nm: nm.setInterruptionFilter({"off": 1, "priority": 2, "total": 3, "alarms": 4}[mode]))
