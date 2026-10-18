"""ui.* (Android APK plugin) against a fake HidAccessibilityService bridge, no device needed."""
import importlib.util
import json
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


@pytest.fixture
def load():
    def _load(hid):
        spec = importlib.util.spec_from_file_location("ui_android_test",
                                                      PY / "rook_android/plugins/ui_android.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m._Hid = hid
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
    out = p._click(text="I was the passenger", desc="", id="", contains="false", index=0, long=False)
    assert out["ok"] and out["via"] == "action" and out["app"] == "USAA"
    assert hid.calls[-1] == ("click", "I was the passenger", None, None, False, 0, False)
    p._click(desc="Oct 14", contains="true", index="1", long="yes")
    assert hid.calls[-1] == ("click", None, "Oct 14", None, True, 1, True)


def test_scroll_direction(load):
    hid = FakeHid()
    m, p = load(hid)
    assert p._scroll()["scrolled"] is True
    assert hid.calls[-1] == ("scroll", True, None, None, False)
    p._scroll(direction="up", id="list")
    assert hid.calls[-1] == ("scroll", False, None, "list", False)
    assert p._scroll(direction="sideways")["ok"] is False


def test_back_and_disabled_service(load):
    hid = FakeHid()
    m, p = load(hid)
    assert p._back() == {"ok": True}
    m, p = load(FakeHid(enabled=False))
    for out in (p._nodes(), p._click(text="x"), p._scroll(), p._back()):
        assert out["ok"] is False and "accessibility" in out["error"]


def test_null_bridge_reply_is_an_error(load):
    hid = FakeHid()
    hid.clickNode = lambda *a: None
    m, p = load(hid)
    assert p._click(text="x")["ok"] is False
