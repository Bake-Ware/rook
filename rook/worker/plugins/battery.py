"""battery.* — power state for battery-backed workers (phones, laptops).

Reports charge level + charging status. Gated on :meth:`available` so only a
host that actually has a battery announces the cap; a desktop/server never
does. When present, a compact ``{percent, charging}`` also rides the worker's
heartbeat (see :meth:`heartbeat`), so the whole band sees live battery on the
~30s announce without anyone polling.

Backends, no extra deps:
  * Linux            : ``/sys/class/power_supply/BAT*`` (capacity + status)
  * Android (Termux) : ``termux-battery-status`` (JSON)
  * Windows          : ``kernel32.GetSystemPowerStatus`` via ctypes
  * macOS            : ``pmset -g batt``
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from ..plugin import Plugin, capability


def _is_termux() -> bool:
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux")


# ---- Linux sysfs -----------------------------------------------------------

def _linux_supplies() -> list[str]:
    # Prefer BAT*, but some devices name the battery "battery" (Android/CrOS).
    ps = sorted(glob.glob("/sys/class/power_supply/*"))
    bats = []
    for p in ps:
        t = _read(os.path.join(p, "type")) or ""
        name = os.path.basename(p)
        if t.strip().lower() == "battery" or re.match(r"(?i)bat", name):
            bats.append(p)
    return bats


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _linux_status() -> dict | None:
    bats = _linux_supplies()
    if not bats:
        return None
    # Aggregate across packs (rare multi-battery laptops): mean percent, and
    # "charging" if any pack is charging.
    pcts, statuses = [], []
    for b in bats:
        cap = _read(os.path.join(b, "capacity"))
        st = (_read(os.path.join(b, "status")) or "").lower()
        if cap is not None and cap.isdigit():
            pcts.append(int(cap))
        if st:
            statuses.append(st)
    if not pcts:
        return None
    charging = any(s == "charging" for s in statuses)
    full = all(s == "full" for s in statuses) if statuses else False
    plugged = charging or full or any(s in ("full", "not charging") for s in statuses)
    return {
        "percent": round(sum(pcts) / len(pcts)),
        "charging": charging,
        "plugged": plugged,
        "status": statuses[0] if statuses else ("full" if full else "unknown"),
        "packs": len(pcts),
    }


# ---- Termux / Android ------------------------------------------------------

async def _termux_status() -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "termux-battery-status",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except Exception:
        return None
    try:
        d = json.loads(out.decode() or "{}")
    except Exception:
        return None
    pct = d.get("percentage")
    st = str(d.get("status", "")).lower()   # CHARGING / DISCHARGING / FULL / NOT_CHARGING
    if pct is None:
        return None
    charging = st == "charging"
    return {
        "percent": round(float(pct)),
        "charging": charging,
        "plugged": st in ("charging", "full", "not_charging"),
        "status": st or "unknown",
        "temp_c": (d.get("temperature") if isinstance(d.get("temperature"), (int, float)) else None),
        "health": d.get("health"),
    }


def _termux_status_sync() -> dict | None:
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True,
                             timeout=10).stdout
        d = json.loads(out.decode() or "{}")
    except Exception:
        return None
    pct = d.get("percentage")
    if pct is None:
        return None
    st = str(d.get("status", "")).lower()
    return {"percent": round(float(pct)), "charging": st == "charging",
            "plugged": st in ("charging", "full", "not_charging"),
            "status": st or "unknown"}


# ---- Windows ---------------------------------------------------------------

def _windows_status() -> dict | None:
    if sys.platform != "win32":
        return None
    import ctypes

    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]

    sps = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return None
    flag = sps.BatteryFlag & 0xFF
    pct = sps.BatteryLifePercent & 0xFF
    # BatteryFlag 128 = "no system battery"; 255 = unknown.
    if flag == 128 or pct == 255:
        return None
    ac = sps.ACLineStatus & 0xFF          # 0 offline, 1 online, 255 unknown
    charging = bool(flag & 8)             # bit 3 = charging
    return {
        "percent": int(pct),
        "charging": charging,
        "plugged": ac == 1,
        "status": "charging" if charging else ("full" if pct >= 99 and ac == 1
                                               else ("discharging" if ac == 0 else "unknown")),
    }


# ---- macOS -----------------------------------------------------------------

def _macos_status() -> dict | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             timeout=10).stdout.decode()
    except Exception:
        return None
    m = re.search(r"(\d+)%", out)
    if not m:
        return None
    low = out.lower()
    charging = "charging" in low and "discharging" not in low
    plugged = "ac power" in low or charging
    st = "charging" if charging else ("charged" if "charged" in low
                                      else ("discharging" if "discharging" in low else "unknown"))
    return {"percent": int(m.group(1)), "charging": charging,
            "plugged": plugged, "status": st}


def _read_status_sync() -> dict | None:
    """Best cheap synchronous read for this host (used by heartbeat + gate)."""
    if _is_termux():
        return _termux_status_sync()
    if sys.platform.startswith("linux"):
        return _linux_status()
    if sys.platform == "win32":
        return _windows_status()
    if sys.platform == "darwin":
        return _macos_status()
    return None


class BatteryPlugin(Plugin):
    NAMESPACE = "battery"

    def available(self) -> bool:
        try:
            return _read_status_sync() is not None
        except Exception:
            return False

    @capability("status")
    async def _status(self) -> dict:
        """Current battery: ``{percent, charging, plugged, status, ...}``.

        ``percent`` 0-100, ``charging`` True while charging, ``plugged`` True
        on external power (charging or full). Extra fields when the platform
        exposes them (Termux: temp_c, health)."""
        if _is_termux():
            d = await _termux_status()
        else:
            d = _read_status_sync()
        if not d:
            return {"ok": False, "error": "battery unreadable on this host"}
        d["ok"] = True
        d["ts"] = time.time()
        return d

    def heartbeat(self) -> dict | None:
        # Rides the ~30s announce. Keep it tiny — just the two things a fleet
        # view cares about. termux-battery-status is a subprocess (~tens of ms)
        # but only fires once per announce, which is acceptable.
        d = _read_status_sync()
        if not d:
            return None
        return {"percent": d.get("percent"), "charging": bool(d.get("charging"))}


PLUGIN = BatteryPlugin
