"""notification.post on the worker it is called on: notify-send, or a tray
balloon through PowerShell on Windows."""

BALLOON = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $n = New-Object System.Windows.Forms.NotifyIcon; "
           "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; $n.BalloonTipTitle = $env:ROOK_TITLE; "
           "$n.BalloonTipText = $env:ROOK_TEXT; $n.ShowBalloonTip(5000); Start-Sleep -Seconds 4; $n.Dispose()")


async def post(ctx, text, title="Rook"):
    if ctx.worker["os"] == "windows":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", BALLOON]
    else:
        argv = ["notify-send", "-a", "rook", "--", title, text]
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=argv,
                            env={"ROOK_TITLE": title, "ROOK_TEXT": text})
    if out["code"] != 0:
        tail = out["stderr"].strip()[-2000:]
        raise ctx.Error(f"{argv[0]} exited {out['code']}" + (f": {tail}" if tail else ""))
    return out["stdout"].rstrip("\r\n")
