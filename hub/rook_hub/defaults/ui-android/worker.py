"""ui-android: the apps on the phone the Rook Android app runs on, read and driven by what they show
(brief AP5), shipped as a plugin installed by default. ui.screen, ui.click, ui.scroll, ui.type and
ui.wait are 0.5's (origin/castling rook/plugins/ui-android): the same names, arguments and replies.
Labels work where pixels do not: apps that block screenshots, screens of any size.

The app's accessibility service (systems.bake.rook.core.worker.Hands) reads the screen's tree and acts
on it; its screen model is 0.5's ScreenModel (Screen.kt in the app). The first call that needs it
while it is off takes the person to its page in Android's settings and waits (Bridge.hands); refused,
the error says where to turn it on. Each call first turns the screen on if it is off (Bridge.awake).

Nothing here bounds a call (Bake, 17 November). ui.screen gives every item and every line of text
unless the caller passes `limit` or `text_limit` (0.5 cut at 60 and 40 by default, capped both at 300,
and cut each label, detail and line); ui.wait waits as long as `timeout` says and, with none, until
it is true (0.5: 8 s by default, 60 at most); `wait` on ui.click and ui.type has no maximum (0.5: 8);
`settle_ms` none (0.5: 10000); text none (0.5: 10000 characters); no manifest timeout. `screen: true`
answers once the change has settled: nothing on screen changed for 500 ms (0.5's own measure), with
no end of ours, so a screen that never stops changing is waited on. Android's own refusals (a
password field's text) are reported as they are.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import json
import time

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

#: ui.wait with no condition but the screen being still: still for this long (0.5's).
STILL_MS = 600


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _text(args, name):
    """A string argument; empty means not given (0.5's)."""
    value = args.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value if value.strip() else None


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _whole(args, name, default, low):
    value = args.get(name, default)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < low):
        raise ValueError(f"{name} must be a whole number, {low} or more")
    return value


def _seconds(args, name, default):
    value = args.get(name, default)
    if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
        raise ValueError(f"{name} must be a number of seconds, 0 or more")
    return value


def _bridge():
    return jclass("systems.bake.rook.core.worker.Bridge")


def _hands(cap):
    """The accessibility service for `cap`, asked for if it is off; the screen turned on if it is off."""
    refused = _bridge().hands(cap)
    if refused is not None:
        raise ValueError(str(refused))
    _bridge().awake(cap)
    return jclass("systems.bake.rook.core.worker.Hands")


def _result(raw):
    """A Hands reply: `ok` dropped; {"ok": false, "error": ...} raises, with what else it said."""
    out = json.loads(str(raw))
    if out.pop("ok", True) is False:
        error = str(out.pop("error", None) or "the phone refused")
        raise ValueError(f"{error} {json.dumps(out, ensure_ascii=False)}" if out else error)
    return out


def screen_now(hands, limit=None, text_limit=None):
    return json.loads(str(hands.screen(-1 if limit is None else limit, -1 if text_limit is None else text_limit)))


def after(hands, rev, since, app=None):
    """The screen once an action's change has settled (a reply's `screen`)."""
    try:
        return json.loads(str(hands.after(rev, app, since, -1, -1)))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _appear(hands, count, wait, want):
    """Wait up to `wait` seconds (the caller's) for count() to exceed `want` (ui.type's index -1 waits for one)."""
    if not wait:
        return
    end = time.monotonic() + wait
    while True:
        seen = hands.changes()
        if count() > want:
            return
        left = end - time.monotonic()
        if left <= 0:
            return
        hands.awaitChange(seen, max(1, int(left * 1000)))


def _drive(cap, do, show, ready=None):
    """Make sure of the service, wait for `ready` when given, do `do(hands)`; with `show`, add the screen once settled."""
    hands = _hands(cap)
    if ready:
        ready(hands)
    before = str(hands.rev()) if show else None
    since = hands.now()
    out = _result(do(hands))
    out.update(json.loads(str(hands.front())))
    if show:
        out["screen"] = after(hands, before, since)
    return out


