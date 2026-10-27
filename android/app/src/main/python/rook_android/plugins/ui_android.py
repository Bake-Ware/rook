"""ui.* — read what's on screen via the AccessibilityService (already granted).

ui.text returns all visible text on the current screen. Pairs with hid.* (tap /
type / back / home) so an agent can read the screen, then act on it.

ui.nodes / ui.click / ui.scroll / ui.back work from labels rather than pixels,
so they also drive apps that block screenshots (FLAG_SECURE).

ui.screen (a compact summary), ui.wait (until something shows, goes, or the
screen settles) and ui.type (into a field by its label) cut the read-act-read
round trips; screen=true on an action returns the screen after it settles.
They read the active window plus any popup or dialog window above it.
"""

from __future__ import annotations

import asyncio
import json

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place

from rook_android import screenwait as _sw

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


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        x = default
    return max(lo, min(hi, x))


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
    async def _click(self, text: str = "", desc: str = "", id: str = "", contains: bool = False,
                     index: int = 0, long: bool = False, wait: float = 0,
                     screen: bool = False) -> dict:
        """Click the node whose text, desc (content description) or id (view id, with or
        without the package: prefix) matches; all given fields must match. text also
        matches the description of a node with no text. Exact, case-insensitive match
        unless contains=true; index picks among several matches (0 = first, top-down).
        Clicks the nearest clickable ancestor, else taps the node's center. long=true
        long-presses. Not found -> ok false with near matches.
        wait=<seconds> (up to 8) first waits for the node to appear (an app still
        opening, a dropdown still dropping). screen=true returns the screen after the
        click settles (see ui.screen) as screen."""
        if (err := _guard()):
            return err
        try:
            t, d, i, c = _opt(text), _opt(desc), _opt(id), _flag(contains)
            want = int(index)
            secs = _num(wait, 0, 0, 8)
            if secs > 0 and (t or d or i):
                async def present():
                    n = await asyncio.to_thread(_Hid.matchCount, t, d, i, c)
                    return n < 0 or n > want
                await _sw.until(present, secs)
            before = await _sw.rev(_Hid) if _flag(screen) else None
            out = _bridge(await asyncio.to_thread(_Hid.clickNode, t, d, i, c, want, _flag(long)))
            out = {**out, **_app()}
            if _flag(screen) and out.get("ok"):
                await _sw.with_screen(out, before, _Hid)
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("scroll", risk="exec", scope="local")
    async def _scroll(self, direction: str = "down", text: str = "", id: str = "",
                      contains: bool = False, screen: bool = False) -> dict:
        """Scroll one page down (forward) or up (backward). Scrolls the scrollable node
        holding the text/id match, else the outermost scrollable node on screen (an
        open dropdown's list first). scrolled=false means it was already at the end
        (or the view refused). screen=true returns the screen after it settles."""
        if (err := _guard()):
            return err
        d = str(direction).strip().lower()
        if d not in ("down", "up", "forward", "backward"):
            return {"ok": False, "error": f"direction must be down or up, not {direction!r}"}
        try:
            before = await _sw.rev(_Hid) if _flag(screen) else None
            out = _bridge(await asyncio.to_thread(_Hid.scrollNode, d in ("down", "forward"),
                                                  _opt(text), _opt(id), _flag(contains)))
            out = {**out, **_app()}
            if _flag(screen) and out.get("ok"):
                await _sw.with_screen(out, before, _Hid)
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("back", risk="exec", scope="local")
    async def _back(self, screen: bool = False) -> dict:
        """Press Back (the global accessibility action). screen=true returns the
        screen after it settles."""
        if (err := _guard()):
            return err
        before = await _sw.rev(_Hid) if _flag(screen) else None
        out = {"ok": bool(_Hid.back())}
        if _flag(screen) and out["ok"]:
            await _sw.with_screen(out, before, _Hid)
        return out

    @capability("screen", risk="exec", tags=("sensitive",), scope="local")
    async def _screen(self, limit: int = 60, text_limit: int = 40) -> dict:
        """The current screen, compactly: package and app; items to act on
        (label, kind button|field|toggle|list, detail, short id, checked, a
        field's value and hint, center at [x,y] for hid.mouse.click); the other
        text top to bottom; focus (the focused field); locked when the lock
        screen is up; rev, a revision to pass to ui.wait changed_from. Popups
        and dialogs above the app are included. Passwords are never shown."""
        if (err := _guard()):
            return err
        try:
            out = await _sw.summary(_Hid, int(_num(limit, 60, 1, 300)),
                                    int(_num(text_limit, 40, 0, 300)))
            return out if out.get("ok") is False else {"ok": True, **out}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("wait", risk="exec", tags=("sensitive",), scope="local")
    async def _wait(self, text: str = "", desc: str = "", id: str = "", contains: bool = False,
                    gone: bool = False, package: str = "", changed_from: str = "",
                    settle_ms: int = 0, timeout: float = 8, screen: bool = True) -> dict:
        """Wait (up to timeout seconds, max 60) until the screen is how you want it,
        then return it (screen=false leaves the summary out). All given conditions
        must hold: a node matching text/desc/id (ui.click's rules) is visible, or
        with gone=true none is; package is in front (package name or app label);
        the screen's rev differs from changed_from (from ui.screen). settle_ms then
        also waits until nothing changed for that long; with no other condition it
        is 600 ms (wait for the screen to settle). Timed out -> ok false with the
        screen as it is."""
        if (err := _guard()):
            return err
        t, d, i, c = _opt(text), _opt(desc), _opt(id), _flag(contains)
        pkg, since = _opt(package), _opt(changed_from)
        quiet = int(_num(settle_ms, 0, 0, 10000))
        if not (t or d or i or pkg or since) and quiet == 0:
            quiet = 600
        no_node = _flag(gone)
        state = {}

        async def met() -> bool:
            if t or d or i:
                n = await asyncio.to_thread(_Hid.matchCount, t, d, i, c)
                state["matches"] = max(n, 0)
                if (n > 0) == no_node:
                    return False
            if pkg:
                app = _app()
                p = pkg.strip().lower()
                if p not in (app.get("package", "").lower(), app.get("app", "").lower()):
                    return False
            if since and await _sw.rev(_Hid) == since:
                return False
            if quiet:
                q = await _sw.quiet_ms(_Hid)
                if 0 <= q < quiet:
                    return False
            return True

        try:
            ok, waited = await _sw.until(met, _num(timeout, 8, 0, _sw.WAIT_MAX_S))
            out: dict = {"ok": ok, "waited_ms": waited, **state}
            if not ok:
                out["error"] = f"timed out after {waited / 1000:.1f}s"
            if _flag(screen) or not ok:
                shot = await _sw.summary(_Hid)
                shot.pop("ok", None)
                out["screen"] = shot
            else:
                out.update(_app())
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("type", risk="exec", scope="local")
    async def _type(self, text: str, label: str = "", id: str = "", contains: bool = False,
                    index: int = 0, append: bool = False, submit: bool = False,
                    wait: float = 0, screen: bool = False) -> dict:
        """Type text into a field in one call (no click first). label names the
        field: its hint, description, label view, current text, or the caption
        just above it or to its left; id matches its view id. With neither, the
        focused field, else the only field on screen. Replaces the field's text
        unless append=true; submit=true then presses the keyboard's enter/send
        (Android 11+). wait=<seconds> (up to 8) first waits for the field;
        screen=true returns the screen after it settles. Not found -> ok false
        with the fields on screen. The reply never repeats the text."""
        if (err := _guard()):
            return err
        lab, i, c = _opt(label), _opt(id), _flag(contains)
        want = int(index)
        try:
            secs = _num(wait, 0, 0, 8)
            if secs > 0:
                async def present():
                    n = await asyncio.to_thread(_Hid.fieldCount, lab, i, c)
                    return n < 0 or n > want
                await _sw.until(present, secs)
            before = await _sw.rev(_Hid) if _flag(screen) else None
            out = _bridge(await asyncio.to_thread(_Hid.typeInto, lab, i, c, want, str(text),
                                                  _flag(append), _flag(submit)))
            out = {**out, **_app()}
            if _flag(screen) and out.get("ok"):
                await _sw.with_screen(out, before, _Hid)
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidUiPlugin
