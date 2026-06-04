"""screenshot.* — capture the local display.

Cross-platform with a runtime-detected backend:

  Linux   : scrot (preferred) → ImageMagick ``import`` (fallback)
  Windows : ``mss`` package → ``pyautogui`` (fallback)
  Android : ``termux-camera-photo`` (no real screen-grab API in Termux)

All capabilities return a uniform dict:

    {"ok": true, "format": "jpeg", "width": W, "height": H,
     "size_bytes": N, "data": "<base64>"}

Errors return ``{"ok": False, "error": "..."}``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile

from ..plugin import Plugin, capability


def _is_termux() -> bool:
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux")


def _jpeg_dims(data: bytes) -> tuple[int, int]:
    """Parse a JPEG's SOFn marker to get (width, height). Returns (0, 0) on failure."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return (0, 0)
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            return (0, 0)
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        # SOF0..SOF15 except DHT(C4), JPG(C8), DAC(CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if seg_len < 2:
            return (0, 0)
        i += 2 + seg_len
    return (0, 0)


def _pack(data: bytes) -> dict:
    w, h = _jpeg_dims(data)
    return {
        "ok": True,
        "format": "jpeg",
        "width": w,
        "height": h,
        "size_bytes": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


async def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, bytes, bytes]:
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
