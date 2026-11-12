"""battery.status for the worker it is called on: the OS's own tool runs
there (shell.exec), its output is read here."""

import json
import re

from rook.kit.ctx import KitError

UPOWER = ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]
# No upower: the first battery in sysfs, printed like upower does.
SYSFS = ["sh", "-c", 'for b in /sys/class/power_supply/BAT*; do [ -r "$b/capacity" ] || continue; '
         'printf "state: %s\\npercentage: %s%%\\n" "$(tr "A-Z " "a-z-" < "$b/status")" "$(cat "$b/capacity")"; '
         'exit 0; done; exit 3']
WINDOWS = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "$b = Get-CimInstance Win32_Battery | Select-Object -First 1; if (-not $b) { exit 3 }; "
           "$s = @{1='discharging';2='plugged';3='fully-charged';6='charging';7='charging';8='charging';"
           "9='charging'}[[int]$b.BatteryStatus]; 'state: ' + $s; "
           "'percentage: ' + $b.EstimatedChargeRemaining + '%'"]
TERMUX = ["termux-battery-status"]
LINES = re.compile(r"state:\s+(?P<status>[\w-]+)[\s\S]*?percentage:\s+(?P<percent>[\d.]+)%")


async def status(ctx):
    os_name = ctx.worker["os"]
    if os_name == "termux":
        data = json.loads(await _run(ctx, TERMUX))
        return {"percent": float(data["percentage"]), "status": data["status"]}
    if os_name == "windows":
        text = await _run(ctx, WINDOWS)
    else:
        try:
            text = await _run(ctx, UPOWER)
        except KitError:                # no upower, or no upowerd running
            text = await _run(ctx, SYSFS)
    if "battery-missing" in text:       # upower's DisplayDevice when there is no battery
        raise ctx.Error("no battery: upower reports battery-missing")
    m = LINES.search(text)
    if m is None:
        raise ctx.Error(f"unreadable battery report: {text.strip()[:200]}")
    return {"percent": float(m["percent"]), "status": m["status"]}


async def _run(ctx, argv):
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=argv, env={"LANG": "C"}, timeout=8)
    if out["code"] != 0:
        raise ctx.Error((out.get("stderr") or "").strip()[:300] or f"{argv[0]} exited {out['code']}")
    return out["stdout"]