async def screen(args):
    _check(args, "ui.screen", ("limit", "text_limit"))
    limit, text_limit = _whole(args, "limit", None, 1), _whole(args, "text_limit", None, 0)
    return await asyncio.to_thread(lambda: screen_now(_hands("ui.screen"), limit, text_limit))


async def click(args):
    _check(args, "ui.click", ("text", "desc", "id", "contains", "index", "long", "wait", "screen"))
    t, d, i = _text(args, "text"), _text(args, "desc"), _text(args, "id")
    if not (t or d or i):
        raise ValueError("give text, desc or id")
    contains, index, long = _flag(args, "contains", False), _whole(args, "index", 0, 0), _flag(args, "long", False)
    wait, show = _seconds(args, "wait", 0), _flag(args, "screen", False)
    return await asyncio.to_thread(_drive, "ui.click", lambda h: h.click(t, d, i, contains, index, long), show,
                                   lambda h: _appear(h, lambda: h.matchCount(t, d, i, contains), wait, index))


async def scroll(args):
    _check(args, "ui.scroll", ("direction", "text", "id", "contains", "screen"))
    direction = args.get("direction", "down")
    if direction not in ("down", "up"):
        raise ValueError("direction must be down or up")
    t, i, contains, show = _text(args, "text"), _text(args, "id"), _flag(args, "contains", False), _flag(args, "screen", False)
    return await asyncio.to_thread(_drive, "ui.scroll", lambda h: h.scroll(direction == "down", t, i, contains), show)


async def type_text(args):
    _check(args, "ui.type", ("text", "label", "id", "contains", "index", "append", "submit", "wait", "screen"), ("text",))
    if not isinstance(args["text"], str):
        raise ValueError("text must be a string")
    text, label, i = args["text"], _text(args, "label"), _text(args, "id")
    contains, index = _flag(args, "contains", False), _whole(args, "index", -1, -1)
    append, submit = _flag(args, "append", False), _flag(args, "submit", False)
    wait, show = _seconds(args, "wait", 0), _flag(args, "screen", False)
    return await asyncio.to_thread(_drive, "ui.type", lambda h: h.typeInto(label, i, contains, index, text, append, submit),
                                   show, lambda h: _appear(h, lambda: h.fieldCount(label, i, contains), wait, max(index, 0)))


def brief(shot):
    """A failed wait's screen in one line: the app and every label."""
    labels = [item["text"] for item in shot.get("items", ()) if item.get("text")]
    return f"{shot.get('app') or shot.get('package') or '?'}: {', '.join(labels) or '(nothing to act on)'}"


def _wait(t, d, i, contains, gone, package, since, settle_ms, timeout, show):
    hands = _hands("ui.wait")
    quiet = settle_ms or (0 if (t or d or i or package or since) else STILL_MS)
    state = {}
    start = time.monotonic()

    def met():
        """(True, None) when every condition holds; else (False, ms until only stillness is missing, or None)."""
        if not hands.shown():           # no window for a moment while apps switch: neither gone nor settled
            return False, None
        if t or d or i:
            n = int(hands.matchCount(t, d, i, contains))
            state["matches"] = n
            if (n > 0) == gone:
                return False, None
        if package:
            app = json.loads(str(hands.front()))
            if package.lower() not in (app.get("package", "").lower(), app.get("app", "").lower()):
                return False, None
        if since and str(hands.rev()) == since:
            return False, None
        if quiet:
            q = int(hands.quietMs())
            if q < quiet:
                return False, quiet - q
        return True, None

    while True:
        seen = hands.changes()
        ok, still = met()
        if ok:
            break
        ms = still or 0                  # 0: until the screen changes
        if timeout is not None:
            left = timeout - (time.monotonic() - start)
            if left <= 0:
                waited = int((time.monotonic() - start) * 1000)
                raise ValueError(f"timed out after {waited / 1000:.1f}s; the screen now: {brief(screen_now(hands))}")
            ms = min(ms, left * 1000) if ms else left * 1000
        hands.awaitChange(seen, max(1, int(ms)) if ms else 0)
    out = {"waited_ms": int((time.monotonic() - start) * 1000), **state}
    if show:
        out["screen"] = screen_now(hands)
    else:
        out.update(json.loads(str(hands.front())))
    return out


