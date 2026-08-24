"""device.* — grab-bag device controls that need no special service.

info, vibrate, torch (flashlight), clipboard get/set, open a URL, launch an app.
Most need no permission; VIBRATE is a normal (auto-granted) permission. Clipboard
*read* is blocked in the background on Android 10+, so device.clipboard_get is
best-effort.
"""

from __future__ import annotations

import time

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, cast


class AndroidDevicePlugin(Plugin):
    NAMESPACE = "device"

    @capability("info")
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

    @capability("wake")
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

    @capability("unlock")
    async def _unlock(self, pin: str, hold_ms: int = 9000, pre_swipe: bool = True) -> dict:
        """Unlock a PIN-secured screen: wake, surface the PIN bouncer, and tap
        the digits at keypad positions computed from the live screen size.

        ``pin`` is digits only, passed per-call (never stored in the app). This
        does NOT bypass anything — it enters the real PIN the way a finger would,
        so it only works with the correct PIN and where the OS lets the
        accessibility service inject taps on the keyguard (verified on Samsung /
        One UI). Standard 4/6-digit PINs auto-submit; a variable-length PIN may
        need an explicit OK/enter the phone shows.
        """
        import asyncio
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        pin = str(pin).strip()
        if not pin or not pin.isdigit():
            return {"ok": False, "error": "pin must be digits only"}
        try:
            Hid = jclass("systems.bake.rook.HidAccessibilityService")
            if not Hid.isEnabled():
                return {"ok": False, "error": "accessibility (HID) not enabled — grant it in the app"}
            Settings = jclass("android.provider.Settings")
            if not Settings.canDrawOverlays(ctx):
                return {"ok": False, "error": "grant 'Display over other apps' first"}

            # Live screen size → keypad coordinate model.
            Context = jclass("android.content.Context")
            wm = cast(jclass("android.view.WindowManager"), ctx.getSystemService(Context.WINDOW_SERVICE))
            DisplayMetrics = jclass("android.util.DisplayMetrics")
            m = DisplayMetrics()
            wm.getDefaultDisplay().getRealMetrics(m)
            w, h = int(m.widthPixels), int(m.heightPixels)

            # Wake + surface the PIN bouncer (requestDismissKeyguard).
            Intent = jclass("android.content.Intent")
            ComponentName = jclass("android.content.ComponentName")
            i = Intent()
            i.setComponent(ComponentName(ctx, "systems.bake.rook.WakeActivity"))
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            i.putExtra("dismiss", True)
            i.putExtra("hold_ms", int(hold_ms))
            ctx.startActivity(i)
            await asyncio.sleep(1.3)  # let the screen wake + bouncer come up

            # A nudge swipe helps on setups that show the clock before the pad.
            if pre_swipe:
                try:
                    Hid.swipe(int(w * 0.5), int(h * 0.85), int(w * 0.5), int(h * 0.2), 200)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            # Samsung/One UI portrait PIN bouncer: 3 cols, rows for 1-9 then 0.
            colx = [round(0.25 * w), round(0.5 * w), round(0.75 * w)]
            rowy = [round(0.641 * h), round(0.718 * h), round(0.795 * h), round(0.872 * h)]
            pos = {str(d): (colx[(d - 1) % 3], rowy[(d - 1) // 3]) for d in range(1, 10)}
            pos["0"] = (colx[1], rowy[3])

            taps = []
            for ch in pin:
                x, y = pos[ch]
                ok = bool(Hid.tap(int(x), int(y)))
                taps.append({"d": ch, "x": x, "y": y, "ok": ok})
                await asyncio.sleep(0.28)

            return {"ok": True, "screen": {"w": w, "h": h}, "digits": len(pin),
                    "taps": taps,
                    "note": "4/6-digit PINs auto-submit; verify with ui.text"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("vibrate")
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

    @capability("torch")
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

    @capability("clipboard_set")
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

    @capability("clipboard_get")
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

    @capability("open_url")
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

    @capability("launch")
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
