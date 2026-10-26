"""ui.* — read what's on screen via the AccessibilityService (already granted).

ui.text returns all visible text on the current screen. Pairs with hid.* (tap /
type / back / home) so an agent can read the screen, then act on it.

ui.nodes / ui.click / ui.scroll / ui.back work from labels rather than pixels,
so they also drive apps that block screenshots (FLAG_SECURE).
"""

from __future__ import annotations

import json

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place

try:
    from java import jclass
    _Hid = jclass("systems.bake.rook.HidAccessibilityService")
except Exception:  # pragma: no cover
    _Hid = None


def _guard() -> dict | None:
    if _Hid is None:
        return {"ok": False, "error": "not an Android host"}
    if not _Hid.isEnabled():
        return {"ok": False, "error": "accessibility service not enabled (grant it in the app)"}
    return None


def _app() -> dict:
    try:
        app = _Hid.screenApp()
        if app is not None and len(app) == 2:
            return {"package": str(app[0]), "app": str(app[1])}
    except Exception:
        pass
    return {}


def _opt(v) -> str | None:
    """Empty strings mean "not given" (dashboard forms send them)."""
    if v is None:
        return None
    s = str(v)
    return s if s.strip() else None


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _bridge(raw) -> dict:
    if raw is None:
        return {"ok": False, "error": "accessibility service not enabled (grant it in the app)"}
    return json.loads(str(raw))


class AndroidUiPlugin(Plugin):
    NAMESPACE = "ui"
    PLACEMENT = place(ANDROID_APP)

    @capability("text", risk="exec", tags=("sensitive",), scope="local")
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

    @capability("nodes", risk="exec", tags=("sensitive",), scope="local")
    def _nodes(self, all: bool = False, contains: str = "", limit: int = 300) -> dict:
        """Visible nodes on screen, top-to-bottom: text, desc, id, class, clickable,
        scrollable, checked, bounds [l,t,r,b]. By default only nodes with a label or
        an action; all=true lists every visible node. contains filters by text/desc/id
        (case-insensitive)."""
        if (err := _guard()):
            return err
        try:
            nodes = json.loads(str(_Hid.screenNodes(_flag(all), 5000) or "[]"))
            if (needle := _opt(contains)):
                n = needle.lower()
                nodes = [x for x in nodes
                         if any(n in str(x.get(k, "")).lower() for k in ("text", "desc", "id"))]
            total = len(nodes)
            nodes = nodes[:max(1, int(limit))]
            return {"ok": True, **_app(), "count": total, "truncated": total > len(nodes),
                    "nodes": nodes}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("click", risk="exec", scope="local")
    def _click(self, text: str = "", desc: str = "", id: str = "", contains: bool = False,
               index: int = 0, long: bool = False) -> dict:
        """Click the node whose text, desc (content description) or id (view id, with or
        without the package: prefix) matches; all given fields must match. text also
        matches the description of a node with no text. Exact, case-insensitive match
        unless contains=true; index picks among several matches (0 = first, top-down).
        Clicks the nearest clickable ancestor, else taps the node's center. long=true
        long-presses. Not found -> ok false with near matches."""
        if (err := _guard()):
            return err
        try:
            out = _bridge(_Hid.clickNode(_opt(text), _opt(desc), _opt(id), _flag(contains),
                                         int(index), _flag(long)))
            return {**out, **_app()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("scroll", risk="exec", scope="local")
    def _scroll(self, direction: str = "down", text: str = "", id: str = "",
                contains: bool = False) -> dict:
        """Scroll one page down (forward) or up (backward). Scrolls the scrollable node
        holding the text/id match, else the outermost scrollable node on screen.
        scrolled=false means it was already at the end (or the view refused)."""
        if (err := _guard()):
            return err
        d = str(direction).strip().lower()
        if d not in ("down", "up", "forward", "backward"):
            return {"ok": False, "error": f"direction must be down or up, not {direction!r}"}
        try:
            out = _bridge(_Hid.scrollNode(d in ("down", "forward"), _opt(text), _opt(id),
                                          _flag(contains)))
            return {**out, **_app()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("back", risk="exec", scope="local")
    def _back(self) -> dict:
        """Press Back (the global accessibility action)."""
        if (err := _guard()):
            return err
        return {"ok": bool(_Hid.back())}


PLUGIN = AndroidUiPlugin