async def wait(args):
    _check(args, "ui.wait", ("text", "desc", "id", "contains", "gone", "package", "changed_from", "settle_ms",
                             "timeout", "screen"))
    return await asyncio.to_thread(
        _wait, _text(args, "text"), _text(args, "desc"), _text(args, "id"), _flag(args, "contains", False),
        _flag(args, "gone", False), _text(args, "package"), _text(args, "changed_from"),
        _whole(args, "settle_ms", 0, 0), _seconds(args, "timeout", None), _flag(args, "screen", True))


def _str(description=None):
    out = {"type": "string", "default": ""}
    if description:
        out["description"] = description
    return out


CAPS = {} if jclass is None else {
    "ui.screen": Cap(
        "The screen, compactly: app and package; items to act on, each with text, kind (button, field, toggle, list), "
        "detail, id, checked, a field's value and hint, and its centre x, y for hid.click; the other text top to bottom; "
        "focus; locked; rev (for ui.wait changed_from). Popups and dialogs above the app are included; passwords never are.",
        {"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer", "minimum": 1, "description": "At most this many items; every one when not given."},
            "text_limit": {"type": "integer", "minimum": 0, "description": "At most this many lines of other text; every one when not given."}}},
        screen),
    "ui.click": Cap(
        "Click what matches text, desc or id (every given one must match, exactly and ignoring case unless contains; "
        "index picks among matches); the nearest clickable parent, else a tap on its centre; long long-presses; wait "
        "first waits that many seconds for it to appear; screen returns the screen once the click has settled.",
        {"type": "object", "additionalProperties": False, "properties": {
            "text": _str(), "desc": _str(), "id": _str("View id, with or without the package: prefix."),
            "contains": {"type": "boolean", "default": False}, "index": {"type": "integer", "minimum": 0, "default": 0},
            "long": {"type": "boolean", "default": False}, "wait": {"type": "number", "minimum": 0, "default": 0},
            "screen": {"type": "boolean", "default": False}}},
        click),
    "ui.scroll": Cap(
        "Scroll one page down or up: the scrollable holding the text or id match, else the outermost one on screen "
        "(an open dropdown's list first); scrolled false: already at the end; screen returns the screen once settled.",
        {"type": "object", "additionalProperties": False, "properties": {
            "direction": {"type": "string", "enum": ["down", "up"], "default": "down"},
            "text": _str(), "id": _str(), "contains": {"type": "boolean", "default": False},
            "screen": {"type": "boolean", "default": False}}},
        scroll),
    "ui.type": Cap(
        "Type text into a field without clicking it first: label names it (hint, description, label, or the caption "
        "above or left of it), id its view id, neither the focused or only field; index picks among several; replaces "
        "its text unless append; submit then presses enter (Android 11+); wait and screen as ui.click. The reply never repeats the text.",
        {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string"}, "label": _str(), "id": _str(), "contains": {"type": "boolean", "default": False},
            "index": {"type": "integer", "minimum": -1, "default": -1}, "append": {"type": "boolean", "default": False},
            "submit": {"type": "boolean", "default": False}, "wait": {"type": "number", "minimum": 0, "default": 0},
            "screen": {"type": "boolean", "default": False}}},
        type_text),
    "ui.wait": Cap(
        "Wait until the screen is how you want it, then return it (screen false leaves it out): something matching "
        "text, desc or id shows (or with gone none does), package is in front, rev differs from changed_from, and it "
        "has been still for settle_ms (600 with no other condition); timeout seconds, else until it is true.",
        {"type": "object", "additionalProperties": False, "properties": {
            "text": _str(), "desc": _str(), "id": _str(), "contains": {"type": "boolean", "default": False},
            "gone": {"type": "boolean", "default": False}, "package": _str("A package or an app's name."),
            "changed_from": _str("A rev from ui.screen."), "settle_ms": {"type": "integer", "minimum": 0, "default": 0},
            "timeout": {"type": "number", "minimum": 0, "description": "Seconds; waits until it is true when not given."},
            "screen": {"type": "boolean", "default": True}}},
        wait),
}
