"""location.get — bounded native GPS/network request, independent of Maps."""
import json
from rook.worker.plugin import ANDROID_APP, Plugin, capability, place
from rook_android.androidctx import app_context, jclass


class AndroidLocationPlugin(Plugin):
    NAMESPACE = "location"
    PLACEMENT = place(ANDROID_APP)

    @capability("get", risk="exec", tags=("sensitive",), scope="self")
    def _get(self, timeout: float = 8.0) -> dict:
        """Get lat/lon, accuracy, age, stale flag and Maps URL. Timeout: 1–25s.

        Requires device Location enabled and Rook's background-location grant.
        """
        try:
            ctx = app_context()
            if ctx is None:
                return {"ok": False, "error": "not an Android host"}
            return json.loads(str(jclass("systems.bake.rook.LocationBridge").get(ctx, float(timeout))))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidLocationPlugin
