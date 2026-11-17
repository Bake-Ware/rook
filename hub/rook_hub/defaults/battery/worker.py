"""battery: this worker's battery charge and state, shipped as a plugin installed by default
(PL7, H4). docs/inventory.md CAP-07. battery.status is 0.5's (origin/castling
rook/plugins/battery/plugin.yaml), by Bake's answer of 17 November: no arguments, and
{percent, status}, status in the system's own word (charging, discharging, fully-charged, ...).

On Linux, as 0.5 does: upower's display device, and where upower is not installed or reports
no battery, the first battery in /sys/class/power_supply, its state written as upower writes it.
A machine with neither answers that it has no battery. Only Linux is built: on another system the
cap answers that it is not built there yet (W5). Nothing waits on a bound: upower runs until it
answers."""

import asyncio
import glob
import os
import re
import sys

from rook_worker import Cap

UPOWER = ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]
STATE = re.compile(r"state:\s+(?P<status>[\w-]+)[\s\S]*?percentage:\s+(?P<percent>[\d.]+)%")


async def status(args):
    extra = sorted(args)
    if extra:
        raise ValueError(f"battery.status takes no argument {', '.join(extra)}; it takes none")
    if not sys.platform.startswith("linux"):
        raise ValueError(f"battery.status is not built for {sys.platform} workers yet; call it on a Linux worker")
    found = await _upower()
    if found is None:
        found = await asyncio.to_thread(_sysfs)
    if found is None:
        raise ValueError("this worker has no battery: upower reports none and /sys/class/power_supply has no "
                         "BAT*; call battery.status on a worker with a battery, such as a laptop")
    return found


async def _upower():
    """upower's display device, or None when upower is not installed or reports no battery."""
    try:
        proc = await asyncio.create_subprocess_exec(*UPOWER, env={**os.environ, "LANG": "C"},
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    out = (await proc.communicate())[0].decode("utf-8", errors="replace")
    match = STATE.search(out)
    if proc.returncode != 0 or "battery-missing" in out or match is None:
        return None
    return {"percent": float(match["percent"]), "status": match["status"]}


def _sysfs():
    """The first battery in sysfs, or None."""
    for folder in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        try:
            with open(os.path.join(folder, "capacity")) as f:
                percent = float(f.read().strip())
            with open(os.path.join(folder, "status")) as f:
                state = f.read().strip().lower().replace(" ", "-")
        except (OSError, ValueError):
            continue
        return {"percent": percent, "status": state}
    return None


CAPS = {
    "battery.status": Cap(
        "This worker's battery: percent charged and status (charging, discharging, fully-charged, ...).",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
}
