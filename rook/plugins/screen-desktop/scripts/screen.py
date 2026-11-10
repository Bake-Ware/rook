"""screen.capture on the worker it is called on."""

import base64
import secrets

WINDOWS = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
           "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
           "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
           "$g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
           "$m = New-Object System.IO.MemoryStream; $bmp.Save($m, [System.Drawing.Imaging.ImageFormat]::Jpeg); "
           "[Convert]::ToBase64String($m.ToArray())")


def _tools(path, q, region):
    """The Linux capture tools, in order; a missing or failing one hands over to the next."""
    x, y, w, h = region or (None,) * 4
    return [
        ["grim", "-t", "jpeg", "-q", q, *(["-g", f"{x},{y} {w}x{h}"] if region else []), path],
        ["scrot", "-q", q, "-o", *(["-a", f"{x},{y},{w},{h}"] if region else []), path],
        ["spectacle", "-b", "-n", "-o", path],
        ["gnome-screenshot", "-f", path],
        ["import", "-window", "root", "-quality", q, path],
    ]


async def capture(ctx, quality=85, x=None, y=None, w=None, h=None):
    wid = ctx.worker["id"]
    if ctx.worker["os"] == "windows":
        out = await _exec(ctx, ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS])
        return _jpeg(ctx, base64.b64decode(out["stdout"]))
    region = (x, y, w, h) if None not in (x, y, w, h) else None
    path = f"/tmp/rook-shot-{secrets.token_hex(8)}.jpg"
    missing, failed = [], None
    try:
        for argv in _tools(path, str(quality), region):
            try:
                await _exec(ctx, argv)
            except ctx.Error as e:
                if e.message == f"not found: {argv[0]}":
                    missing.append(argv[0])
                else:
                    failed = e
                continue
            got = await ctx.call_on(wid, "file.read", path=path, encoding="base64")
            return _jpeg(ctx, base64.b64decode(got["content"]))
    finally:
        await ctx.call_on(wid, "shell.exec", argv=["rm", "-f", path])
    raise failed or ctx.Error(f"not installed on this worker: {', '.join(missing)}")


async def _exec(ctx, argv):
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=argv, timeout=25)
    if out["code"] != 0:
        tail = out["stderr"].strip()[-2000:]
        raise ctx.Error(f"{argv[0]} exited {out['code']}" + (f": {tail}" if tail else ""))
    return out


def _jpeg(ctx, data):
    return ctx.file(data=data, name="s.jpg", mime="image/jpeg")
