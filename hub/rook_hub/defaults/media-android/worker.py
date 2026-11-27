"""media-android: what plays on the phone the Rook Android app runs on, and its transport controls
(brief AP4), shipped as a plugin installed by default. The cap names, arguments and replies are 0.5's
(origin/castling rook/plugins/media-android).

Every app's media session is read through Android's MediaSessionManager, which shows them to an app
whose notification listener it names (NotificationListener.kt): so these caps need Rook's notification
access. The first call that needs it opens Android's page for it through the app's bridge (or a
notification that opens it, when the app is not in front), waits for the person, then answers.
Refused, the error says where to turn it on.

The session a control acts on is 0.5's: the one `package` names (a package or an app's name, any
case), else the one playing, else the most recent. With no session at all, a control without
`package` or `by` sends a media key, which Android gives to the app that played last, as 0.5 did.
0.5's 10 s timeouts are left out.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

APP = "systems.bake.rook.core.worker."
STATES = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast_forwarding", 5: "rewinding",
          6: "buffering", 7: "error", 8: "connecting", 9: "skipping_previous", 10: "skipping_next",
          11: "skipping"}       # PlaybackState.STATE_*
KEYS = {"play": 126, "pause": 127, "stop": 86, "next": 87, "previous": 88, "seek": 90}   # KeyEvent.KEYCODE_MEDIA_*
PLAYING = ("playing", "buffering")
ACTIONS = {"stop": 1, "pause": 2, "play": 4, "previous": 16, "next": 32, "seek": 64, "seek_to": 256}   # PlaybackState.ACTION_*


def _check(args, cap, allowed):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")


def _context():
    return jclass(APP + "Bridge").context()


def _service(name, kind):
    return cast(jclass(kind), _context().getSystemService(getattr(jclass("android.content.Context"), name)))


def _controllers(cap):
    """Every active media session's controller, most recent first, once Rook has notification access."""
    Bridge = jclass(APP + "Bridge")
    refused = Bridge.ask(Bridge.NOTIFICATION_ACCESS, cap)
    if refused is not None:
        raise ValueError(str(refused))
    sessions = _service("MEDIA_SESSION_SERVICE", "android.media.session.MediaSessionManager")
    found = sessions.getActiveSessions(jclass(APP + "NotificationListener").component(_context()))
    return [found.get(i) for i in range(int(found.size()))]


def position_ms(state, now_ms):
    """Where a session is now, from its PlaybackState: the position it last told, moved on by the time
    since at its speed while it plays. None when it does not say."""
    told = int(state.getPosition())
    if told < 0:
        return None
    if STATES.get(int(state.getState())) != "playing" or int(state.getLastPositionUpdateTime()) <= 0:
        return told
    return told + int((now_ms - int(state.getLastPositionUpdateTime())) * float(state.getPlaybackSpeed()))


def _describe(controller):
    package = str(controller.getPackageName())
    out = {"package": package, "app": package, "state": "none"}
    try:
        manager = _context().getPackageManager()
        out["app"] = str(manager.getApplicationLabel(manager.getApplicationInfo(package, 0)))
    except Exception:
        pass
    state = controller.getPlaybackState()
    if state is not None:
        out["state"] = STATES.get(int(state.getState()), "unknown")
        at = position_ms(state, int(jclass("android.os.SystemClock").elapsedRealtime()))
        if at is not None:
            out["position_s"] = round(at / 1000.0, 1)
    metadata = controller.getMetadata()
    if metadata is not None:
        for key in ("title", "artist", "album"):
            value = metadata.getString(f"android.media.metadata.{key.upper()}")
            if value:
                out[key] = str(value)
        if int(metadata.getLong("android.media.metadata.DURATION")) > 0:
            out["duration_s"] = round(int(metadata.getLong("android.media.metadata.DURATION")) / 1000.0, 1)
    return out


def pick(sessions, package):
    """(controller, described) to act on: the one `package` names (package or app name), else the
    one playing, else the first (the most recent). None when there is none."""
    wanted = package.strip().lower()
    if wanted:
        return next(((c, s) for c, s in sessions if wanted in (s["package"].lower(), s["app"].lower())), None)
    return next(((c, s) for c, s in sessions if s["state"] in PLAYING), None) or (sessions[0] if sessions else None)


def _status():
    active = bool(_service("AUDIO_SERVICE", "android.media.AudioManager").isMusicActive())
    described = [_describe(c) for c in _controllers("media.status")]
    return {"music_active": active, "sessions": sorted(described, key=lambda s: s["state"] not in PLAYING)}


async def status(args):
    _check(args, "media.status", ())
    return await asyncio.to_thread(_status)


def _control(action, package, by):
    sessions = [(c, _describe(c)) for c in _controllers(f"media.{action}")]
    if sessions:
        got = pick(sessions, package)
        if got is None:
            raise ValueError(f"no media session is {package!r}; these are: {', '.join(s['app'] for _, s in sessions)}")
        controller, described = got
        controls = controller.getTransportControls()
        state = controller.getPlaybackState()
        offered = ACTIONS["seek_to" if action == "seek" and by is not None else action]
        if action == "seek" and by is not None:
            at = position_ms(state, int(jclass("android.os.SystemClock").elapsedRealtime())) if state is not None else None
            controls.seekTo(max(0, (at or 0) + by * 1000))
        else:
            {"play": controls.play, "pause": controls.pause, "stop": controls.stop, "next": controls.skipToNext,
             "previous": controls.skipToPrevious, "seek": controls.fastForward}[action]()
        out = {"via": "session", "package": described["package"], "app": described["app"], "was": described["state"]}
        if described.get("title"):
            out["title"] = described["title"]
        if state is not None and not int(state.getActions()) & offered:
            out["note"] = (f"{described['app']} does not list {action if offered != 256 else 'seeking to a place'} "
                           "among what its player takes, so it may do nothing; it was sent all the same")
        return out
    if package:
        raise ValueError(f"no media session is {package!r}: nothing on the phone has one now")
    if action == "seek" and by is not None:
        raise ValueError("seeking by seconds needs a media session, and nothing on the phone has one now")
    audio, KeyEvent = _service("AUDIO_SERVICE", "android.media.AudioManager"), jclass("android.view.KeyEvent")
    for press in (KeyEvent.ACTION_DOWN, KeyEvent.ACTION_UP):
        audio.dispatchMediaKeyEvent(KeyEvent(press, KEYS[action]))
    return {"via": "key"}


def _control_cap(action):
    async def handler(args):
        allowed = ("package", "by") if action == "seek" else ("package",)
        _check(args, f"media.{action}", allowed)
        package = args.get("package", "")
        if not isinstance(package, str):
            raise ValueError("package must be a package or an app's name")
        by = args.get("by")
        if by is not None and (not isinstance(by, int) or isinstance(by, bool)):
            raise ValueError("by must be a whole number of seconds (negative goes back)")
        return await asyncio.to_thread(_control, action, package, by)
    return handler


_PACKAGE = {"type": "string", "default": "",
            "description": "A package or an app's name; blank: the one playing, else the most recent."}


def _schema(seek=False):
    properties = {"package": _PACKAGE}
    if seek:
        properties["by"] = {"type": "integer", "description": "Seconds; negative goes back. Blank: the player's own skip forward."}
    return {"type": "object", "additionalProperties": False, "properties": properties}


CAPS = {} if jclass is None else {
    "media.status": Cap(
        "What is playing: each media session's app, package, state, title, artist, album, position_s and "
        "duration_s, the playing one first; music_active.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
    "media.play": Cap("Play (or resume).", _schema(), _control_cap("play")),
    "media.pause": Cap("Pause.", _schema(), _control_cap("pause")),
    "media.stop": Cap("Stop.", _schema(), _control_cap("stop")),
    "media.next": Cap("Skip to the next track or episode.", _schema(), _control_cap("next")),
    "media.previous": Cap("Back to the previous track or episode.", _schema(), _control_cap("previous")),
    "media.seek": Cap("Move by seconds (negative goes back); blank: the player's own skip forward.",
                      _schema(seek=True), _control_cap("seek")),
}
