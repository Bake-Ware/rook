"""device.* — grab-bag device controls that need no special service.

info, vibrate, torch (flashlight), clipboard get/set, open a URL, launch an app,
wake/unlock, and keep-awake (device.keep_awake; passive mode is in
rook_android.keepawake). Most need no permission; VIBRATE is a normal (auto-granted) permission. Clipboard
*read* is blocked in the background on Android 10+, so device.clipboard_get is
best-effort.
"""

from __future__ import annotations

import json
import os
import time

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place, setting
from rook_android.androidctx import app_context, jclass, cast

ENV_KEEP_AWAKE_AUTO = "ROOK_KEEP_AWAKE_AUTO"
ENV_KEEP_AWAKE_IDLE = "ROOK_KEEP_AWAKE_IDLE_MINUTES"


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
    def _open_url(self, url: str) -> dict:
        """Open a URL (or any Uri) in the default handler app."""
        return self._start_view(url)

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
    def _launch(self, package: str) -> dict:
        """Launch an installed app by package name (e.g. com.android.settings)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            pm = ctx.getPackageManager()
            i = pm.getLaunchIntentForPackage(str(package))
            if i is None:
                return {"ok": False, "error": f"no launchable app for {package!r}"}
            Intent = jclass("android.content.Intent")
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            return {"ok": True, "package": str(package), "ts": time.time()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidDevicePlugin
