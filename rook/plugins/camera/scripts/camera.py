"""camera.list: what each OS calls its cameras. Processes start through the
worker's launch path (spec 7.1)."""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys

from rook.core2.worker.security import launch

TERMUX_DEFAULT = [{"camera": 0, "name": "back", "facing": "back"},
                  {"camera": 1, "name": "front", "facing": "front"}]


async def list_(ctx):
    if os.environ.get("TERMUX_VERSION") or os.path.isdir("/data/data/com.termux"):
        return await _termux(ctx)
    if sys.platform == "win32":
        return await _dshow(ctx)
    out = []
    for dev in sorted(glob.glob("/dev/video*"), key=lambda d: int(re.sub(r"\D", "", d) or 0)):
        n = re.fullmatch(r"/dev/video(\d+)", dev)
        if not n:
            continue
        try:
            with open(f"/sys/class/video4linux/video{n[1]}/name", encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            name = dev
        out.append({"camera": int(n[1]), "name": name})
    return out


async def _run(ctx, argv, timeout=15) -> tuple[int, str, str]:
    proc = await launch(argv, getattr(ctx, "lease", None), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _termux(ctx):
    if not shutil.which("termux-camera-info"):
        return TERMUX_DEFAULT
    code, out, _ = await _run(ctx, ["termux-camera-info"])
    try:
        cams = json.loads(out) if code == 0 else None
    except ValueError:
        cams = None
    if not isinstance(cams, list):
        return TERMUX_DEFAULT
    return [{"camera": int(c.get("id", i)), "name": str(c.get("facing") or c.get("id", i)),
             **({"facing": c["facing"]} if c.get("facing") else {})}
            for i, c in enumerate(cams) if isinstance(c, dict)]


async def _dshow(ctx):
    if not shutil.which("ffmpeg"):
        raise ctx.Error("ffmpeg is needed to list cameras on Windows")
    # ffmpeg prints the device list on stderr and exits 1 (the dummy input).
    _, _, err = await _run(ctx, ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    names = re.findall(r'"([^"]+)"\s*\(video\)', err)
    if not names:   # older ffmpeg: a "DirectShow video devices" header, then one name per line
        part = err.split("DirectShow audio devices")[0]
        names = [n for n in re.findall(r'\]\s+"([^"]+)"', part) if not n.startswith("@device")]
    return [{"camera": i, "name": n} for i, n in enumerate(dict.fromkeys(names))]
