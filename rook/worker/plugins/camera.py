"""camera.* — grab a still photo from a webcam / camera on the worker.

Distinct from `screenshot.*` (which captures the *display*): this captures a
physical camera. Loads only where a camera + a capture tool is present.

Backends:
  Linux   : fswebcam → ffmpeg (v4l2), devices are /dev/video0, /dev/video1, …
  Android : termux-camera-photo (Termux), camera 0 = back, 1 = front
  Windows : ffmpeg (dshow)

    camera.list()                     -> available cameras
    camera.snap(camera=0, ...)        -> JPEG still (base64) from that camera
"""

from __future__ import annotations

import glob
import os
import sys
import tempfile
from typing import Any

from ...core.pluginkit import fail, is_termux, is_windows, require_int, run, which
from ..plugin import Plugin, capability


def _pack(data: bytes, **extra) -> dict[str, Any]:
    import base64
    return {"ok": True, "format": "jpeg", "bytes": len(data),
            "data": base64.b64encode(data).decode("ascii"), **extra}


async def _shrink(path: str, quality: int, max_dim: int = 1600) -> None:
    """Downscale/re-encode in place so the base64 payload relays over the band.
    Best-effort via ImageMagick; a no-op if it isn't installed."""
    conv = which("magick", "convert")
    if not conv:
        return
    out = path + ".s.jpg"
    try:
        code, _, _ = await run([conv, path, "-resize", f"{max_dim}x{max_dim}>",
                                 "-quality", str(quality), out], timeout=10)
        if code == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            os.replace(out, path)
    except Exception:
        pass
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _linux_device(camera) -> str:
    """Resolve the `camera` arg to a /dev/videoN path."""
    s = str(camera).strip()
    if s.startswith("/dev/"):
        return s
    return f"/dev/video{int(s)}" if s.isdigit() else f"/dev/video{s}"


async def _linux_snap(camera, quality: int, resolution: str) -> dict:
    dev = _linux_device(camera)
    if not os.path.exists(dev):
        fail(f"no such camera device: {dev} (try camera.list)")
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-cam-")
    os.close(fd)
    try:
        if which("fswebcam"):
            code, _out, err = await run(
                ["fswebcam", "-d", dev, "--no-banner", "-q", "-r", resolution,
                 "--jpeg", str(int(quality)), path], timeout=25)
        elif which("ffmpeg"):
            # -frames:v 3 then keep the last: skip the first (often dark) frames.
            code, _out, err = await run(
                ["ffmpeg", "-y", "-f", "v4l2", "-video_size", resolution, "-i", dev,
                 "-frames:v", "1", "-q:v", "3", path], timeout=25)
        else:
            fail("no capture tool — install fswebcam or ffmpeg")
        if code == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            await _shrink(path, quality)
            with open(path, "rb") as f:
                return _pack(f.read(), camera=dev)
        fail(err.strip() or f"capture failed (exit {code})")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _termux_snap(camera, quality: int) -> dict:
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-cam-")
    os.close(fd)
    try:
        code, _out, err = await run(
            ["termux-camera-photo", "-c", str(require_int(camera, "camera", min=0)), path],
            timeout=30)
        if code != 0 or not os.path.getsize(path):
            fail(err.strip() or f"termux-camera-photo exit {code}")
        await _shrink(path, quality)
        with open(path, "rb") as f:
            return _pack(f.read(), camera=int(camera))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _windows_snap(camera, quality: int, resolution: str) -> dict:
    ff = which("ffmpeg")
    if not ff:
        fail("ffmpeg required on Windows")
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="rook-cam-")
    os.close(fd)
    try:
        # `camera` is the dshow device name (see camera.list); index falls back to first.
        name = str(camera) if not str(camera).isdigit() else None
        src = f"video={name}" if name else "video=Integrated Camera"
        code, _out, err = await run(
            [ff, "-y", "-f", "dshow", "-video_size", resolution, "-i", src,
             "-frames:v", "1", "-q:v", "3", path], timeout=25)
        if code == 0 and os.path.getsize(path) > 0:
            await _shrink(path, quality)
            with open(path, "rb") as f:
                return _pack(f.read(), camera=src)
        fail(err.strip() or f"ffmpeg exit {code}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class CameraPlugin(Plugin):
    NAMESPACE = "camera"

    def available(self) -> bool:
        if is_termux():
            return bool(which("termux-camera-photo"))
        if sys.platform.startswith("linux"):
            return bool(glob.glob("/dev/video*")) and bool(which("fswebcam", "ffmpeg"))
        if is_windows():
            return bool(which("ffmpeg"))
        return False

    @capability("list", risk="read")
    async def _list(self) -> dict:
        """List the cameras on this worker (use one as the ``camera`` arg to snap)."""
        if is_termux():
            if which("termux-camera-info"):
                code, out, _ = await run(["termux-camera-info"], timeout=10)
                if code == 0:
                    import json
                    try:
                        return {"ok": True, "cameras": json.loads(out)}
                    except Exception:
                        pass
            return {"ok": True, "cameras": [{"id": 0, "facing": "back"},
                                            {"id": 1, "facing": "front"}]}
        if sys.platform.startswith("linux"):
            devs = sorted(glob.glob("/dev/video*"))
            names: dict[str, str] = {}
            if which("v4l2-ctl"):
                code, out, _ = await run(["v4l2-ctl", "--list-devices"], timeout=8)
                cur = None
                for line in out.splitlines():
                    if line and not line[0].isspace():
                        cur = line.strip().rstrip(":")
                    elif line.strip().startswith("/dev/video") and cur:
                        names.setdefault(line.strip(), cur)
            cams = []
            for d in devs:
                tail = d.replace("/dev/video", "")
                cams.append({"index": int(tail) if tail.isdigit() else None,
                             "device": d, "name": names.get(d)})
            return {"ok": True, "cameras": cams}
        return {"ok": True, "cameras": []}

    @capability("snap", risk="read", tags=("sensitive", "physical"))
    async def _snap(self, camera=0, quality: int = 85,
                    resolution: str = "1280x720") -> dict:
        """Capture a still JPEG from a camera.

        Args:
            camera: which camera. Linux: a device index (``0``, ``1``, …) or a
                ``/dev/videoN`` path; Termux: ``0`` (back) or ``1`` (front);
                Windows: the dshow device name. See ``camera.list``.
            quality: JPEG quality 1–100.
            resolution: capture size, ``WxH`` (e.g. ``1280x720``).

        Returns ``{ok, format:"jpeg", data:<base64>, bytes, camera}``.
        """
        quality = require_int(quality, "quality", min=1, max=100)
        if is_termux():
            return await _termux_snap(camera, quality)
        if sys.platform.startswith("linux"):
            return await _linux_snap(camera, quality, str(resolution))
        if is_windows():
            return await _windows_snap(camera, quality, str(resolution))
        fail(f"unsupported platform: {sys.platform}")


PLUGIN = CameraPlugin
