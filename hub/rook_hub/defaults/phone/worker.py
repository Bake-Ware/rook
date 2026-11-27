"""phone: what only a phone does, on the Rook Android app's worker (brief AP2), shipped as a plugin
installed by default: ring to be found (phone.find, phone.find_stop), buzz (phone.vibrate), the
flashlight (phone.torch) and, with the screen caps (brief AP5), unlocking its PIN lock screen
(phone.unlock). The names, arguments and replies are 0.5's (origin/castling rook/plugins/phone and the
app's FindDeviceBridge.kt and UnlockPin.kt); 0.5's phone.switch is not here (it switched between 0.4
and 0.5).

phone.unlock unlocks the way a finger would, through the app's accessibility service (asked for the
first time it is needed, as the ui caps ask): it turns the screen on and asks Android to dismiss the
lock screen. With no secure lock that is all it takes (and a swipe up). With a PIN, Android brings up
its PIN pad (where it does not, a swipe up, then the lock screen's lock icon, bring it up) and the
service presses its keys, found by what they are (the pad's digit keys) rather
than at 0.5's pixel positions for one phone's layout, then its enter key. The PIN is `pin`, best
passed as a vault reference (`secret:<name>`: the hub puts the digits in on the way, and the journal
keeps the reference, X2), or else the one in this worker's config for this plugin, `unlock_pin`, set
the same way: `config.set {worker: <the phone>, set: {"phone": {"unlock_pin": "secret:<name>"}}}`.
That config is where the worker's secrets are: kept at the hub, sent to the phone with the secret put
in, and held in the worker's memory only (CF2, CF3). The digits are never logged and never in a reply.
It bypasses nothing: a wrong PIN is Android's to refuse, and Android's own lockout applies.

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
import json
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


SETTLE_MS = 500         # what "settled" means: nothing on screen changed for this long (Hands.SETTLE_MS, 0.5's)
NO_PIN = ("this phone has a secure lock and no PIN was given: pass pin (best as secret:<name>, so the journal never "
          "holds the digits), or set unlock_pin in this worker's config for the phone plugin: config.set {worker, set: "
          "{\"phone\": {\"unlock_pin\": \"secret:<name>\"}}}")


def _pin(given):
    """The PIN to type and where it came from: (digits, "given" | "saved"), or (None, None) when there is none."""
    if given is not None:
        if not isinstance(given, str) or not given.isdigit():
            raise ValueError("phone.unlock: pin must be the PIN's digits, as text")
        return given, "given"
    saved = (globals().get("WORKER") and WORKER.config.get("unlock_pin")) or None  # noqa: F821 - the worker puts WORKER here
    if saved is None:
        return None, None
    if not isinstance(saved, str) or not saved.isdigit():
        raise ValueError("unlock_pin in this worker's config for the phone plugin must be the PIN's digits, as text")
    return saved, "saved"


def _until(hands, done, settle=True, showing=False):
    """Wait until done() is true, or (with settle) until the screen has settled: nothing on it changed for
    SETTLE_MS, counted from now (with showing, only once it shows something to act on: the empty moment while a
    lock screen goes away is not an answer). Whether done() came true. No end of ours."""
    since = int(hands.now())
    while True:
        seen = hands.changes()
        if done():
            return True
        if not settle:
            hands.awaitChange(seen, 0)
            continue
        now = int(hands.now())
        quiet = now - max(now - int(hands.quietMs()), since)
        if quiet >= SETTLE_MS:
            if not showing or json.loads(str(hands.screen(-1, -1))).get("items"):
                return bool(done())
            hands.awaitChange(seen, 0)
            continue
        hands.awaitChange(seen, SETTLE_MS - quiet)


def _settle(hands):
    _until(hands, lambda: False)


def _screen_words(hands):
    shot = json.loads(str(hands.screen(-1, -1)))
    return ", ".join([i["text"] for i in shot.get("items", ()) if i.get("text")] + shot.get("text", [])) or "nothing to act on"


def _unlock(pin, source, hold_ms, pre_swipe):
    Bridge, Hands = jclass("systems.bake.rook.core.worker.Bridge"), jclass("systems.bake.rook.core.worker.Hands")
    refused = Bridge.hands("phone.unlock")
    if refused is not None:
        raise ValueError(str(refused))
    keyguard = _service("KEYGUARD_SERVICE", "android.app.KeyguardManager")
    secure = bool(keyguard.isDeviceSecure())
    if secure and keyguard.isKeyguardLocked() and pin is None:
        raise ValueError(NO_PIN)
    refused = Bridge.wake("phone.unlock", True, hold_ms or 0)
    if refused is not None:
        raise ValueError(str(refused))

    def ready():            # unlocked, or the PIN pad is up
        return not keyguard.isKeyguardLocked() or (secure and Hands.pinKeys() > 0)

    typed = {"taps_ok": 0, "digits": 0}
    try:
        _settle(Hands)          # the pad is pressed only once the lock screen has settled: keys pressed while it moves are lost
        if not ready() and pre_swipe:           # lock screens that show the clock first
            metrics = jclass("android.util.DisplayMetrics")()
            _service("WINDOW_SERVICE", "android.view.WindowManager").getDefaultDisplay().getRealMetrics(metrics)
            w, h = int(metrics.widthPixels), int(metrics.heightPixels)
            Hands.swipe(w // 2, h * 85 // 100, w // 2, h * 20 // 100, 200)
            _settle(Hands)
        if not ready() and secure and Hands.pressUnlockIcon():     # its lock icon brings up the pad where a swipe did not
            _settle(Hands)
        if secure and keyguard.isKeyguardLocked():
            if Hands.pinKeys() == 0:
                raise ValueError("the lock screen shows no PIN pad (a pattern or a password lock?); it shows: "
                                 + _screen_words(Hands))
            typed = json.loads(str(Hands.typePin(pin)))
            if "keys" in typed:
                raise ValueError(f"the lock screen's PIN pad shows {typed['keys']} of the 10 digit keys, not every digit the PIN has")
            # Enter, then until the lock screen is gone or the PIN field has emptied (Android clears it after a wrong
            # PIN; checking one can take a while with nothing on screen changing, so stillness alone is not an
            # answer), then until gone or settled showing something (not the empty moment while it goes away). With
            # no enter key (a PIN that confirms itself), until gone or settled.
            if Hands.pressEnter():
                _until(Hands, lambda: not keyguard.isKeyguardLocked() or Hands.pinLength() <= 0, settle=False)
            _until(Hands, lambda: not keyguard.isKeyguardLocked(), showing=True)
    finally:
        Bridge.wakeEnd()
    unlocked = not bool(keyguard.isKeyguardLocked())
    out = {"unlocked": unlocked, "pin": source or "none", "taps_ok": typed["taps_ok"] == typed["digits"]}
    if not unlocked:
        out["note"] = "still locked: the PIN may be wrong, or the pad wants another key; check ui.screen"
    return out


async def unlock(args):
    _check(args, "phone.unlock", ("pin", "hold_ms", "pre_swipe"))
    hold_ms, pre_swipe = _whole(args, "hold_ms", "phone.unlock"), args.get("pre_swipe", True)
    if not isinstance(pre_swipe, bool):
        raise ValueError("phone.unlock: pre_swipe must be true or false")
    pin, source = _pin(args.get("pin") or None)
    return await asyncio.to_thread(_unlock, pin, source, hold_ms, pre_swipe)


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
    "phone.unlock": Cap(
        "Unlock the lock screen the way a finger would: wake, bring up the PIN pad, press the PIN's keys and enter; "
        "pin (best secret:<name>), else the worker's config unlock_pin; with no secure lock, a wake and a swipe. "
        "{unlocked, pin: given|saved|none, taps_ok, note?}; the reply never carries digits.",
        {"type": "object", "additionalProperties": False, "properties": {
            "pin": {"type": "string", "description": "The PIN's digits; pass a vault reference, secret:<name>."},
            "hold_ms": {"type": "integer", "minimum": 1, "description": "Keep the waking screen up this long."},
            "pre_swipe": {"type": "boolean", "default": True,
                          "description": "Swipe up first when no PIN pad shows (lock screens that show the clock first)."}}},
        unlock),
    "phone.torch": Cap(
        "Turn the phone's flashlight on (on: true, the default) or off.",
        {"type": "object", "additionalProperties": False, "properties": {"on": {"type": "boolean"}}},
        torch),
}
