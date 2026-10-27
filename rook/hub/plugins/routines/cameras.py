"""Camera review: grab a frame from each camera, compare it with the last one
cheaply on the hub, and only when the picture changed ask a vision-capable
model (a model provider on Intelligence sources > Model providers) whether
anything notable happened. Reports notable events only.

A camera is one of:

* ``{"name": "porch", "url": "http://.../snapshot.jpg"}``: the hub fetches a
  JPEG snapshot URL (a camera's own snapshot endpoint, Frigate's
  ``/api/<cam>/latest.jpg``, go2rtc's ``/api/frame.jpeg?src=<cam>``,
  Scrypted, ...). Credentials go in as ``{{secret:name}}`` (the whole URL, or
  ``headers`` such as ``{"Authorization": "Bearer {{secret:cam-token}}"}``);
* ``{"name": "garage", "worker": "nas", "rtsp": "rtsp://..."}``: a worker
  with ``shell.exec``, ``ffmpeg`` and ``base64`` (Linux or macOS) grabs one
  frame from an RTSP stream;
* ``{"name": "desk", "worker": "kaiju", "camera": 0}``: ``camera.snap`` on a
  worker with a webcam (or a phone running Termux).

Ring has no local API: bring a bridge that exposes the cameras as RTSP or a
snapshot URL (ring-mqtt with its go2rtc stream, or Scrypted with the Ring
plugin) and point a camera entry at it.

The change test: each frame is shrunk to 64x36 greyscale and compared with the
camera's previous frame (mean absolute difference, 0 to 1); below
``threshold`` (0.06 by default) nothing is sent to a model. The first frame of
a camera is its baseline. Frames are kept on the hub only as the last small
JPEG per camera (in the plugin's state directory), for the next comparison.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any

from .common import band_call, clip, find_worker

log = logging.getLogger("rook.hub.plugins.routines.cameras")

THUMB = (64, 36)
KEEP_DIM = 640
MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 20.0
MODEL_TIMEOUT = 90.0
MAX_CAMERAS = 12
NAME_RE = re.compile(r"[^a-z0-9_-]+")
LEVELS = ("nothing", "notable", "urgent")

SYSTEM = ("You review frames from a home's security cameras for the person who lives there. "
          "You see the camera's previous frame and its current one. Say what changed in one plain "
          "sentence. Notable: a person, a vehicle arriving or leaving, a package, an animal in the "
          "house, a door or gate opened, smoke, fire or water. Not notable: light, shadows, weather, "
          "trees moving, the picture shifting, noise. Urgent only for fire, smoke, flooding or a "
          "break-in in progress. Answer with JSON only: "
          "{\"level\": \"nothing|notable|urgent\", \"what\": \"<one sentence>\"}.")


def slug(name: str) -> str:
    return NAME_RE.sub("-", str(name or "").lower()).strip("-")[:40] or "camera"


def check_cameras(cameras: Any) -> list[dict]:
    """The ``cameras`` argument, validated; raises ValueError saying what to fix."""
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("cameras: a list of cameras, each {name, url} or {name, worker, rtsp} or "
                         "{name, worker, camera}")
    if len(cameras) > MAX_CAMERAS:
        raise ValueError(f"cameras: at most {MAX_CAMERAS}")
    out, seen = [], set()
    for i, c in enumerate(cameras):
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            raise ValueError(f"cameras[{i}]: needs a name")
        kinds = [k for k in ("url", "rtsp", "camera") if c.get(k) not in (None, "")]
        if len(kinds) != 1:
            raise ValueError(f"cameras[{i}] ({c['name']}): give exactly one of url, rtsp or camera")
        if kinds[0] in ("rtsp", "camera") and not str(c.get("worker") or "").strip():
            raise ValueError(f"cameras[{i}] ({c['name']}): {kinds[0]} needs the worker that grabs it")
        if kinds[0] == "url" and not str(c["url"]).startswith(("http://", "https://", "{{secret:")):
            raise ValueError(f"cameras[{i}] ({c['name']}): url must be http(s):// (or a {{{{secret:name}}}})")
        key = slug(c["name"])
        if key in seen:
            raise ValueError(f"cameras[{i}]: two cameras named {c['name']!r}")
        seen.add(key)
        out.append({**c, "kind": kinds[0], "key": key})
    return out


async def http_get(url: str, headers: dict, timeout: float) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.get(url, headers=headers or {}) as r:
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}")
            data = await r.content.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise RuntimeError("the snapshot is larger than 8 MB")
            return data


RTSP_SH = ('ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp -i "$1" -frames:v 1 '
           '-vf "scale=\'min(1280,iw)\':-2" -f image2 -c:v mjpeg - | base64 | tr -d "\\n"')


class Cameras:
    def __init__(self, node: Any, *, http=http_get) -> None:
        self.node = node
        self.http = http

    async def grab(self, cam: dict) -> bytes:
        kind = cam["kind"]
        if kind == "url":
            url = str(cam["url"])
            if "{{secret:" in url:
                raise RuntimeError("the url placeholder was not resolved (run it from a job, or "
                                   "pass {{secret:name}} through rook_call)")
            headers = {str(k): str(v) for k, v in (cam.get("headers") or {}).items()}
            return await self.http(url, headers, FETCH_TIMEOUT)
        wid = find_worker(self.node, cam.get("worker"))
        if wid is None:
            raise RuntimeError(f"worker {cam.get('worker')!r} is not on the band now")
        if kind == "camera":
            got = await band_call(self.node, "camera.snap", {"camera": cam["camera"]}, wid,
                                  sensitive=True, why=f"camera review: a frame from {cam['name']}")
            data = got.get("data")
        else:
            got = await band_call(self.node, "shell.exec",
                                  {"argv": ["sh", "-c", RTSP_SH, "rook-camera", str(cam["rtsp"])],
                                   "timeout": 30}, wid, timeout=40, sensitive=True,
                                  why=f"camera review: a frame from {cam['name']} (ffmpeg)")
            data = got.get("stdout")
            if not data:
                raise RuntimeError(clip(got.get("stderr") or "ffmpeg returned no frame", 200))
        if not data:
            raise RuntimeError("no frame came back")
        try:
            return base64.b64decode(str(data).strip(), validate=False)
        except ValueError:
            raise RuntimeError("the frame was not base64") from None


def thumb(jpeg: bytes) -> tuple[bytes, bytes]:
    """``(64x36 greyscale bytes, a smaller JPEG to keep)``."""
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
    except Exception as e:  # noqa: BLE001 - Pillow raises many kinds
        raise RuntimeError(f"not an image ({type(e).__name__})") from None
    img = img.convert("RGB")
    small = img.convert("L").resize(THUMB)
    keep = img.copy()
    keep.thumbnail((KEEP_DIM, KEEP_DIM))
    buf = io.BytesIO()
    keep.save(buf, "JPEG", quality=80)
    return small.tobytes(), buf.getvalue()


def difference(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def parse_verdict(text: str) -> dict:
    """The model's ``{level, what}``; anything unreadable is "nothing"."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    level = str(d.get("level") or "").lower()
    if level not in LEVELS:
        level = "notable" if d.get("notable") is True else "nothing"
    return {"level": level, "what": clip(d.get("what") or "", 200)}


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def messages(name: str, before: bytes, now: bytes, extra: str = "") -> list[dict]:
    ask = f"Camera: {name}. First image: before. Second image: now."
    if extra:
        ask += " The person also says: " + clip(extra, 300)
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": data_url(before)}},
                {"type": "image_url", "image_url": {"url": data_url(now)}}]}]
