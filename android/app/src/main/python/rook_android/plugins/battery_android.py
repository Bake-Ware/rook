"""battery.* — native Android battery via BatteryManager (Chaquopy bridge).

The stock ``battery`` plugin reads Linux sysfs (``/sys/class/power_supply``),
which an Android app sandbox can't access, so it never loads here and the phone
reported no battery. This reads the same info through Android's BatteryManager
(percent + charging) and the sticky ``ACTION_BATTERY_CHANGED`` broadcast (status,
plugged, temperature). No extra permission needed. Mirrors the stock plugin's
shape so ``battery.status`` and the {percent,charging} heartbeat work identically.
"""

from __future__ import annotations

import time

from rook.worker.plugin import Plugin, capability

try:
    from java import jclass, cast  # provided by Chaquopy at runtime
    _ActivityThread = jclass("android.app.ActivityThread")
    _Context = jclass("android.content.Context")
    _Intent = jclass("android.content.Intent")
    _IntentFilter = jclass("android.content.IntentFilter")
    _BatteryManager = jclass("android.os.BatteryManager")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    _ActivityThread = None


def _app_context():
    """The app Context, fetched without needing Kotlin to hand it over."""
    if _ActivityThread is None:
        return None
    try:
        return _ActivityThread.currentApplication()
    except Exception:
        return None


_STATUS = {1: "unknown", 2: "charging", 3: "discharging",
           4: "not charging", 5: "full"}


def _read() -> dict | None:
    ctx = _app_context()
    if ctx is None:
        return None
    out: dict = {}

    # Percent + charging straight from the BatteryManager system service — no
    # receiver, no permission (API 21+ / isCharging API 23+).
    try:
        bm = cast("android.os.BatteryManager",
                  ctx.getSystemService(_Context.BATTERY_SERVICE))
        if bm is not None:
            pct = bm.getIntProperty(_BatteryManager.BATTERY_PROPERTY_CAPACITY)
            if pct is not None and 0 <= int(pct) <= 100:
                out["percent"] = int(pct)
            try:
                out["charging"] = bool(bm.isCharging())
            except Exception:
                pass
    except Exception:
        pass

    # Richer detail from the sticky battery broadcast (null receiver = read-only,
    # no RECEIVER_EXPORTED flag needed for this protected system broadcast).
    try:
        flt = _IntentFilter(_Intent.ACTION_BATTERY_CHANGED)
        batt = ctx.registerReceiver(None, flt)
        if batt is not None:
            level = batt.getIntExtra(_BatteryManager.EXTRA_LEVEL, -1)
            scale = batt.getIntExtra(_BatteryManager.EXTRA_SCALE, -1)
            status = batt.getIntExtra(_BatteryManager.EXTRA_STATUS, -1)
            plugged = batt.getIntExtra(_BatteryManager.EXTRA_PLUGGED, -1)
            temp = batt.getIntExtra(_BatteryManager.EXTRA_TEMPERATURE, -1)
            if "percent" not in out and level >= 0 and scale > 0:
                out["percent"] = round(level * 100 / scale)
            out["status"] = _STATUS.get(status, "unknown")
            if "charging" not in out:
                out["charging"] = status == 2  # BATTERY_STATUS_CHARGING
            out["plugged"] = plugged > 0
            if temp and temp > 0:
                out["temp_c"] = round(temp / 10.0, 1)
    except Exception:
        pass

    return out if "percent" in out else None


class AndroidBatteryPlugin(Plugin):
    NAMESPACE = "battery"

    def available(self) -> bool:
        try:
            return _read() is not None
        except Exception:
            return False

    @capability("status")
    def _status(self) -> dict:
        """Current battery: ``{percent, charging, plugged, status, temp_c?}``."""
        d = _read()
        if not d:
            return {"ok": False, "error": "battery unreadable on this host"}
        d["ok"] = True
        d["ts"] = time.time()
        return d

    def heartbeat(self) -> dict | None:
        d = _read()
        if not d:
            return None
        return {"percent": d.get("percent"), "charging": bool(d.get("charging"))}


PLUGIN = AndroidBatteryPlugin
