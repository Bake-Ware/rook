"""camera on the worker it is called on: the OS's own tools run there through
shell.exec; a photo is written to the worker's temp folder, read back with
file.read and removed."""

import base64
import json
import re
import secrets

TERMUX_DEFAULT = [{"camera": 0, "name": "back", "facing": "back"},
                  {"camera": 1, "name": "front", "facing": "front"}]
# /dev/videoN and the driver's name for it, one "N<TAB>name" line each.
LINUX_LIST = ('for d in /dev/video*; do n=${d#/dev/video}; case $n in ""|*[!0-9]*) continue;; esac; '
              'name=$(cat /sys/class/video4linux/video$n/name 2>/dev/null || echo "$d"); printf "%s\\t%s\\n" "$n" "$name"; done')


async def list_(ctx):
    os_name = ctx.worker["os"]
    if os_name == "termux":
        return await _termux(ctx)
    if os_name == "windows":
        return await _dshow(ctx)
    out = await _exec(ctx, ["sh", "-c", LINUX_LIST])
    rows = [line.split("\t", 1) for line in out["stdout"].splitlines() if "\t" in line]
    return sorted(({"camera": int(n), "name": name.strip()} for n, name in rows), key=lambda c: c["camera"])


async def capture(ctx, camera=0, device="Integrated Camera", resolution="1280x720", quality=85):
    os_name, wid = ctx.worker["os"], ctx.worker["id"]
    windows = os_name == "windows"
    folder = await ctx.call_on(wid, "shell.env", name="TEMP" if windows else "TMPDIR", default="/tmp")
    sep = "\\" if windows else "/"
    path = f"{folder}{sep}rook-cam-{secrets.token_hex(8)}.jpg"
    ffmpeg = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f"]
    if windows:
        tries = [[*ffmpeg, "dshow", "-video_size", resolution, "-i", f"video={device}", "-frames:v", "1",
                  "-q:v", "3", path]]
    elif os_name == "termux":
        tries = [["termux-camera-photo", "-c", str(camera), path]]
    else:
        tries = [["fswebcam", "-d", f"/dev/video{camera}", "--no-banner", "-q", "-r", resolution,
                  "--jpeg", str(quality), path],
                 [*ffmpeg, "v4l2", "-video_size", resolution, "-i", f"/dev/video{camera}", "-frames:v", "1",
                  "-q:v", "3", path]]
    missing, failed = [], None
    try:
        for argv in tries:
            try:
                await _exec(ctx, argv)
            except ctx.Error as e:
                if e.message == f"not found: {argv[0]}":
                    missing.append(argv[0])
                else:
                    failed = e
                continue
            got = await ctx.call_on(wid, "file.read", path=path, encoding="base64")
            return ctx.file(data=base64.b64decode(got["content"]), name="c.jpg", mime="image/jpeg")
    finally:
        rm = ["cmd", "/c", "del", "/q", path] if windows else ["rm", "-f", path]
        await ctx.call_on(wid, "shell.exec", argv=rm)
    raise failed or ctx.Error(f"not installed on this worker: {', '.join(missing)}")


async def _exec(ctx, argv, ok=(0,)):
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=argv, timeout=25)
    if out["code"] not in ok:
        tail = out["stderr"].strip()[-2000:]
        raise ctx.Error(f"{argv[0]} exited {out['code']}" + (f": {tail}" if tail else ""))
    return out


async def _termux(ctx):
    try:
        out = await _exec(ctx, ["termux-camera-info"])
        cams = json.loads(out["stdout"])
    except (ctx.Error, ValueError):
        return TERMUX_DEFAULT
    if not isinstance(cams, list):
        return TERMUX_DEFAULT
    return [{"camera": int(c.get("id", i)), "name": str(c.get("facing") or c.get("id", i)),
             **({"facing": c["facing"]} if c.get("facing") else {})}
            for i, c in enumerate(cams) if isinstance(c, dict)]


async def _dshow(ctx):
    # ffmpeg prints the device list on stderr and exits 1 (the dummy input).
    try:
        out = await _exec(ctx, ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                          ok=(0, 1))
    except ctx.Error as e:
        if e.message == "not found: ffmpeg":
            raise ctx.Error("ffmpeg is needed to list cameras on Windows") from None
        raise
    err = out["stderr"]
    names = re.findall(r'"([^"]+)"\s*\(video\)', err)
    if not names:   # older ffmpeg: a "DirectShow video devices" header, then one name per line
        part = err.split("DirectShow audio devices")[0]
        names = [n for n in re.findall(r'\]\s+"([^"]+)"', part) if not n.startswith("@device")]
    return [{"camera": i, "name": n} for i, n in enumerate(dict.fromkeys(names))]
