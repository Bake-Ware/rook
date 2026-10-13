"""screenshot.* — cross-platform display capture for fallback modality.

Backend detection per platform:
  Linux   : scrot → ImageMagick import (X11/Wayland)
  Windows : mss → pyautogui (Win32 API)
  Android : termux-camera-photo (Termux)

Failures raise (the host reports them as the reply's ``error``).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

from ...core.pluginkit import fail, has_display, is_termux, is_windows, require_int, run, which
from ..plugin import ANDROID_APP_REPLACED, Plugin, capability, place


def _pack(data: bytes) -> dict[str, Any]:
    import base64
    return {"ok": True, "format": "jpeg", "data": base64.b64encode(data).decode("ascii"),
            "bytes": len(data)}


async def _shrink(path: str, quality: int, max_dim: int = 1600) -> None:
    """Downscale + re-encode the capture in place so the base64 payload stays
    small enough to relay reliably over the band. Some grabbers (e.g. spectacle)
    ignore the quality flag and emit multi-hundred-KB frames. Best-effort via
    ImageMagick; a no-op if it isn't installed."""
    conv = which("magick", "convert")
    if not conv:
        return
    out = path + ".s.jpg"
    try:
        code, _, _ = await run(
            [conv, path, "-resize", f"{max_dim}x{max_dim}>", "-quality", str(quality), out],
            timeout=10)
        if code == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            os.replace(out, path)
    except Exception:
        pass
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


# ---------- Linux -----------------------------------------------------------

async def _linux_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    if not has_display():
        fail("no DISPLAY/WAYLAND_DISPLAY set")
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-shot-")
    os.close(fd)
    wl = bool(os.environ.get("WAYLAND_DISPLAY"))

    # Screenshot tooling is compositor-specific — wlroots→grim, KDE→spectacle,
    # GNOME→gnome-screenshot, X11→scrot/import. Build an ordered list of every
    # available backend and try each until one produces a non-empty file, so a
    # box where (say) grim exists but the compositor rejects wlr-screencopy
    # still succeeds via spectacle.
    backends: list[tuple[str, list[str]]] = []
    if wl and which("grim"):
        g = ["grim", "-t", "jpeg", "-q", str(quality)]
        if region:
            x, y, w, h = region
            g += ["-g", f"{x},{y} {w}x{h}"]
        backends.append(("grim", g + [path]))
    if not region and which("spectacle"):
        backends.append(("spectacle", ["spectacle", "-b", "-n", "-o", path]))
    if not region and which("gnome-screenshot"):
        backends.append(("gnome-screenshot", ["gnome-screenshot", "-f", path]))
    if which("scrot"):
        s = ["scrot", "-q", str(quality), "-o"]
        if region:
            x, y, w, h = region
            s += ["-a", f"{x},{y},{w},{h}"]
        backends.append(("scrot", s + [path]))
    if which("import"):
        i = ["import", "-window", "root", "-quality", str(quality)]
        if region:
            x, y, w, h = region
            i += ["-crop", f"{w}x{h}{x:+d}{y:+d}"]  # signed: WxH-10+0, not WxH+-10+0
        backends.append(("import", i + [path]))

    if not backends:
        try:
            os.unlink(path)
        except OSError:
            pass
        fail("no screenshot backend (install grim / spectacle / scrot / imagemagick)")

    errs = []
    try:
        for name, cmd in backends:
            try:
                code, _, err = await run(cmd, timeout=15)
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}: {e}")
                continue
            if code == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
                await _shrink(path, quality)
                with open(path, "rb") as f:
                    return _pack(f.read())
            errs.append(f"{name}: {err.strip() or f'exit {code}'}")
            try:
                open(path, "wb").close()   # clear for the next backend
            except OSError:
                pass
        fail("all screenshot backends failed — " + " | ".join(errs))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------- Windows ----------------------------------------------------------

def _windows_capture_sync(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    try:
        import io
        try:
            import mss  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError:
            mss = None
        if mss is not None:
            with mss.mss() as sct:
                if region:
                    x, y, w, h = region
                    mon = {"left": x, "top": y, "width": w, "height": h}
                else:
                    mon = sct.monitors[1]  # primary
                raw = sct.grab(mon)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
        else:
            import pyautogui  # type: ignore
            from PIL import Image  # type: ignore  # noqa: F401
            shot = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
            img = shot.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return _pack(buf.getvalue())
    except ImportError as e:
        fail(f"missing dependency: {e.name} (pip install mss pillow)")


async def _windows_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    return await asyncio.to_thread(_windows_capture_sync, quality, region)


# ---------- Android ----------------------------------------------------------

async def _android_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    if not which("termux-camera-photo"):
        fail("termux-camera-photo not found (install termux-api)")
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-shot-")
    os.close(fd)
    try:
        code, _, err = await run(["termux-camera-photo", "-c", "0", path], timeout=20.0)
        if code != 0:
            fail(f"termux-camera-photo failed: {err.strip()}")
        with open(path, "rb") as f:
            data = f.read()
        if region:
            # Region crop on Android requires PIL — best-effort only.
            try:
                import io
                from PIL import Image  # type: ignore
                x, y, w, h = region
                img = Image.open(io.BytesIO(data)).convert("RGB").crop((x, y, x + w, y + h))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                data = buf.getvalue()
            except ImportError:
                pass  # return uncropped frame
        return _pack(data)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------- Dispatch ---------------------------------------------------------

async def _dispatch(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    if is_termux():
        return await _android_capture(quality, region)
    if is_windows():
        return await _windows_capture(quality, region)
    if sys.platform.startswith("linux"):
        return await _linux_capture(quality, region)
    fail(f"unsupported platform: {sys.platform}")


class ScreenshotPlugin(Plugin):
    NAMESPACE = "screenshot"
    PLACEMENT = place(ANDROID_APP_REPLACED)  # the APK has native replacements

    def available(self) -> bool:
        if is_termux():
            return bool(which("termux-camera-photo"))
        if is_windows():
            try:
                import mss  # noqa: F401
                return True
            except ImportError:
                try:
                    import pyautogui  # noqa: F401
                    return True
                except ImportError:
                    return False
        if sys.platform.startswith("linux"):
            if not has_display():
                return False  # headless / no logged-in graphical session
            return any(which(t) for t in
                       ("grim", "spectacle", "gnome-screenshot", "scrot", "import"))
        return False

    @capability("capture", risk="read", tags=("sensitive",))
    async def _capture(self, quality: int = 85) -> dict:
        """Capture the full primary display as a JPEG."""
        return await _dispatch(require_int(quality, "quality", min=1, max=100), None)

    @capability("capture_region", risk="read", tags=("sensitive",))
    async def _capture_region(self, x: int, y: int, w: int, h: int,
                              quality: int = 85) -> dict:
        """Capture a rectangular region of the screen (x/y may be negative on multi-monitor desktops)."""
        # x/y are virtual-desktop coordinates: a monitor left of or above
        # the primary one has negative ones.
        region = (require_int(x, "x"), require_int(y, "y"),
                  require_int(w, "w", min=1), require_int(h, "h", min=1))
        return await _dispatch(require_int(quality, "quality", min=1, max=100), region)

    @capability("capture_preview", risk="read", tags=("sensitive",))
    async def _capture_preview(self) -> dict:
        """Low-quality full-screen grab for quick visual checks."""
        return await _dispatch(40, None)


PLUGIN = ScreenshotPlugin
