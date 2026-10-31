"""screen.*: WakeActivity, KeepAwake and ScreenCaptureBridge (systems.bake.rook)."""

from rook.kit import android
from rook.kit.android import screen
from rook.kit.ctx import KitError


def start(ctx):
    """Hand set keep-awake settings to the app (it keeps them; its Settings
    screen changes them too, last change wins). Unset ones are left alone."""
    auto, idle = ctx.settings.get("keep_awake_auto"), ctx.settings.get("keep_awake_idle_minutes")
    if auto is None and idle is None:
        return
    J = android.jclass
    android.app("KeepAwake").configure(android.context(), None if auto is None else J("java.lang.Boolean")(auto),
                                       None if idle is None else J("java.lang.Integer")(idle))


def on(ctx, dismiss_keyguard, hold_ms):
    screen.awake("screen.on", unlock_after=False)
    screen.wake(dismiss_keyguard, hold_ms)
    return {"dismiss_keyguard": dismiss_keyguard}


def keep_on(ctx, on, minutes):
    ka, c = android.app("KeepAwake"), android.context()
    return android.result(ka.engage(c, minutes) if on else ka.release(c))


def status(ctx):
    return android.result(android.app("KeepAwake").status(android.context()))


def capture(ctx, quality=85):
    screen.awake("screen.capture")
    data = android.app("ScreenCaptureBridge").captureJpeg(quality)
    if data is None:
        raise KitError("no screen capture consent yet: allow it in the Rook app")
    return ctx.file(data=bytes(data), name="screen.jpg", mime="image/jpeg")
