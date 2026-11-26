"""battery: this worker's battery charge and state, shipped as a plugin installed by default
(PL7, H4). docs/inventory.md CAP-07. battery.status is 0.5's (origin/castling
rook/plugins/battery/plugin.yaml), by Bake's answer of 17 November: no arguments, and
{percent, status}, status in the system's own word (charging, discharging, fully-charged, ...).

On Linux, as 0.5 does: upower's display device, and where upower is not installed or reports
no battery, the first battery in /sys/class/power_supply, its state written as upower writes it.
A machine with neither answers that it has no battery.

On the Rook Android app's worker (brief AP2), as 0.5's battery-android does (origin/castling
rook/plugins/battery-android): BatteryManager for percent and charging, the sticky
ACTION_BATTERY_CHANGED broadcast for the rest, so the reply also has charging, plugged and temp_c
(when the phone reports it), and status is Android's word (charging, discharging, not charging, full,
unknown). An app cannot read /sys/class/power_supply there. The app is told apart by Chaquopy's
`java` module, which imports nowhere else. On any other system the cap answers that it is not built
there yet (W5). Nothing waits on a bound: upower runs until it answers."""

import asyncio
import glob
import os
import re
import sys

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app (brief AP2)
except ImportError:
    jclass = None

UPOWER = ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]
STATE = re.compile(r"state:\s+(?P<status>[\w-]+)[\s\S]*?percentage:\s+(?P<percent>[\d.]+)%")


async def status(args):
    extra = sorted(args)
    if extra:
        raise ValueError(f"battery.status takes no argument {', '.join(extra)}; it takes none")
    if jclass is not None:
        return await asyncio.to_thread(_android)
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


ANDROID_STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}   # BATTERY_STATUS_*


def _android():
    """The phone's battery: percent, charging, status, plugged, and temp_c when it reports one."""
    context = jclass("systems.bake.rook.core.worker.Bridge").context()
    BatteryManager, Intent = jclass("android.os.BatteryManager"), jclass("android.content.Intent")
    out = {}
    manager = cast(BatteryManager, context.getSystemService(jclass("android.content.Context").BATTERY_SERVICE))
    if manager is not None:
        percent = int(manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
        if 0 <= percent <= 100:
            out["percent"] = percent
        out["charging"] = bool(manager.isCharging())
    # A null receiver reads the sticky broadcast once and registers nothing.
    sticky = context.registerReceiver(None, jclass("android.content.IntentFilter")(Intent.ACTION_BATTERY_CHANGED))
    if sticky is not None:
        level, scale = sticky.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), sticky.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        state = sticky.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        temperature = sticky.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1)
        if "percent" not in out and level >= 0 and scale > 0:
            out["percent"] = round(level * 100 / scale)
        out["status"] = ANDROID_STATUS.get(state, "unknown")
        out.setdefault("charging", state == 2)
        out["plugged"] = sticky.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1) > 0
        if temperature > 0:
            out["temp_c"] = round(temperature / 10.0, 1)
    if "percent" not in out:
        raise ValueError("this phone does not report its battery")
    return out


CAPS = {
    "battery.status": Cap(
        "This worker's battery: percent charged and status (charging, discharging, fully-charged, ...); on a phone also charging, plugged and temp_c.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        status),
}
