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
    return {"ok": True, "format": "jpeg", "data": base64.b64encode(data).decode("ascii")}


# ---------- Linux -----------------------------------------------------------

async def _linux_capture(quality: int, region: tuple[int, int, int, int] | None) -> dict:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return {"ok": False, "error": "no DISPLAY/WAYLAND_DISPLAY set"}
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-shot-")
    os.close(fd)
    try:
        if shutil.which("scrot"):
            cmd = ["scrot", "-q", str(quality), "-o"]
            if region:
                x, y, w, h = region
                cmd += ["-a", f"{x},{y},{w},{h}"]
            cmd.append(path)
            code, _, err = await _run(cmd)
            if code != 0:
                return {"ok": False, "error": f"scrot failed: {err.decode(errors='replace').strip()}"}
        elif shutil.which("import"):
            # ImageMagick. -window root grabs the whole screen; -crop for region.
            cmd = ["import", "-window", "root", "-quality", str(quality)]
            if region:
                x, y, w, h = region
                cmd += ["-crop", f"{w}x{h}+{x}+{y}"]
            cmd.append(path)
            code, _, err = await _run(cmd)
            if code != 0:
                return {"ok": False, "error": f"import failed: {err.decode(errors='replace').strip()}"}
        else:
            return {"ok": False, "error": "no screenshot backend (install scrot or imagemagick)"}
        with open(path, "rb") as f:
            data = f.read()
        return _pack(data)
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
