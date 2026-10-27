"""ui.* (Android APK plugin) against a fake HidAccessibilityService bridge, no device needed."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PY = Path(__file__).parents[1] / "android/app/src/main/python"


class FakeHid:
    def __init__(self, enabled=True):
        self.enabled, self.calls = enabled, []

    def isEnabled(self): return self.enabled
    def screenApp(self): return ["com.usaa.mobile.android.usaa", "USAA"]
    def back(self): self.calls.append(("back",)); return True

    def screenNodes(self, all, limit):
        self.calls.append(("nodes", all, limit))
        return json.dumps([
            {"text": "Trip History", "class": "TextView", "bounds": [0, 100, 1080, 200]},
            {"desc": "Oct 14 trip", "id": "pkg:id/trip", "clickable": True, "bounds": [0, 300, 1080, 500]},
        ])

    def clickNode(self, text, desc, id, contains, index, long):
        self.calls.append(("click", text, desc, id, contains, index, long))
        return json.dumps({"ok": True, "via": "action", "clicked": {"text": text}})

    def scrollNode(self, forward, text, id, contains):
        self.calls.append(("scroll", forward, text, id, contains))
        return json.dumps({"ok": True, "scrolled": True})

    # ui.screen / ui.wait / ui.type / screen=true
    revs = ["r1"]
    quiet = 1000
    present = 1

    def screenRev(self):
        return self.revs.pop(0) if len(self.revs) > 1 else self.revs[0]

    def quietMs(self): return self.quiet

    def matchCount(self, text, desc, id, contains):
        self.calls.append(("count", text, desc, id, contains))
        n = self.present
        if isinstance(n, list):
            n = n.pop(0) if len(n) > 1 else n[0]
        return n

    def fieldCount(self, label, id, contains):
        self.calls.append(("fields", label, id, contains))
        return 1

    def screenSummary(self, max_items, max_text):
        self.calls.append(("summary", max_items, max_text))
        return json.dumps({"items": [{"label": "Settings", "kind": "button", "at": [5, 5]}],
                           "text": ["Trip History"], "rev": "r2"})

    def typeInto(self, label, id, contains, index, text, append, submit):
        self.calls.append(("type", label, id, contains, index, text, append, submit))
        return json.dumps({"ok": True, "field": {"label": label or "field"}, "chars": len(text)})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def load(monkeypatch):
    monkeypatch.syspath_prepend(str(PY))
    for k in [k for k in sys.modules if k == "rook_android" or k.startswith("rook_android.")]:
        monkeypatch.delitem(sys.modules, k)

    def _load(hid):
        spec = importlib.util.spec_from_file_location("ui_android_test",
                                                      PY / "rook_android/plugins/ui_android.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m._Hid = hid

        async def no_sleep(_s): return None
        monkeypatch.setattr(m._sw.asyncio, "sleep", no_sleep)
        return m, m.AndroidUiPlugin.__new__(m.AndroidUiPlugin)
    return _load


def test_nodes_filter_and_app(load):
    m, p = load(FakeHid())
    out = p._nodes(contains="trip h")
    assert out["ok"] and out["package"] == "com.usaa.mobile.android.usaa"
    assert [n.get("text") for n in out["nodes"]] == ["Trip History"]
    assert out["count"] == 1 and not out["truncated"]
    assert p._nodes(limit=1)["truncated"] is True


def test_click_passes_criteria_and_blanks_are_none(load):
    hid = FakeHid()
    m, p = load(hid)
    out = run(p._click(text="I was the passenger", desc="", id="", contains="false", index=0, long=False))
    assert out["ok"] and out["via"] == "action" and out["app"] == "USAA"
    assert hid.calls[-1] == ("click", "I was the passenger", None, None, False, 0, False)
    run(p._click(desc="Oct 14", contains="true", index="1", long="yes"))
    assert hid.calls[-1] == ("click", None, "Oct 14", None, True, 1, True)


def test_scroll_direction(load):
    hid = FakeHid()
    m, p = load(hid)
    assert run(p._scroll())["scrolled"] is True
    assert hid.calls[-1] == ("scroll", True, None, None, False)
    run(p._scroll(direction="up", id="list"))
    assert hid.calls[-1] == ("scroll", False, None, "list", False)
    assert run(p._scroll(direction="sideways"))["ok"] is False


def test_back_and_disabled_service(load):
    hid = FakeHid()
    m, p = load(hid)
    assert run(p._back()) == {"ok": True}
    m, p = load(FakeHid(enabled=False))
    for out in (p._nodes(), run(p._click(text="x")), run(p._scroll()), run(p._back())):
        assert out["ok"] is False and "accessibility" in out["error"]


def test_null_bridge_reply_is_an_error(load):
    hid = FakeHid()
    hid.clickNode = lambda *a: None
    m, p = load(hid)
    assert run(p._click(text="x"))["ok"] is False


# ---- ui.screen / ui.wait / ui.type / screen=true ------------------------------

def test_screen_summary_adds_the_app(load):
    hid = FakeHid()
    m, p = load(hid)
    out = run(p._screen(limit="500", text_limit=""))
    assert out["ok"] and out["app"] == "USAA" and out["rev"] == "r2"
    assert out["items"][0]["label"] == "Settings"
    assert hid.calls[-1] == ("summary", 300, 40)       # clamped; blank = default


def test_click_waits_for_the_node_then_returns_the_new_screen(load):
    hid = FakeHid()
    hid.present = [0, 0, 1]
    hid.revs = ["before", "before", "after"]
    m, p = load(hid)
    out = run(p._click(text="Settings", wait=3, screen=True))
    counts = [c for c in hid.calls if c[0] == "count"]
    assert len(counts) == 3                      # polled until it appeared
    assert hid.calls.index(counts[-1]) < hid.calls.index(("click", "Settings", None, None, False, 0, False))
    assert out["ok"] and out["screen"]["changed"] is True and out["screen"]["settled"] is True
    assert out["screen"]["items"][0]["label"] == "Settings" and out["screen"]["app"] == "USAA"


def test_click_without_wait_or_screen_is_one_bridge_call(load):
    hid = FakeHid()
    m, p = load(hid)
    out = run(p._click(text="Settings"))
    assert "screen" not in out
    assert [c[0] for c in hid.calls] == ["click"]


def test_click_wait_gives_up_and_still_reports_not_found(load):
    hid = FakeHid()
    hid.present = 0
    hid.clickNode = lambda *a: json.dumps({"ok": False, "error": "no visible node matches"})
    m, p = load(hid)
    clock = iter(range(0, 1000))
    monkey = m._sw
    monkey._clock = lambda: next(clock) * 0.5
    out = run(p._click(text="Nope", wait=2))
    assert out["ok"] is False and "no visible node" in out["error"]


def test_wait_for_text_and_gone(load):
    hid = FakeHid()
    hid.present = [0, 2]
    m, p = load(hid)
    out = run(p._wait(text="Trip", contains=True, timeout=5))
    assert out["ok"] and out["matches"] == 2 and out["screen"]["rev"] == "r2"
    hid.present = [1, 0]
    out = run(p._wait(text="Loading", gone=True, screen=False))
    assert out["ok"] and out["matches"] == 0 and "screen" not in out and out["app"] == "USAA"


def test_wait_settles_by_default_and_times_out_with_the_screen(load):
    hid = FakeHid()
    hid.quiet = 100                              # always busy
    m, p = load(hid)
    clock = iter(range(0, 1000))
    m._sw._clock = lambda: next(clock) * 0.5
    out = run(p._wait(timeout=2))
    assert out["ok"] is False and "timed out" in out["error"] and out["screen"]["rev"] == "r2"
    hid.quiet = 900
    assert run(p._wait())["ok"] is True


def test_wait_for_package_and_changed_rev(load):
    hid = FakeHid()
    m, p = load(hid)
    assert run(p._wait(package="usaa", timeout=0))["ok"] is True
    assert run(p._wait(package="com.usaa.mobile.android.usaa", timeout=0))["ok"] is True
    assert run(p._wait(package="com.other", timeout=0))["ok"] is False
    hid.revs = ["r1"]
    assert run(p._wait(changed_from="r1", timeout=0))["ok"] is False
    assert run(p._wait(changed_from="r0", timeout=0))["ok"] is True


def test_type_into_a_named_field_never_echoes_the_text(load):
    hid = FakeHid()
    m, p = load(hid)
    out = run(p._type(text="hunter2", label="Password", submit="yes"))
    assert hid.calls[-1] == ("type", "Password", None, False, -1, "hunter2", False, True)
    assert out["ok"] and "hunter2" not in json.dumps(out) and out["app"] == "USAA"
    run(p._type(text="more", append=True, label="", id="", wait=1))
    assert ("fields", None, None, False) in hid.calls
    assert hid.calls[-1] == ("type", None, None, False, -1, "more", True, False)
    run(p._type(text="y", label="Name", index="1"))
    assert hid.calls[-1] == ("type", "Name", None, False, 1, "y", False, False)


def test_new_caps_need_the_service(load):
    m, p = load(FakeHid(enabled=False))
    for out in (run(p._screen()), run(p._wait()), run(p._type(text="x"))):
        assert out["ok"] is False and "accessibility" in out["error"]


def test_wait_needs_a_window_in_front(load):
    hid = FakeHid()
    hid.present = 0
    apps = [None, None, ["com.x", "X"]]
    hid.screenApp = lambda: apps.pop(0) if len(apps) > 1 else apps[0]
    m, p = load(hid)
    out = run(p._wait(text="Loading", gone=True, screen=False, timeout=5))
    assert out["ok"] and len(apps) == 1        # it waited past the gap with no window


def test_screen_never_mixes_another_apps_label(load):
    hid = FakeHid()
    hid.screenSummary = lambda a, b: json.dumps({"package": "com.next", "items": [], "text": [],
                                                 "rev": "r9"})
    m, p = load(hid)
    out = run(p._screen())
    assert out["package"] == "com.next" and "app" not in out


def test_screen_after_an_action_says_when_it_never_settled(load):
    hid = FakeHid()
    hid.quiet = 10                                   # a spinner: always changing
    hid.revs = ["a", "b"]
    m, p = load(hid)
    clock = iter(range(0, 1000))
    m._sw._clock = lambda: next(clock) * 0.5
    out = run(p._click(text="Settings", screen=True))
    assert out["screen"]["settled"] is False and out["screen"]["changed"] is True
