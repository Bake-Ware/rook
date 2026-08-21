"""location.* — current GPS/network location (Chaquopy java bridge).

Uses the last known fix from the fused/gps/network providers (fast, no callback
plumbing). If nothing is cached yet, asks Android for a single current fix and
waits briefly. Needs ACCESS_FINE_LOCATION (or COARSE).
"""

from __future__ import annotations

import threading

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, cast, has_permission

_FINE = "android.permission.ACCESS_FINE_LOCATION"
_COARSE = "android.permission.ACCESS_COARSE_LOCATION"


def _loc_to_dict(loc, provider: str) -> dict:
    import time as _t
    age = None
    try:
        age = round(max(0.0, (_t.time() - loc.getTime() / 1000.0)), 1)
    except Exception:
        pass
    d = {"lat": loc.getLatitude(), "lon": loc.getLongitude(),
         "provider": provider or loc.getProvider(), "age_s": age}
    try:
        if loc.hasAccuracy():
            d["accuracy_m"] = round(loc.getAccuracy(), 1)
    except Exception:
        pass
    try:
        if loc.hasAltitude():
            d["altitude_m"] = round(loc.getAltitude(), 1)
    except Exception:
        pass
    try:
        if loc.hasSpeed():
            d["speed_mps"] = round(loc.getSpeed(), 2)
    except Exception:
        pass
    return d


class AndroidLocationPlugin(Plugin):
    NAMESPACE = "location"

    @capability("get")
    def _get(self, timeout: float = 8.0) -> dict:
        """Current location: {lat, lon, accuracy_m, provider, age_s}."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not (has_permission(_FINE) or has_permission(_COARSE)):
            return {"ok": False, "error": "location permission not granted (grant it in the app)"}
        try:
            Context = jclass("android.content.Context")
            lm = cast(jclass("android.location.LocationManager"),
                      ctx.getSystemService(Context.LOCATION_SERVICE))
            # Best cached fix across providers.
            best, best_age = None, None
            import time as _t
            for prov in ("gps", "fused", "network", "passive"):
                try:
                    loc = lm.getLastKnownLocation(prov)
                except Exception:
                    loc = None
                if loc is None:
                    continue
                age = _t.time() - loc.getTime() / 1000.0
                if best is None or age < best_age:
                    best, best_age, best_prov = loc, age, prov
            if best is not None and best_age is not None and best_age < 120:
                return {"ok": True, **_loc_to_dict(best, best_prov)}
            # Nothing fresh cached — ask for a single current fix (API 30+).
            fresh = self._current_fix(ctx, lm, timeout)
            if fresh is not None:
                return {"ok": True, **_loc_to_dict(fresh, fresh.getProvider())}
            if best is not None:
                return {"ok": True, "stale": True, **_loc_to_dict(best, best_prov)}
            return {"ok": False, "error": "no location fix available (open a maps app to warm GPS)"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _current_fix(self, ctx, lm, timeout: float):
        """One-shot getCurrentLocation (API 30+); returns a Location or None."""
        try:
            Build = jclass("android.os.Build$VERSION")
            if Build.SDK_INT < 30:
                return None
            Executors = jclass("java.util.concurrent.Executors")
            executor = Executors.newSingleThreadExecutor()
            got = {}
            ev = threading.Event()
            from java import dynamic_proxy
            Consumer = jclass("java.util.function.Consumer")

            class _C(dynamic_proxy(Consumer)):
                def accept(self, value):
                    got["loc"] = value
                    ev.set()

            prov = "fused"
            try:
                if not lm.isProviderEnabled(prov):
                    prov = "gps"
            except Exception:
                prov = "gps"
            lm.getCurrentLocation(prov, None, executor, _C())
            ev.wait(max(1.0, float(timeout)))
            return got.get("loc")
        except Exception:
            return None


PLUGIN = AndroidLocationPlugin
