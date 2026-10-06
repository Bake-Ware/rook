"""ui.* — read what's on screen via the AccessibilityService (already granted).

ui.text returns all visible text on the current screen. Pairs with hid.* (tap /
type / back / home) so an agent can read the screen, then act on it.
"""

from __future__ import annotations

from rook.worker.plugin import Plugin, capability

try:
    from java import jclass
    _Hid = jclass("systems.bake.rook.HidAccessibilityService")
except Exception:  # pragma: no cover
    _Hid = None


class AndroidUiPlugin(Plugin):
    NAMESPACE = "ui"

    @capability("text")
    def _text(self) -> dict:
        """All visible text on the current screen (top-to-bottom)."""
        if _Hid is None:
            return {"ok": False, "error": "not an Android host"}
        if not _Hid.isEnabled():
            return {"ok": False, "error": "accessibility service not enabled (grant it in the app)"}
        try:
            txt = _Hid.screenText()
            s = str(txt) if txt is not None else ""
            out = {"ok": True, "text": s, "lines": s.count("\n") + (1 if s else 0)}
            try:
                app = _Hid.screenApp()
                if app is not None and len(app) == 2:
                    out["package"], out["app"] = str(app[0]), str(app[1])
            except Exception:
                pass    # older service build: text only
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidUiPlugin
