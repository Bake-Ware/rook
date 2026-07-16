"""screenshot.* — cross-platform display capture for fallback modality.

Backend detection per platform:
  Linux   : scrot → ImageMagick import (X11/Wayland)
  Windows : mss → pyautogui (Win32 API)
  Android : termux-camera-photo (Termux)

All capabilities return {"ok": True, ...} or {"ok": False, "error": ...}.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from typing import Any

from ..plugin import Plugin, capability


def _is_termux() -> bool:
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux")


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode or 0, out, err


def _pack(data: bytes) -> dict[str, Any]:
    import base64
    return {"ok": True, "format": "jpeg", "data": base64.b64encode(data).decode("ascii"),
            "bytes": len(data)}


async def _shrink(path: str, quality: int, max_dim: int = 1600) -> None:
    """Downscale + re-encode the capture in place so the base64 payload stays
    small enough to relay reliably over the band. Some grabbers (e.g. spectacle)
    ignore the quality flag and emit multi-hundred-KB frames. Best-effort via
    ImageMagick; a no-op if it isn't installed."""
    conv = shutil.which("magick") or shutil.which("convert")
    if not conv:
        return
    out = path + ".s.jpg"
    try:
        code, _, _ = await _run(
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
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return {"ok": False, "error": "no DISPLAY/WAYLAND_DISPLAY set"}
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-shot-")
    os.close(fd)
    wl = bool(os.environ.get("WAYLAND_DISPLAY"))

    # Screenshot tooling is compositor-specific — wlroots→grim, KDE→spectacle,
    # GNOME→gnome-screenshot, X11→scrot/import. Build an ordered list of every
    # available backend and try each until one produces a non-empty file, so a
    # box where (say) grim exists but the compositor rejects wlr-screencopy
    # still succeeds via spectacle.
    backends: list[tuple[str, list[str]]] = []
    if wl and shutil.which("grim"):
        g = ["grim", "-t", "jpeg", "-q", str(quality)]
        if region:
            x, y, w, h = region
            g += ["-g", f"{x},{y} {w}x{h}"]
        backends.append(("grim", g + [path]))
    if not region and shutil.which("spectacle"):
        backends.append(("spectacle", ["spectacle", "-b", "-n", "-o", path]))
    if not region and shutil.which("gnome-screenshot"):
        backends.append(("gnome-screenshot", ["gnome-screenshot", "-f", path]))
    if shutil.which("scrot"):
        s = ["scrot", "-q", str(quality), "-o"]
        if region:
            x, y, w, h = region
            s += ["-a", f"{x},{y},{w},{h}"]
        backends.append(("scrot", s + [path]))
    if shutil.which("import"):
        i = ["import", "-window", "root", "-quality", str(quality)]
        if region:
            x, y, w, h = region
            i += ["-crop", f"{w}x{h}+{x}+{y}"]
        backends.append(("import", i + [path]))

    if not backends:
        try:
            os.unlink(path)
        except OSError:
            pass
        return {"ok": False, "error": "no screenshot backend (install grim / spectacle / scrot / imagemagick)"}

    errs = []
    try:
        for name, cmd in backends:
            try:
                code, _, err = await _run(cmd, timeout=15)
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}: {e}")
                continue
            if code == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
                await _shrink(path, quality)
                with open(path, "rb") as f:
                    return _pack(f.read())
            errs.append(f"{name}: {err.decode(errors='replace').strip() or f'exit {code}'}")
            try:
                open(path, "wb").close()   # clear for the next backend
            except OSError:
                pass
        return {"ok": False, "error": "all screenshot backends failed — " + " | ".join(errs)}
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
        return {"ok": False, "error": f"missing dependency: {e.name} (pip install mss pillow)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _windows_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    return await asyncio.to_thread(_windows_capture_sync, quality, region)


# ---------- Android ----------------------------------------------------------

async def _android_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    if not shutil.which("termux-camera-photo"):
        return {"ok": False, "error": "termux-camera-photo not found (install termux-api)"}
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-shot-")
    os.close(fd)
    try:
        code, _, err = await _run(["termux-camera-photo", "-c", "0", path], timeout=20.0)
        if code != 0:
            return {"ok": False, "error": f"termux-camera-photo failed: {err.decode(errors='replace').strip()}"}
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
    if _is_termux():
        return await _android_capture(quality, region)
    if sys.platform == "win32":
        return await _windows_capture(quality, region)
    if sys.platform.startswith("linux"):
        return await _linux_capture(quality, region)
    return {"ok": False, "error": f"unsupported platform: {sys.platform}"}


class ScreenshotPlugin(Plugin):
    NAMESPACE = "screenshot"

    @capability("capture")
    async def _capture(self, quality: int = 85) -> dict:
        """Capture the full primary display as a JPEG."""
        return await _dispatch(int(quality), None)

    @capability("capture_region")
    async def _capture_region(self, x: int, y: int, w: int, h: int,
                              quality: int = 85) -> dict:
        """Capture a rectangular region of the screen."""
        return await _dispatch(int(quality), (int(x), int(y), int(w), int(h)))

    @capability("capture_preview")
    async def _capture_preview(self) -> dict:
        """Low-quality full-screen grab for quick visual checks."""
        return await _dispatch(40, None)


PLUGIN = ScreenshotPlugin
