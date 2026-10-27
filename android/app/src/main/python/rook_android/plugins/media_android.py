"""media.* — what is playing, and play/pause/next on it (MediaSession).

media.now lists the phone's active media sessions (app, playing/paused, title,
artist, position) and media.control drives one with its transport controls.
Both read sessions through the notification listener Rook already has
(RookNotificationListener: MediaSessionManager.getActiveSessions needs it).
Without notification access, media.control falls back to a media key, which
goes to whichever app last played, and media.now reports only whether
something is playing.
"""

from __future__ import annotations

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place
from rook_android.androidctx import app_context, cast, jclass

#: PlaybackState.STATE_* -> name.
STATES = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast_forwarding",
          5: "rewinding", 6: "buffering", 7: "error", 8: "connecting",
          9: "skipping_previous", 10: "skipping_next", 11: "skipping"}
#: media.control action -> KeyEvent.KEYCODE_MEDIA_* for the no-access fallback.
KEYS = {"toggle": 85, "play": 126, "pause": 127, "next": 87, "previous": 88, "stop": 86,
        "forward": 90, "rewind": 89}
ACTIONS = tuple(KEYS)
_LISTENER = "systems.bake.rook.RookNotificationListener"


def pick_session(sessions: list[dict], package: str = "") -> dict | None:
    """The session to drive: the given package's, else the one playing, else the first."""
    p = str(package or "").strip().lower()
    if p:
        for s in sessions:
            if s["package"].lower() == p or s.get("app", "").lower() == p:
                return s
        return None
    for s in sessions:
        if s["state"] in ("playing", "buffering"):
            return s
    return sessions[0] if sessions else None


def _controllers(ctx):
    """Active MediaControllers, or None without notification access."""
    Context = jclass("android.content.Context")
    msm = cast(jclass("android.media.session.MediaSessionManager"),
               ctx.getSystemService(Context.MEDIA_SESSION_SERVICE))
    comp = jclass("android.content.ComponentName")(ctx, _LISTENER)
    try:
        found = msm.getActiveSessions(comp)
    except Exception as e:
        if "Security" in type(e).__name__ or "permission" in str(e).lower():
            return None
        raise
    return [found.get(i) for i in range(int(found.size()))]


def _describe(ctx, c) -> dict:
    pkg = str(c.getPackageName())
    out = {"package": pkg, "app": pkg, "state": "none"}
    try:
        pm = ctx.getPackageManager()
        out["app"] = str(pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)))
    except Exception:
        pass
    pb = c.getPlaybackState()
    if pb is not None:
        out["state"] = STATES.get(int(pb.getState()), "unknown")
        pos = int(pb.getPosition())
        if pos >= 0:
            out["position_s"] = round(pos / 1000.0, 1)
    md = c.getMetadata()
    if md is not None:
        for key, name in (("TITLE", "title"), ("ARTIST", "artist"), ("ALBUM", "album")):
            v = md.getString(f"android.media.metadata.{key}")
            if v:
                out[name] = str(v)
        dur = int(md.getLong("android.media.metadata.DURATION"))
        if dur > 0:
            out["duration_s"] = round(dur / 1000.0, 1)
    out["_c"] = c
    return out


def _public(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


class AndroidMediaPlugin(Plugin):
    NAMESPACE = "media"
    PLACEMENT = place(ANDROID_APP)

    @capability("now", risk="read", tags=("sensitive",), scope="local")
    def _now(self) -> dict:
        """What is playing: each active media session's app, state
        (playing|paused|stopped|buffering...), title, artist, album, position_s and
        duration_s, the playing one first. Without notification access only
        music_active (something is playing) is known."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            am = cast(jclass("android.media.AudioManager"), ctx.getSystemService(Context.AUDIO_SERVICE))
            active = bool(am.isMusicActive())
            ctrls = _controllers(ctx)
            if ctrls is None:
                return {"ok": True, "music_active": active, "sessions": [],
                        "note": "grant notification access in the app to see what is playing"}
            sessions = [_describe(ctx, c) for c in ctrls]
            sessions.sort(key=lambda s: s["state"] not in ("playing", "buffering"))
            return {"ok": True, "music_active": active, "sessions": [_public(s) for s in sessions]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("control", risk="exec", tags=("physical",), scope="local")
    def _control(self, action: str = "toggle", package: str = "") -> dict:
        """Play, pause, toggle, next, previous, stop, forward or rewind. Drives
        package's session (package name or app label), else the one playing,
        else the most recent. Without notification access it sends a media key
        instead (via=key), which the last app that played gets."""
        a = str(action or "toggle").strip().lower()
        a = {"play_pause": "toggle", "skip": "next", "prev": "previous", "back": "previous"}.get(a, a)
        if a not in KEYS:
            return {"ok": False, "error": "action is one of " + ", ".join(ACTIONS)}
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            ctrls = _controllers(ctx)
            if ctrls:
                sessions = [_describe(ctx, c) for c in ctrls]
                s = pick_session(sessions, package)
                if s is None:
                    return {"ok": False, "error": f"no media session for {package!r}",
                            "sessions": [_public(x) for x in sessions]}
                tc = s["_c"].getTransportControls()
                act = a
                if a == "toggle":
                    act = "pause" if s["state"] in ("playing", "buffering") else "play"
                {"play": tc.play, "pause": tc.pause, "next": tc.skipToNext,
                 "previous": tc.skipToPrevious, "stop": tc.stop, "forward": tc.fastForward,
                 "rewind": tc.rewind}[act]()
                return {"ok": True, "via": "session", "action": act, "package": s["package"],
                        "app": s["app"], "was": s["state"], **({"title": s["title"]} if s.get("title") else {})}
            if package:
                return {"ok": False, "error": f"no media session for {package!r}"
                        if ctrls is not None else "grant notification access in the app to pick an app"}
            Context = jclass("android.content.Context")
            am = cast(jclass("android.media.AudioManager"), ctx.getSystemService(Context.AUDIO_SERVICE))
            KeyEvent = jclass("android.view.KeyEvent")
            for ev in (KeyEvent.ACTION_DOWN, KeyEvent.ACTION_UP):
                am.dispatchMediaKeyEvent(KeyEvent(ev, KEYS[a]))
            return {"ok": True, "via": "key", "action": a}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidMediaPlugin
