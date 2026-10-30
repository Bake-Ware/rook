"""clock.*: android.intent.action.SET_TIMER / SET_ALARM / SHOW_ALARMS (the
app holds com.android.alarm.permission.SET_ALARM)."""

from rook.kit import android
from rook.kit.android import screen

EXTRA = "android.intent.extra.alarm."


def _go(cap, action, extras=None):
    screen.awake(cap)
    i = android.intent(action, extras=extras)
    app = android.handler(i)
    return {**({"app": app} if app else {}), **android.start(i)}


def _label(extras, label):
    if label:
        extras[EXTRA + "MESSAGE"] = label
    return extras


def timer(ctx, seconds, label, start):
    return _go("clock.timer", "android.intent.action.SET_TIMER",
               _label({EXTRA + "LENGTH": seconds, EXTRA + "SKIP_UI": start}, label))


def alarm(ctx, hour, minute, label, start):
    return _go("clock.alarm", "android.intent.action.SET_ALARM",
               _label({EXTRA + "HOUR": hour, EXTRA + "MINUTES": minute, EXTRA + "SKIP_UI": start}, label))


def alarms(ctx):
    return _go("clock.alarms", "android.intent.action.SHOW_ALARMS")
