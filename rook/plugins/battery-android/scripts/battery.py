"""battery.status on the phone: BatteryManager for percent and charging, the
sticky ACTION_BATTERY_CHANGED broadcast for the rest. No permission needed."""

from rook.kit import android
from rook.kit.ctx import KitError

STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}


def status(ctx):
    BM = android.jclass("android.os.BatteryManager")
    out = {}
    bm = android.service("BATTERY_SERVICE", "android.os.BatteryManager")
    if bm is not None:
        pct = bm.getIntProperty(BM.BATTERY_PROPERTY_CAPACITY)
        if pct is not None and 0 <= int(pct) <= 100:
            out["percent"] = int(pct)
        out["charging"] = bool(bm.isCharging())
    Intent = android.jclass("android.content.Intent")
    # A null receiver reads the sticky broadcast once and registers nothing.
    batt = android.context().registerReceiver(None, android.jclass("android.content.IntentFilter")(
        Intent.ACTION_BATTERY_CHANGED))
    if batt is not None:
        level, scale = batt.getIntExtra(BM.EXTRA_LEVEL, -1), batt.getIntExtra(BM.EXTRA_SCALE, -1)
        state = batt.getIntExtra(BM.EXTRA_STATUS, -1)
        temp = batt.getIntExtra(BM.EXTRA_TEMPERATURE, -1)
        if "percent" not in out and level >= 0 and scale > 0:
            out["percent"] = round(level * 100 / scale)
        out["status"] = STATUS.get(state, "unknown")
        out.setdefault("charging", state == 2)
        out["plugged"] = batt.getIntExtra(BM.EXTRA_PLUGGED, -1) > 0
        if temp > 0:
            out["temp_c"] = round(temp / 10.0, 1)
    if "percent" not in out:
        raise KitError("the phone doesn't report its battery")
    return out
