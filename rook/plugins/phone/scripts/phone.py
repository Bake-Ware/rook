"""phone.*: FindDeviceBridge, the PIN unlock (rook.kit.android.screen),
Vibrator, the camera's torch and the app's 0.4/0.5 switch."""

import json
import os

from rook.kit import android
from rook.kit.android import screen
from rook.kit.ctx import KitError


def find(ctx, seconds):
    return android.result(android.app("FindDeviceBridge").ring(android.context(), seconds))


def find_stop(ctx):
    return android.result(android.app("FindDeviceBridge").stop())


def unlock(ctx, hold_ms, pre_swipe, pin=None):
    screen.awake("phone.unlock", unlock_after=False)
    return screen.unlock(pin or None, hold_ms=hold_ms, pre_swipe=pre_swipe)


def vibrate(ctx, ms):
    sdk = int(android.jclass("android.os.Build$VERSION").SDK_INT)
    if sdk >= 31:
        vib = android.service("VIBRATOR_MANAGER_SERVICE", "android.os.VibratorManager").getDefaultVibrator()
    else:
        vib = android.service("VIBRATOR_SERVICE", "android.os.Vibrator")
    if sdk >= 26:
        Effect = android.jclass("android.os.VibrationEffect")
        vib.vibrate(Effect.createOneShot(ms, Effect.DEFAULT_AMPLITUDE))
    else:   # Android 6/7 have no VibrationEffect
        vib.vibrate(ms)
    return {"ms": ms}


def torch(ctx, on):
    cm = android.service("CAMERA_SERVICE", "android.hardware.camera2.CameraManager")
    flash = android.jclass("android.hardware.camera2.CameraCharacteristics").FLASH_INFO_AVAILABLE
    camera = next((c for c in cm.getCameraIdList() if cm.getCameraCharacteristics(c).get(flash)), None)
    if camera is None:
        raise KitError("this phone has no camera with a flash")
    cm.setTorchMode(camera, on)
    return {"on": on}


#: The app's one-shot switch request (WorkerService.kt reads it every 15 s).
SWITCH = "rook05/switch.json"


def switch(ctx, to):
    path = os.path.join(str(android.context().getFilesDir().getAbsolutePath()), SWITCH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"to": to}, f)
    os.replace(tmp, path)
    return {"to": to, "asked": True}
