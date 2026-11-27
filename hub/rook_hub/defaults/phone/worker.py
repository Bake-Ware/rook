"""phone: what only a phone does, on the Rook Android app's worker (brief AP2), shipped as a plugin
installed by default: ring to be found (phone.find, phone.find_stop), buzz (phone.vibrate) and the
flashlight (phone.torch). The names, arguments and replies are 0.5's (origin/castling
rook/plugins/phone and the app's FindDeviceBridge.kt). phone.unlock comes with the screen caps
(brief AP5); 0.5's phone.switch is not here (it switched between 0.4 and 0.5).

Nothing here bounds a call (Bake, 17 November). phone.find rings for `seconds` when given and,
without it, until phone.find_stop; phone.vibrate buzzes for the `ms` it is given, as long as that is,
and for 300 ms without it, as 0.5 did (the steward's decision on pull request 465, reported to Bake: a
default is not a bound). 0.5 capped both (120 s, 5000 ms) and rang 30 s without `seconds`; neither is kept.

phone.find plays the alarm sound on the alarm stream, at that stream's full volume, as 0.5 did, and
puts the volume back when it stops; Do Not Disturb still applies. The ring is held here, in this
module, until it stops. None of the caps needs a permission the person is asked for: vibrating needs
VIBRATE, which Android grants at install, and the torch needs none.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import threading

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

VIBRATE_MS = 300               # phone.vibrate without ms, as 0.5
NOTE = "Alarm volume raised to maximum; device Do Not Disturb policy applies. Volume restores when stopped."
_ring = {}                      # the ring while it rings: player, volume to put back, timer
_lock = threading.Lock()


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _whole(args, name, cap):
    value = args.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise ValueError(f"{cap}: {name} must be a whole number, 1 or more")
    return value


def _context():
    return jclass("systems.bake.rook.core.worker.Bridge").context()


def _service(name, kind):
    return cast(jclass(kind), _context().getSystemService(getattr(jclass("android.content.Context"), name)))


def _audio():
    return _service("AUDIO_SERVICE", "android.media.AudioManager")


def _stop():
    """Stop the ring, if one rings, and put the alarm volume back."""
    with _lock:
        ring = dict(_ring)
        _ring.clear()
    if ring:
        if ring["timer"] is not None:
            ring["timer"].cancel()
        try:
            ring["player"].release()
        finally:
            _audio().setStreamVolume(jclass("android.media.AudioManager").STREAM_ALARM, ring["volume"], 0)
    return {"ringing": False}


def _find(seconds):
    _stop()
    AudioManager, RingtoneManager = jclass("android.media.AudioManager"), jclass("android.media.RingtoneManager")
    Attributes = jclass("android.media.AudioAttributes")
    audio = _audio()
    volume = int(audio.getStreamVolume(AudioManager.STREAM_ALARM))
    audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
    player = jclass("android.media.MediaPlayer")()
    try:
        sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM) \
            or RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        player.setAudioAttributes(Attributes.Builder().setUsage(Attributes.USAGE_ALARM)
                                  .setContentType(Attributes.CONTENT_TYPE_SONIFICATION).build())
        player.setDataSource(_context(), sound)
        player.setLooping(True)
        player.prepare()
        player.start()
    except Exception:
        player.release()
        audio.setStreamVolume(AudioManager.STREAM_ALARM, volume, 0)
        raise
    timer = None
    if seconds is not None:
        timer = threading.Timer(seconds, _stop)
        timer.daemon = True
    with _lock:
        _ring.update(player=player, volume=volume, timer=timer)
    if timer is not None:
        timer.start()
    out = {"ringing": True}
    if seconds is not None:
        out["seconds"] = seconds
    out.update(volume=int(audio.getStreamVolume(AudioManager.STREAM_ALARM)), note=NOTE)
    return out


async def find(args):
    _check(args, "phone.find", ("seconds",))
    return await asyncio.to_thread(_find, _whole(args, "seconds", "phone.find"))


async def find_stop(args):
    _check(args, "phone.find_stop", ())
    return await asyncio.to_thread(_stop)


def _vibrate(ms):
    sdk = int(jclass("android.os.Build$VERSION").SDK_INT)
    if sdk >= 31:
        vibrator = _service("VIBRATOR_MANAGER_SERVICE", "android.os.VibratorManager").getDefaultVibrator()
    else:
        vibrator = _service("VIBRATOR_SERVICE", "android.os.Vibrator")
    if not vibrator.hasVibrator():
        raise ValueError("this phone has no vibration motor")
    if sdk >= 26:
        Effect = jclass("android.os.VibrationEffect")
        vibrator.vibrate(Effect.createOneShot(ms, Effect.DEFAULT_AMPLITUDE))
    else:                           # Android 7 has no VibrationEffect
        vibrator.vibrate(ms)
    return {"ms": ms}


async def vibrate(args):
    _check(args, "phone.vibrate", ("ms",))
    ms = _whole(args, "ms", "phone.vibrate")
    return await asyncio.to_thread(_vibrate, VIBRATE_MS if ms is None else ms)


def _torch(on):
    cameras = _service("CAMERA_SERVICE", "android.hardware.camera2.CameraManager")
    flash = jclass("android.hardware.camera2.CameraCharacteristics").FLASH_INFO_AVAILABLE
    camera = next((c for c in cameras.getCameraIdList() if cameras.getCameraCharacteristics(c).get(flash)), None)
    if camera is None:
        raise ValueError("this phone has no camera with a flash")
    cameras.setTorchMode(camera, on)
    return {"on": on}


async def torch(args):
    _check(args, "phone.torch", ("on",))
    on = args.get("on", True)
    if not isinstance(on, bool):
        raise ValueError("phone.torch: on must be true or false")
    return await asyncio.to_thread(_torch, on)


CAPS = {} if jclass is None else {
    "phone.find": Cap(
        "Ring at full alarm volume so the phone can be found, for seconds or until phone.find_stop, then put the volume back.",
        {"type": "object", "additionalProperties": False,
         "properties": {"seconds": {"type": "integer", "minimum": 1}}},
        find),
    "phone.find_stop": Cap(
        "Stop phone.find's ringing and put the alarm volume back.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        find_stop),
    "phone.vibrate": Cap(
        "Buzz the phone for ms milliseconds (300 without it).",
        {"type": "object", "additionalProperties": False,
         "properties": {"ms": {"type": "integer", "minimum": 1, "default": VIBRATE_MS}}},
        vibrate),
    "phone.torch": Cap(
        "Turn the phone's flashlight on (on: true, the default) or off.",
        {"type": "object", "additionalProperties": False, "properties": {"on": {"type": "boolean"}}},
        torch),
}
