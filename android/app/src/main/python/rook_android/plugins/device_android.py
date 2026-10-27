"""device.* — grab-bag device controls that need no special service.

info, vibrate, torch (flashlight), clipboard get/set, open a URL, launch an app
(by package or label; device.apps lists them), common intents (device.intent:
share, dial, navigate, timer, alarm, settings pages...), volume / ringer / Do
Not Disturb (device.sound), wake/unlock, and keep-awake (device.keep_awake;
passive mode is in rook_android.keepawake). Most need no permission; VIBRATE is a normal (auto-granted) permission. Clipboard
*read* is blocked in the background on Android 10+, so device.clipboard_get is
best-effort.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place, setting
from rook_android.androidctx import app_context, jclass, cast

ENV_KEEP_AWAKE_AUTO = "ROOK_KEEP_AWAKE_AUTO"
ENV_KEEP_AWAKE_IDLE = "ROOK_KEEP_AWAKE_IDLE_MINUTES"


#: device.intent kind=settings: page name -> Settings action. A raw
#: "android.settings.*" action is also accepted.
SETTINGS_PAGES = {
    "main": "android.settings.SETTINGS",
    "wifi": "android.settings.WIFI_SETTINGS",
    "internet": "android.settings.panel.action.INTERNET_CONNECTIVITY",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "airplane": "android.settings.AIRPLANE_MODE_SETTINGS",
    "network": "android.settings.WIRELESS_SETTINGS",
    "nfc": "android.settings.NFC_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "sound": "android.settings.SOUND_SETTINGS",
    "volume": "android.settings.panel.action.VOLUME",
    "dnd": "android.settings.ZEN_MODE_SETTINGS",
    "battery": "android.intent.action.POWER_USAGE_SUMMARY",
    "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "apps": "android.settings.MANAGE_APPLICATIONS_SETTINGS",
    "app": "android.settings.APPLICATION_DETAILS_SETTINGS",
    "notifications": "android.settings.APP_NOTIFICATION_SETTINGS",
    "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "date": "android.settings.DATE_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS",
    "developer": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
}
INTENT_KINDS = ("share", "dial", "navigate", "map", "timer", "alarm", "alarms", "settings",
                "search", "email", "sms", "camera")
_TRAVEL = {"drive": "driving", "driving": "driving", "car": "driving", "walk": "walking",
           "walking": "walking", "bike": "bicycling", "bicycle": "bicycling",
           "bicycling": "bicycling", "transit": "transit", "bus": "transit", "train": "transit"}


def intent_spec(kind: str, text: str = "", to: str = "", subject: str = "", mode: str = "",
                seconds: int = 0, hour: int | None = None, minute: int = 0, label: str = "",
                page: str = "", package: str = "", start: bool = True) -> dict:
    """What device.intent starts, as plain data (unit-tested): action, data
    uri, MIME type, extras {name: value}, chooser title. ValueError on bad input."""
    k = str(kind or "").strip().lower()
    if k == "share":
        if not text:
            raise ValueError("share needs text")
        ex = {"android.intent.extra.TEXT": str(text)}
        if subject:
            ex["android.intent.extra.SUBJECT"] = str(subject)
        return {"action": "android.intent.action.SEND", "type": "text/plain", "extras": ex,
                "chooser": "Share"}
    if k == "dial":
        if not to:
            raise ValueError("dial needs to (a phone number)")
        return {"action": "android.intent.action.DIAL", "data": "tel:" + quote(str(to), safe="+")}
    if k == "navigate":
        if not to:
            raise ValueError("navigate needs to (an address or place)")
        m = _TRAVEL.get(str(mode or "drive").strip().lower())
        if m is None:
            raise ValueError("mode is drive, walk, bike or transit")
        url = ("https://www.google.com/maps/dir/?api=1&destination=" + quote(str(to), safe="")
               + "&travelmode=" + m + "&dir_action=navigate")
        return {"action": "android.intent.action.VIEW", "data": url}
    if k == "map":
        if not to:
            raise ValueError("map needs to (a place to show)")
        return {"action": "android.intent.action.VIEW", "data": "geo:0,0?q=" + quote(str(to), safe="")}
    if k == "timer":
        secs = int(seconds or 0)
        if not 1 <= secs <= 86400:
            raise ValueError("timer needs seconds (1-86400)")
        ex = {"android.intent.extra.alarm.LENGTH": secs,
              "android.intent.extra.alarm.SKIP_UI": bool(start)}
        if label:
            ex["android.intent.extra.alarm.MESSAGE"] = str(label)
        return {"action": "android.intent.action.SET_TIMER", "extras": ex}
    if k == "alarm":
        if hour is None or hour == "":
            raise ValueError("alarm needs hour (0-23) and minute")
        h, mi = int(hour), int(minute or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError("alarm needs hour 0-23 and minute 0-59")
        ex = {"android.intent.extra.alarm.HOUR": h, "android.intent.extra.alarm.MINUTES": mi,
              "android.intent.extra.alarm.SKIP_UI": bool(start)}
        if label:
            ex["android.intent.extra.alarm.MESSAGE"] = str(label)
        return {"action": "android.intent.action.SET_ALARM", "extras": ex}
    if k == "alarms":
        return {"action": "android.intent.action.SHOW_ALARMS"}
    if k == "settings":
        name = str(page or "main").strip()
        action = name if name.startswith("android.settings.") else SETTINGS_PAGES.get(name.lower())
        if action is None:
            raise ValueError("page is one of " + ", ".join(sorted(SETTINGS_PAGES)))
        if name.lower() == "app":
            if not package:
                raise ValueError("settings page app needs package")
            return {"action": action, "data": "package:" + str(package)}
        if name.lower() == "notifications":
            if not package:
                raise ValueError("settings page notifications needs package")
            return {"action": action, "extras": {"android.provider.extra.APP_PACKAGE": str(package)}}
        return {"action": action}
    if k == "search":
        if not text:
            raise ValueError("search needs text")
        return {"action": "android.intent.action.WEB_SEARCH", "extras": {"query": str(text)}}
    if k == "email":
        q = [f"{n}={quote(str(v), safe='')}" for n, v in (("subject", subject), ("body", text)) if v]
        return {"action": "android.intent.action.SENDTO",
                "data": "mailto:" + quote(str(to or ""), safe="@,+") + ("?" + "&".join(q) if q else "")}
    if k == "sms":
        if not to:
            raise ValueError("sms needs to (a phone number)")
        ex = {"sms_body": str(text)} if text else {}
        return {"action": "android.intent.action.SENDTO", "data": "smsto:" + quote(str(to), safe="+,"),
                "extras": ex}
    if k == "camera":
        return {"action": "android.media.action.STILL_IMAGE_CAMERA"}
    raise ValueError("kind is one of " + ", ".join(INTENT_KINDS))


def pick_app(apps: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """The launcher app ``query`` names: an exact label (case-insensitive), else
    the only label starting with it, else the only label or package containing
    it. Returns (app, candidates) — app is None when none or several match."""
    q = str(query or "").strip().lower()
    if not q:
        return None, []
    for test in (lambda a: a["label"].lower() == q or a["package"].lower() == q,
                 lambda a: a["label"].lower().startswith(q),
                 lambda a: q in a["label"].lower() or q in a["package"].lower()):
        hits = [a for a in apps if test(a)]
        # One app can have two launcher entries (same package): one choice.
        if len({a["package"] for a in hits}) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    return None, []


#: device.sound: stream names -> AudioManager.STREAM_*.
STREAMS = {"call": 0, "system": 1, "ring": 2, "media": 3, "music": 3, "alarm": 4,
           "notification": 5}
RINGER = {0: "silent", 1: "vibrate", 2: "normal"}
DND = {0: "unknown", 1: "off", 2: "priority", 3: "total", 4: "alarms"}


def level_index(pct, lo: int, hi: int) -> int:
    """Volume percent (0-100) -> a stream index in [lo, hi]."""
    p = max(0.0, min(100.0, float(pct)))
    return max(lo, min(hi, round(p * hi / 100.0)))


def _ka(name: str):
    return jclass(f"systems.bake.rook.{name}")


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class AndroidDevicePlugin(Plugin):
    NAMESPACE = "device"
    PLACEMENT = place(ANDROID_APP)
    # Passive keep-awake lives in the app's Settings screen; a value pushed with
    # worker.config_apply (env) is copied there when it changes (last change wins).
    SETTINGS = (
        setting("keep_awake_auto", bool, default=True, scope="worker", env=ENV_KEEP_AWAKE_AUTO,
                label="Keep phone awake while agent is working",
                help="Screen-driving caps (ui.*, hid.*, screenshot.*, device.launch) wake the "
                     "phone and keep it awake until it has been idle for the timeout."),
        setting("keep_awake_idle_minutes", int, default=10, scope="worker",
                env=ENV_KEEP_AWAKE_IDLE, min=1, max=1440,
                label="Keep-awake idle timeout (minutes)"),
    )

    async def start(self) -> None:
        ctx = app_context()
        if ctx is None:
            return
        auto, idle = os.environ.get(ENV_KEEP_AWAKE_AUTO), os.environ.get(ENV_KEEP_AWAKE_IDLE)
        if auto is not None or idle is not None:
            _ka("KeepAwake").applyPushed(ctx, auto, idle)

    def setting_values(self) -> dict:
        """Current values of SETTINGS (the app's preferences), for worker.settings_report."""
        ctx = app_context()
        if ctx is None:
            return {}
        ka = _ka("KeepAwake")
        return {"keep_awake_auto": bool(ka.autoEnabled(ctx)),
                "keep_awake_idle_minutes": int(ka.idleMinutes(ctx))}

    @capability("update", risk="admin", tags=("destructive",), scope="local")
    def _update(self, channel: str = "") -> dict:
        """Queue an APK update from the official Rook feed. Nonblocking.

        Validates package, newer version, hash and matching signing certificate.
        Android may require install permission or notification confirmation.
        Poll device.update_status; the worker reconnects after APK replacement.
        channel: "stable" or "beta" switches the phone's update channel (kept)
        before checking; empty keeps the current one. Beta gets a build only
        once the hub has approved it and it is newer, else stable.
        """
        chosen = str(channel or "").strip().lower()
        if chosen and chosen not in ("stable", "beta"):
            return {"ok": False, "error": "channel must be stable or beta"}
        updater = jclass("systems.bake.rook.ApkUpdater")
        if chosen:
            updater.setChannel(app_context(), chosen)
        return json.loads(str(updater.request(app_context(), False)))

    @capability("update_status", risk="read", scope="local")
    def _update_status(self) -> dict:
        """APK updater progress, installed/target versions, update channel, and required action."""
        return json.loads(str(jclass("systems.bake.rook.ApkUpdater").status(app_context())))

    @capability("find", risk="exec", tags=("physical",), scope="local")
    def _find(self, seconds: int = 30) -> dict:
        """Ring at maximum alarm volume for 1–120 seconds; restores volume after.

        Use device.find_stop to stop early. Device DND policy applies.
        """
        try:
            return json.loads(str(jclass("systems.bake.rook.FindDeviceBridge").ring(app_context(), max(1, min(int(seconds), 120)))))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @capability("find_stop", risk="exec", tags=("physical",), scope="local")
    def _find_stop(self) -> dict:
        """Stop the find-device ring and restore the previous alarm volume."""
        return json.loads(str(jclass("systems.bake.rook.FindDeviceBridge").stop()))

    @capability("info", risk="read", scope="local")
    def _info(self) -> dict:
        """Model, Android version, screen, uptime, and battery-independent bits."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Build = jclass("android.os.Build")
            VER = jclass("android.os.Build$VERSION")
            SystemClock = jclass("android.os.SystemClock")
            out = {
                "ok": True,
                "manufacturer": Build.MANUFACTURER,
                "brand": Build.BRAND,
                "model": Build.MODEL,
                "device": Build.DEVICE,
                "android_release": VER.RELEASE,
                "sdk_int": VER.SDK_INT,
                "uptime_s": round(SystemClock.elapsedRealtime() / 1000.0, 1),
            }
            try:
                Context = jclass("android.content.Context")
                wm = cast(jclass("android.view.WindowManager"), ctx.getSystemService(Context.WINDOW_SERVICE))
                DisplayMetrics = jclass("android.util.DisplayMetrics")
                m = DisplayMetrics()
                wm.getDefaultDisplay().getRealMetrics(m)
                out["screen"] = {"w": m.widthPixels, "h": m.heightPixels, "dpi": m.densityDpi}
            except Exception:
                pass
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("wake", risk="exec", tags=("physical",), scope="local")
    def _wake(self, dismiss_keyguard: bool = False, hold_ms: int = 1200) -> dict:
        """Turn the screen ON (and show over the keyguard) so the accessibility
        service can interact with the lock screen. Needs 'Display over other
        apps'. With dismiss_keyguard, also surfaces the PIN pad (Android won't
        bypass a secured lock — it just brings the credential UI up)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            # Refuse quietly-broken case: overlay perm missing -> background
            # activity start is blocked on Android 10+.
            Settings = jclass("android.provider.Settings")
            if not Settings.canDrawOverlays(ctx):
                return {"ok": False, "error": "grant 'Display over other apps' in the app first"}
            Intent = jclass("android.content.Intent")
            ComponentName = jclass("android.content.ComponentName")
            i = Intent()
            i.setComponent(ComponentName(ctx, "systems.bake.rook.WakeActivity"))
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            i.putExtra("dismiss", bool(dismiss_keyguard))
            i.putExtra("hold_ms", int(hold_ms))
            ctx.startActivity(i)
            return {"ok": True, "dismiss_keyguard": bool(dismiss_keyguard)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("unlock", risk="exec", tags=("sensitive", "physical"), scope="local")
    async def _unlock(self, pin: str = "", hold_ms: int = 9000, pre_swipe: bool = True) -> dict:
        """Unlock a PIN-secured screen: wake, surface the PIN bouncer, and tap
        the digits at keypad positions computed from the live screen size.

        ``pin`` is digits only. Leave it out to use the PIN saved in the app
        (Settings > App > Device unlock PIN): that PIN stays on the phone and the
        reply carries no digits or tap positions. This does NOT bypass anything:
        it enters the real PIN the way a finger would, where the OS lets the
        accessibility service tap on the keyguard (verified on Samsung / One UI).
        Standard 4/6-digit PINs auto-submit; ``unlocked`` says whether the lock
        screen went away.
        """
        from rook_android.unlock import unlock
        given = None if pin is None or not str(pin).strip() else str(pin)
        return await unlock(given, hold_ms=int(hold_ms), pre_swipe=_flag(pre_swipe))

    @capability("unlock_status", risk="read", scope="local")
    def _unlock_status(self) -> dict:
        """Whether an unlock PIN is saved in the app (never the PIN itself), and
        whether passive keep-awake may use it (paused after a failed automatic try)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        return json.loads(str(_ka("UnlockPin").status(ctx)))

    @capability("keep_awake", risk="exec", tags=("physical",), scope="local")
    def _keep_awake(self, on: bool, minutes: int = 30) -> dict:
        """Keep the screen on (so it never locks) for ``minutes`` (1–1440), then
        release; on=false releases now. Wakes the screen if it is off. Holds an
        invisible keep-screen-on window plus a screen wake lock and pokes user
        activity every 20 seconds, so a screen-timeout policy can't lock it. Returns
        the state (see device.keep_awake_status)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            ka = _ka("KeepAwake")
            raw = ka.engage(ctx, int(minutes)) if _flag(on) else ka.release(ctx)
            return json.loads(str(raw))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("keep_awake_status", risk="read", scope="local")
    def _keep_awake_status(self) -> dict:
        """Keep-awake state: on, mode (manual|auto), until (epoch s), remaining_s,
        overlay/wake_lock in use, screen_on, locked, the passive setting, whether
        an unlock PIN is saved, and the device's screen timeout / policy cap."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            return json.loads(str(_ka("KeepAwake").status(ctx)))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("keep_awake_settings", risk="exec", scope="local")
    def _keep_awake_settings(self, auto: bool | None = None, idle_minutes: int | None = None) -> dict:
        """Change the passive setting now (same as the app's Settings screen):
        auto = "Keep phone awake while agent is working" (screen-driving caps wake
        the phone and hold it), idle_minutes = release after this long without
        one (1–1440). Omit a field to leave it. Returns the state."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Bool = jclass("java.lang.Boolean")
            Int = jclass("java.lang.Integer")
            a = None if auto is None or auto == "" else Bool(_flag(auto))
            m = None if idle_minutes is None or idle_minutes == "" else Int(int(idle_minutes))
            return json.loads(str(_ka("KeepAwake").configure(ctx, a, m)))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("vibrate", risk="exec", tags=("physical",), scope="local")
    def _vibrate(self, ms: int = 300) -> dict:
        """Buzz the phone for ``ms`` milliseconds."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            VER = jclass("android.os.Build$VERSION")
            dur = max(1, min(int(ms), 5000))
            if VER.SDK_INT >= 26:
                VibrationEffect = jclass("android.os.VibrationEffect")
                eff = VibrationEffect.createOneShot(dur, VibrationEffect.DEFAULT_AMPLITUDE)
                if VER.SDK_INT >= 31:
                    vm = cast(jclass("android.os.VibratorManager"),
                              ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE))
                    vm.getDefaultVibrator().vibrate(eff)
                else:
                    vib = cast(jclass("android.os.Vibrator"), ctx.getSystemService(Context.VIBRATOR_SERVICE))
                    vib.vibrate(eff)
            else:
                # Android 6/7: VibrationEffect doesn't exist; use the long overload.
                vib = cast(jclass("android.os.Vibrator"), ctx.getSystemService(Context.VIBRATOR_SERVICE))
                vib.vibrate(int(dur))
            return {"ok": True, "ms": dur}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("torch", risk="exec", tags=("physical",), scope="local")
    def _torch(self, on: bool = True) -> dict:
        """Flashlight on/off (no permission needed via CameraManager)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            cm = cast(jclass("android.hardware.camera2.CameraManager"),
                      ctx.getSystemService(Context.CAMERA_SERVICE))
            CC = jclass("android.hardware.camera2.CameraCharacteristics")
            flash_id = None
            for cid in cm.getCameraIdList():
                ch = cm.getCameraCharacteristics(cid)
                has = ch.get(CC.FLASH_INFO_AVAILABLE)
                if has:
                    flash_id = cid
                    break
            if flash_id is None:
                return {"ok": False, "error": "no camera with a flash"}
            cm.setTorchMode(flash_id, bool(on))
            return {"ok": True, "on": bool(on), "camera": flash_id}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("clipboard_set", risk="exec", scope="local")
    def _clip_set(self, text: str) -> dict:
        """Put text on the clipboard."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            ClipData = jclass("android.content.ClipData")
            cm = cast(jclass("android.content.ClipboardManager"),
                      ctx.getSystemService(Context.CLIPBOARD_SERVICE))
            cm.setPrimaryClip(ClipData.newPlainText("rook", str(text)))
            return {"ok": True, "chars": len(str(text))}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("clipboard_get", risk="exec", tags=("sensitive",), scope="local")
    def _clip_get(self) -> dict:
        """Read the clipboard. Android 10+ blocks background reads → may be empty."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            cm = cast(jclass("android.content.ClipboardManager"),
                      ctx.getSystemService(Context.CLIPBOARD_SERVICE))
            clip = cm.getPrimaryClip()
            if clip is None or clip.getItemCount() == 0:
                return {"ok": True, "text": None,
                        "note": "empty (Android blocks background clipboard reads)"}
            item = clip.getItemAt(0)
            cs = item.coerceToText(ctx)
            return {"ok": True, "text": str(cs) if cs is not None else None}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("open_url", risk="exec", scope="local")
    async def _open_url(self, url: str, screen: bool = False) -> dict:
        """Open a URL (or any Uri) in the default handler app. screen=true waits
        for the app to come up and returns the screen (see ui.screen)."""
        return await _then_screen(lambda: self._start_view(url), screen)

    def _start_view(self, url: str) -> dict:
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Intent = jclass("android.content.Intent")
            Uri = jclass("android.net.Uri")
            i = Intent(Intent.ACTION_VIEW, Uri.parse(str(url)))
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            return {"ok": True, "url": str(url)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("launch", risk="exec", scope="local")
    async def _launch(self, package: str = "", label: str = "", screen: bool = False) -> dict:
        """Launch an installed app by package name (e.g. com.android.settings) or by
        its launcher label (label="Maps"; a package with no dot is taken as a
        label). An unclear label -> ok false with the candidates (see device.apps).
        screen=true waits for the app to come up and returns the screen."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        pkg, lab = str(package or "").strip(), str(label or "").strip()
        if not lab and pkg and "." not in pkg:
            pkg, lab = "", pkg
        if not pkg and not lab:
            return {"ok": False, "error": "give package or label"}
        if lab and not pkg:
            try:
                app, hits = pick_app(self._launcher_apps(ctx), lab)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if app is None:
                return {"ok": False, "error": f"no single app is labelled {lab!r}" if hits
                        else f"no launchable app labelled {lab!r}",
                        "candidates": [{"label": a["label"], "package": a["package"]}
                                       for a in hits[:10]]}
            pkg = app["package"]

        def go() -> dict:
            pm = ctx.getPackageManager()
            i = pm.getLaunchIntentForPackage(pkg)
            if i is None:
                return {"ok": False, "error": f"no launchable app for {pkg!r}"}
            Intent = jclass("android.content.Intent")
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            return {"ok": True, "package": pkg, "ts": time.time()}
        return await _then_screen(go, screen)

    def _launcher_apps(self, ctx) -> list[dict]:
        Intent = jclass("android.content.Intent")
        i = Intent(Intent.ACTION_MAIN)
        i.addCategory(Intent.CATEGORY_LAUNCHER)
        pm = ctx.getPackageManager()
        found = pm.queryIntentActivities(i, 0)
        out = []
        for k in range(int(found.size())):
            info = found.get(k).activityInfo
            out.append({"label": str(info.loadLabel(pm)), "package": str(info.packageName)})
        out.sort(key=lambda a: (a["label"].lower(), a["package"]))
        return out

    @capability("apps", risk="read", tags=("sensitive",), scope="local")
    def _apps(self, query: str = "", limit: int = 200) -> dict:
        """Apps on the launcher: label and package, sorted by label. query filters
        by label or package (case-insensitive). Launch one with device.launch
        label= or package=."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            apps = self._launcher_apps(ctx)
            q = str(query or "").strip().lower()
            if q:
                apps = [a for a in apps if q in a["label"].lower() or q in a["package"].lower()]
            n = max(1, int(limit or 200))
            return {"ok": True, "count": len(apps), "truncated": len(apps) > n, "apps": apps[:n]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("intent", risk="exec", scope="local")
    async def _intent(self, kind: str, text: str = "", to: str = "", subject: str = "",
                      mode: str = "", seconds: int = 0, hour: int | None = None, minute: int = 0,
                      label: str = "", page: str = "", package: str = "", start: bool = True,
                      screen: bool = False) -> dict:
        """Common phone actions in one call. kind:
        share (text, subject: the share sheet), dial (to: opens the dialer with the
        number, never calls), navigate (to, mode drive|walk|bike|transit: turn-by-turn
        in Maps), map (to: show a place), timer (seconds, label; start=false only
        opens it), alarm (hour, minute, label; start=false only opens it), alarms
        (the alarm list), settings (page: wifi, bluetooth, display, sound, volume,
        dnd, battery, location, apps, app+package, notifications+package,
        accessibility, internet, airplane, network, nfc, date, storage, security,
        developer, main, or a raw android.settings.* action), search (text: web
        search), email (to, subject, text: opens a draft), sms (to, text: opens a
        draft, never sends), camera. app names the app that took it when Android
        says (not for share's chooser); screen=true returns the screen after it opens."""
        try:
            spec = intent_spec(kind, text=text, to=to, subject=subject, mode=mode,
                               seconds=seconds, hour=hour, minute=minute, label=label,
                               page=page, package=package, start=_flag(start))
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}

        def go() -> dict:
            Intent = jclass("android.content.Intent")
            i = Intent(spec["action"])
            if spec.get("data") and spec.get("type"):
                i.setDataAndType(jclass("android.net.Uri").parse(spec["data"]), spec["type"])
            elif spec.get("data"):
                i.setData(jclass("android.net.Uri").parse(spec["data"]))
            elif spec.get("type"):
                i.setType(spec["type"])
            from java import jint   # AlarmClock reads getIntExtra: an int, not a long
            for name, value in (spec.get("extras") or {}).items():
                i.putExtra(name, value if isinstance(value, (bool, str)) else jint(int(value)))
            handler = None
            if spec.get("chooser"):
                i = Intent.createChooser(i, spec["chooser"])
            else:
                try:   # which app takes it (None when Android hides it or asks)
                    ri = ctx.getPackageManager().resolveActivity(i, 0)
                    handler = str(ri.activityInfo.packageName) if ri is not None else None
                except Exception:
                    handler = None
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            try:
                ctx.startActivity(i)
            except Exception as e:
                if "ActivityNotFound" in type(e).__name__ or "No Activity" in str(e):
                    return {"ok": False, "error": f"no app on this phone handles {kind}"}
                raise
            out = {"ok": True, "kind": str(kind).strip().lower(), "action": spec["action"]}
            if handler and handler != "android":
                out["app"] = handler
            return out
        return await _then_screen(go, screen)

    @capability("sound", risk="exec", tags=("physical",), scope="local")
    def _sound(self, stream: str = "media", level: int | None = None, step: str = "",
               ringer: str = "", dnd: str = "") -> dict:
        """Read or set the volume, ringer and Do Not Disturb. With no change asked, returns the state:
        each stream's level (percent and steps), ringer normal|vibrate|silent, dnd
        off|priority|alarms|total, and whether Rook has Do Not Disturb access.
        level=0-100 sets stream (media, ring, alarm, notification, call, system) to
        that percent; step=up|down|mute|unmute nudges it. ringer=normal|vibrate|silent
        and dnd=off|priority|alarms|total need Do Not Disturb access (granted in
        the app) for silent and dnd. Changes show no volume panel."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        name = str(stream or "media").strip().lower()
        if name not in STREAMS:
            return {"ok": False, "error": "stream is one of " + ", ".join(sorted(STREAMS))}
        st = str(step or "").strip().lower()
        if st and st not in ("up", "down", "mute", "unmute"):
            return {"ok": False, "error": "step is up, down, mute or unmute"}
        rg = str(ringer or "").strip().lower()
        if rg and rg not in RINGER.values():
            return {"ok": False, "error": "ringer is normal, vibrate or silent"}
        dz = str(dnd or "").strip().lower()
        dz = "off" if dz in ("none", "false", "0") else dz
        if dz and dz not in ("off", "priority", "alarms", "total"):
            return {"ok": False, "error": "dnd is off, priority, alarms or total"}
        try:
            Context = jclass("android.content.Context")
            VER = jclass("android.os.Build$VERSION")
            am = cast(jclass("android.media.AudioManager"), ctx.getSystemService(Context.AUDIO_SERVICE))
            nm = cast(jclass("android.app.NotificationManager"),
                      ctx.getSystemService(Context.NOTIFICATION_SERVICE))
            access = bool(nm.isNotificationPolicyAccessGranted()) if VER.SDK_INT >= 23 else True
            changed = []
            code = STREAMS[name]
            try:
                if level is not None and level != "":
                    lo = int(am.getStreamMinVolume(code)) if VER.SDK_INT >= 28 else 0
                    am.setStreamVolume(code, level_index(level, lo, int(am.getStreamMaxVolume(code))), 0)
                    changed.append("level")
                if st:
                    adj = {"up": 1, "down": -1, "mute": -100, "unmute": 100}[st]
                    am.adjustStreamVolume(code, adj, 0)
                    changed.append("step")
                if rg:
                    am.setRingerMode({v: k for k, v in RINGER.items()}[rg])
                    changed.append("ringer")
                if dz:
                    nm.setInterruptionFilter({"off": 1, "priority": 2, "total": 3, "alarms": 4}[dz])
                    changed.append("dnd")
            except Exception as e:
                if "Security" in type(e).__name__ or "Not allowed" in str(e):
                    return {"ok": False, "error": "needs Do Not Disturb access (grant it in the app)",
                            "changed": changed, **self._sound_state(am, nm, access, VER)}
                raise
            return {"ok": True, **({"changed": changed} if changed else {}),
                    **self._sound_state(am, nm, access, VER)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _sound_state(am, nm, access: bool, VER) -> dict:
        streams = {}
        for n in ("media", "ring", "notification", "alarm", "call"):
            c = STREAMS[n]
            cur, hi = int(am.getStreamVolume(c)), int(am.getStreamMaxVolume(c))
            streams[n] = {"pct": round(cur * 100 / hi) if hi else 0, "level": cur, "max": hi}
        return {"streams": streams, "ringer": RINGER.get(int(am.getRingerMode()), "unknown"),
                "dnd": DND.get(int(nm.getCurrentInterruptionFilter()), "unknown")
                if VER.SDK_INT >= 23 else "unknown",
                "dnd_access": access}


async def _then_screen(go, screen) -> dict:
    """Run ``go()`` (starts an activity); with screen=true, wait for the screen to
    change and settle and add it to the reply (ui.screen's summary)."""
    def run() -> dict:
        try:
            return go()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not _flag(screen):
        return run()
    from rook_android import screenwait
    bridge = screenwait.hid()
    before = await screenwait.rev(bridge) if bridge is not None else None
    app = (await screenwait.front(bridge) or "") if bridge is not None else ""
    out = run()
    if out.get("ok") and bridge is not None:
        # Wait for another app in front; opening a link in the app already in
        # front (or the launcher's own page) falls back to any change after 2 s.
        await screenwait.with_screen(out, before, bridge, before_app=app)
    elif out.get("ok"):
        out["screen"] = {"error": "accessibility service not enabled (grant it in the app)"}
    return out


PLUGIN = AndroidDevicePlugin
