"""media.*: MediaSessionManager.getActiveSessions (it needs the app's
notification listener) and each session's transport controls; a media key
when there is no access."""

from rook.kit import android
from rook.kit.ctx import KitError

STATES = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast_forwarding", 5: "rewinding",
          6: "buffering", 7: "error", 8: "connecting", 9: "skipping_previous", 10: "skipping_next",
          11: "skipping"}
KEYS = {"play": 126, "pause": 127, "stop": 86, "next": 87, "previous": 88, "seek": 90}   # KeyEvent.KEYCODE_MEDIA_*
LISTENER = "systems.bake.rook.RookNotificationListener"
NO_ACCESS = "allow notification access in the Rook app to pick an app"


def _controllers():
    """Active MediaControllers, or None without notification access."""
    msm = android.service("MEDIA_SESSION_SERVICE", "android.media.session.MediaSessionManager")
    try:
        found = msm.getActiveSessions(android.jclass("android.content.ComponentName")(android.context(), LISTENER))
    except Exception as e:
        if "Security" in type(e).__name__ or "permission" in str(e).lower():
            return None
        raise
    return [found.get(i) for i in range(int(found.size()))]


def _describe(c) -> dict:
    pkg = str(c.getPackageName())
    out = {"package": pkg, "app": pkg, "state": "none"}
    try:
        pm = android.context().getPackageManager()
        out["app"] = str(pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)))
    except Exception:
        pass
    pb = c.getPlaybackState()
    if pb is not None:
        out["state"] = STATES.get(int(pb.getState()), "unknown")
        if int(pb.getPosition()) >= 0:
            out["position_s"] = round(int(pb.getPosition()) / 1000.0, 1)
    md = c.getMetadata()
    if md is not None:
        for key in ("title", "artist", "album"):
            v = md.getString(f"android.media.metadata.{key.upper()}")
            if v:
                out[key] = str(v)
        if int(md.getLong("android.media.metadata.DURATION")) > 0:
            out["duration_s"] = round(int(md.getLong("android.media.metadata.DURATION")) / 1000.0, 1)
    return out


def pick(sessions: list, package: str):
    """(controller, info) to drive: the package's (or app name's), else the playing one, else the first."""
    p = package.strip().lower()
    if p:
        return next(((c, s) for c, s in sessions if p in (s["package"].lower(), s["app"].lower())), None)
    return next(((c, s) for c, s in sessions if s["state"] in ("playing", "buffering")), None) or \
        (sessions[0] if sessions else None)


def status(ctx):
    active = bool(android.service("AUDIO_SERVICE", "android.media.AudioManager").isMusicActive())
    ctrls = _controllers()
    if ctrls is None:
        return {"music_active": active, "sessions": [], "note": NO_ACCESS}
    sessions = sorted((_describe(c) for c in ctrls), key=lambda s: s["state"] not in ("playing", "buffering"))
    return {"music_active": active, "sessions": sessions}


def control(ctx, action, package, by=None):
    ctrls = _controllers()
    if ctrls:
        sessions = [(c, _describe(c)) for c in ctrls]
        got = pick(sessions, package)
        if got is None:
            raise KitError(f"no media session for {package!r} (playing: "
                           f"{', '.join(s['app'] for _, s in sessions)})")
        c, s = got
        tc = c.getTransportControls()
        if action == "seek" and by is not None:
            pos = int(c.getPlaybackState().getPosition()) if c.getPlaybackState() is not None else 0
            tc.seekTo(max(0, pos + by * 1000))
        else:
            {"play": tc.play, "pause": tc.pause, "stop": tc.stop, "next": tc.skipToNext,
             "previous": tc.skipToPrevious, "seek": tc.fastForward}[action]()
        return {"via": "session", "package": s["package"], "app": s["app"], "was": s["state"],
                **({"title": s["title"]} if s.get("title") else {})}
    if package:
        raise KitError(f"no media session for {package!r}" if ctrls is not None else NO_ACCESS)
    if action == "seek" and by is not None:
        raise KitError("seeking by seconds needs a media session: " + NO_ACCESS)
    am = android.service("AUDIO_SERVICE", "android.media.AudioManager")
    KeyEvent = android.jclass("android.view.KeyEvent")
    for ev in (KeyEvent.ACTION_DOWN, KeyEvent.ACTION_UP):
        am.dispatchMediaKeyEvent(KeyEvent(ev, KEYS[action]))
    return {"via": "key"}
