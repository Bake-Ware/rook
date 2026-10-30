"""ui.*: systems.bake.rook.HidAccessibilityService (its ScreenModel) through
rook.kit.android.screen. Empty strings mean "not given"."""

from rook.kit import android
from rook.kit.android import screen as sc
from rook.kit.ctx import KitError


def _opt(v):
    return v if v and v.strip() else None


def _drive(cap, do, show, ready=None):
    """Wake, wait for ``ready(b)`` when given, run ``do(b)`` (a bridge call
    answering JSON); with ``show`` add the screen once the change settled."""
    sc.awake(cap)
    b = sc.hid()
    if ready:
        ready(b)
    before = sc.rev(b) if show else None
    out = {**android.result(do(b)), **sc.front(b)}
    if show:
        out["screen"] = sc.after(b, before)
    return out


def _appear(count, wait, want):
    """Wait up to ``wait`` seconds for ``count()`` (-1: can't tell) to exceed ``want``."""
    if wait > 0:
        sc.until(lambda: (n := count()) < 0 or n > want, wait)


def screen(ctx, limit, text_limit):
    sc.awake("ui.screen")
    return sc.summary(sc.hid(), limit, text_limit)


def click(ctx, text, desc, id, contains, index, long, wait, screen):
    t, d, i = _opt(text), _opt(desc), _opt(id)
    if not (t or d or i):
        raise KitError("give text, desc or id")
    return _drive("ui.click", lambda b: b.clickNode(t, d, i, contains, index, long), screen,
                  lambda b: _appear(lambda: b.matchCount(t, d, i, contains), wait, index))


def scroll(ctx, direction, text, id, contains, screen):
    return _drive("ui.scroll", lambda b: b.scrollNode(direction == "down", _opt(text), _opt(id), contains), screen)


def type_text(ctx, text, label, id, contains, index, append, submit, wait, screen):
    lab, i = _opt(label), _opt(id)
    return _drive("ui.type", lambda b: b.typeInto(lab, i, contains, index, text, append, submit), screen,
                  lambda b: _appear(lambda: b.fieldCount(lab, i, contains), wait, index))


def wait(ctx, text, desc, id, contains, gone, package, changed_from, settle_ms, timeout, screen):
    sc.awake("ui.wait")
    b = sc.hid()
    t, d, i, pkg, since = _opt(text), _opt(desc), _opt(id), _opt(package), _opt(changed_from)
    quiet = settle_ms or (0 if (t or d or i or pkg or since) else 600)
    state = {}

    def met():
        # No window for a moment while apps switch: that is neither gone nor settled.
        if not sc.shown(b):
            return False
        if t or d or i:
            n = b.matchCount(t, d, i, contains)
            state["matches"] = max(n, 0)
            if (n > 0) == gone:
                return False
        if pkg:
            app = sc.front(b)
            if pkg.lower() not in (app.get("package", "").lower(), app.get("app", "").lower()):
                return False
        if since and sc.rev(b) == since:
            return False
        if quiet:
            q = sc.quiet_ms(b)
            if 0 <= q < quiet:
                return False
        return True

    ok, waited = sc.until(met, timeout)
    if not ok:
        raise KitError(f"timed out after {waited / 1000:.1f}s; the screen now: {_brief(sc.summary(b))}")
    out = {"waited_ms": waited, **state}
    if screen:
        out["screen"] = sc.summary(b)
    else:
        out.update(sc.front(b))
    return out


def _brief(s):
    """A failed wait's screen in one line: the app and the first labels."""
    labels = [i["text"] for i in s.get("items", ()) if i.get("text")][:12]
    return f"{s.get('app') or s.get('package') or '?'}: {', '.join(labels) or '(nothing to act on)'}"
