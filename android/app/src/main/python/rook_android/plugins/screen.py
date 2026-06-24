"""screenshot.* — native screen capture via MediaProjection (Chaquopy bridge).

Replaces the stock screenshot plugin's Termux camera hack with a real frame
grab. Calls into ``systems.bake.rook.ScreenCaptureBridge.captureJpeg(quality)``,
which returns a JPEG byte[] or null when the user hasn't granted capture consent
yet (the grant flow lives in MainActivity).
"""

from __future__ import annotations

import base64
from typing import Any

from rook.worker.plugin import Plugin, capability

try:
    from java import jclass  # provided by Chaquopy at runtime
    _Bridge = jclass("systems.bake.rook.ScreenCaptureBridge")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    _Bridge = None


def _capture(quality: int) -> dict[str, Any]:
    if _Bridge is None:
        return {"ok": False, "error": "screen bridge unavailable (not a Chaquopy host)"}
    try:
        data = _Bridge.captureJpeg(int(quality))
    except Exception as e:  # Java exceptions surface as Python exceptions
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if data is None:
        return {"ok": False, "error": "no screen-capture consent yet (grant it in the app)"}
    return {"ok": True, "format": "jpeg",
            "data": base64.b64encode(bytes(data)).decode("ascii")}


class AndroidScreenPlugin(Plugin):
    NAMESPACE = "screenshot"

    @capability("capture")
    def _capture(self, quality: int = 85) -> dict:
        """Capture the full screen as a JPEG via MediaProjection."""
        return _capture(int(quality))

    @capability("capture_preview")
    def _preview(self) -> dict:
        """Low-quality grab for quick visual checks."""
        return _capture(40)


PLUGIN = AndroidScreenPlugin
