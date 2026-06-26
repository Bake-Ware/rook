"""hid.* — rootless HID via AccessibilityService (Chaquopy bridge).

Replaces the stock hid plugin (xdotool/ydotool/Win32/Termux-input) with gesture
+ text dispatch through ``systems.bake.rook.HidAccessibilityService``. Every call
returns the usual {"ok": bool, ...}; if the accessibility service isn't enabled
yet, ok is False with a hint.
"""

from __future__ import annotations

from typing import Any

from rook.worker.plugin import Plugin, capability

try:
    from java import jclass  # Chaquopy
    _Hid = jclass("systems.bake.rook.HidAccessibilityService")
except Exception:  # pragma: no cover
    _Hid = None


def _guard() -> dict[str, Any] | None:
    if _Hid is None:
        return {"ok": False, "error": "hid bridge unavailable (not a Chaquopy host)"}
    if not _Hid.isEnabled():
        return {"ok": False, "error": "accessibility service not enabled (grant it in the app)"}
    return None


class AndroidHidPlugin(Plugin):
    NAMESPACE = "hid"

    @capability("backend")
    def _backend(self) -> dict:
        enabled = bool(_Hid and _Hid.isEnabled())
        return {"ok": True, "backend": "android-accessibility", "ready": enabled}

    @capability("type")
    def _type(self, text: str) -> dict:
        if (err := _guard()):
            return err
        ok = bool(_Hid.typeText(str(text)))
        return {"ok": ok} if ok else {"ok": False, "error": "no focused editable field"}

    @capability("mouse.click")
    def _click(self, x: int, y: int) -> dict:
        if (err := _guard()):
            return err
        return {"ok": bool(_Hid.tap(int(x), int(y)))}

    @capability("mouse.drag")
    def _drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> dict:
        if (err := _guard()):
            return err
        ms = max(1, int(float(duration) * 1000))
        return {"ok": bool(_Hid.swipe(int(x1), int(y1), int(x2), int(y2), ms))}

    @capability("key_combo")
    def _key_combo(self, keys: str) -> dict:
        """Map a few common combos to global accessibility actions."""
        if (err := _guard()):
            return err
        action = {"back": _Hid.back, "home": _Hid.home, "recents": _Hid.recents}.get(
            str(keys).strip().lower()
        )
        if action is None:
            return {"ok": False, "error": f"unsupported key_combo on android: {keys!r} "
                                          "(try back|home|recents)"}
        return {"ok": bool(action())}


PLUGIN = AndroidHidPlugin
