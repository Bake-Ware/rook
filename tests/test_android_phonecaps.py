"""device.intent / device.apps / device.launch label= / device.sound / media.*:
the plain-data parts of the Android caps that drive the phone, off-device."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from tests.test_plugin_contract import _android_stubs


@pytest.fixture
def mods(monkeypatch):
    _android_stubs(monkeypatch)
    dev = importlib.import_module("rook_android.plugins.device_android")
    media = importlib.import_module("rook_android.plugins.media_android")
    return SimpleNamespace(dev=dev, media=media)


# ---- device.intent ----------------------------------------------------------

def test_share_and_dial_and_search(mods):
    spec = mods.dev.intent_spec("share", text="hello", subject="hi")
    assert spec["action"] == "android.intent.action.SEND" and spec["type"] == "text/plain"
    assert spec["extras"] == {"android.intent.extra.TEXT": "hello",
                              "android.intent.extra.SUBJECT": "hi"}
    assert spec["chooser"]
    assert mods.dev.intent_spec("dial", to="+1 555 0100")["data"] == "tel:+1%20555%200100"
    assert mods.dev.intent_spec("DIAL", to="5550100")["action"] == "android.intent.action.DIAL"
    assert mods.dev.intent_spec("search", text="weather")["extras"] == {"query": "weather"}


def test_navigate_and_map_encode_the_place(mods):
    nav = mods.dev.intent_spec("navigate", to="1600 Amphitheatre Pkwy, Mountain View", mode="walk")
    assert nav["action"] == "android.intent.action.VIEW"
    assert nav["data"].startswith("https://www.google.com/maps/dir/?api=1&destination=1600%20")
    assert "travelmode=walking" in nav["data"] and "dir_action=navigate" in nav["data"]
    assert "travelmode=driving" in mods.dev.intent_spec("navigate", to="home")["data"]
    assert mods.dev.intent_spec("map", to="coffee & tea")["data"] == "geo:0,0?q=coffee%20%26%20tea"
    with pytest.raises(ValueError):
        mods.dev.intent_spec("navigate", to="x", mode="teleport")


def test_timer_and_alarm_extras(mods):
    t = mods.dev.intent_spec("timer", seconds="600", label="pasta")
    assert t["action"] == "android.intent.action.SET_TIMER"
    assert t["extras"] == {"android.intent.extra.alarm.LENGTH": 600,
                           "android.intent.extra.alarm.SKIP_UI": True,
                           "android.intent.extra.alarm.MESSAGE": "pasta"}
    assert mods.dev.intent_spec("timer", seconds=5, start=False)["extras"][
        "android.intent.extra.alarm.SKIP_UI"] is False
    a = mods.dev.intent_spec("alarm", hour=6, minute="30")
    assert a["extras"]["android.intent.extra.alarm.HOUR"] == 6
    assert a["extras"]["android.intent.extra.alarm.MINUTES"] == 30
    for bad in ({"kind": "timer"}, {"kind": "timer", "seconds": 90000},
                {"kind": "alarm"}, {"kind": "alarm", "hour": 24}):
        with pytest.raises(ValueError):
            mods.dev.intent_spec(**bad)


def test_settings_pages(mods):
    assert mods.dev.intent_spec("settings", page="wifi")["action"] == "android.settings.WIFI_SETTINGS"
    assert mods.dev.intent_spec("settings")["action"] == "android.settings.SETTINGS"
    app = mods.dev.intent_spec("settings", page="app", package="com.example")
    assert app == {"action": "android.settings.APPLICATION_DETAILS_SETTINGS",
                   "data": "package:com.example"}
    n = mods.dev.intent_spec("settings", page="notifications", package="com.example")
    assert n["extras"] == {"android.provider.extra.APP_PACKAGE": "com.example"}
    raw = mods.dev.intent_spec("settings", page="android.settings.CAST_SETTINGS")
    assert raw["action"] == "android.settings.CAST_SETTINGS"
    with pytest.raises(ValueError, match="page is one of"):
        mods.dev.intent_spec("settings", page="hyperdrive")
    with pytest.raises(ValueError):
        mods.dev.intent_spec("settings", page="app")


def test_email_and_sms_only_open_drafts(mods):
    e = mods.dev.intent_spec("email", to="a@b.c", subject="Hi there", text="x&y")
    assert e["action"] == "android.intent.action.SENDTO"
    assert e["data"] == "mailto:a@b.c?subject=Hi%20there&body=x%26y"
    s = mods.dev.intent_spec("sms", to="+15550100", text="on my way")
    assert s["action"] == "android.intent.action.SENDTO" and s["data"] == "smsto:+15550100"
    assert s["extras"] == {"sms_body": "on my way"}
    with pytest.raises(ValueError, match="kind is one of"):
        mods.dev.intent_spec("call", to="911")


def test_intent_cap_reports_bad_input_without_a_phone(mods):
    p = mods.dev.AndroidDevicePlugin.__new__(mods.dev.AndroidDevicePlugin)
    assert asyncio.run(p._intent(kind="share"))["error"] == "share needs text"
    assert asyncio.run(p._intent(kind="share", text="x"))["error"] == "not an Android host"


# ---- device.apps / device.launch label= ----------------------------------------

APPS = [{"label": "Calculator", "package": "com.sec.android.app.popupcalculator"},
        {"label": "Calendar", "package": "com.samsung.android.calendar"},
        {"label": "Claude", "package": "com.anthropic.claude"},
        {"label": "Maps", "package": "com.google.android.apps.maps"},
        {"label": "Rook Worker", "package": "systems.bake.rook"},
        {"label": "Rook Worker", "package": "systems.bake.rook"}]


def test_pick_app(mods):
    pick = mods.dev.pick_app
    assert pick(APPS, "maps")[0]["package"] == "com.google.android.apps.maps"
    assert pick(APPS, "com.anthropic.claude")[0]["label"] == "Claude"
    assert pick(APPS, "rook")[0]["package"] == "systems.bake.rook"     # two entries, one app
    app, hits = pick(APPS, "cal")
    assert app is None and {h["label"] for h in hits} == {"Calculator", "Calendar"}
    assert pick(APPS, "calc")[0]["label"] == "Calculator"
    assert pick(APPS, "anthropic")[0]["label"] == "Claude"
    assert pick(APPS, "zzz") == (None, [])
    assert pick(APPS, " ") == (None, [])


def test_launch_takes_a_label_and_lists_candidates(mods, monkeypatch):
    p = mods.dev.AndroidDevicePlugin.__new__(mods.dev.AndroidDevicePlugin)
    assert asyncio.run(p._launch())["error"] == "not an Android host"
    monkeypatch.setattr(mods.dev, "app_context", lambda: object())
    monkeypatch.setattr(mods.dev.AndroidDevicePlugin, "_launcher_apps", lambda self, ctx: APPS)
    assert asyncio.run(p._launch())["error"] == "give package or label"
    out = asyncio.run(p._launch(package="Cal"))          # no dot: a label
    assert out["ok"] is False and len(out["candidates"]) == 2
    assert "no launchable app" in asyncio.run(p._launch(label="zzz"))["error"]


# ---- device.sound ---------------------------------------------------------------

def test_level_index(mods):
    li = mods.dev.level_index
    assert li(0, 0, 15) == 0 and li(100, 0, 15) == 15 and li(50, 0, 15) == 8
    assert li(0, 1, 7) == 1 and li(250, 0, 7) == 7 and li("40", 0, 25) == 10


def test_sound_validates_before_touching_the_phone(mods, monkeypatch):
    p = mods.dev.AndroidDevicePlugin.__new__(mods.dev.AndroidDevicePlugin)
    monkeypatch.setattr(mods.dev, "app_context", lambda: object())
    assert "stream is one of" in p._sound(stream="bass")["error"]
    assert "step is" in p._sound(step="sideways")["error"]
    assert "ringer is" in p._sound(ringer="loud")["error"]
    assert "dnd is" in p._sound(dnd="maybe")["error"]


class _Stream:
    def __init__(self):
        self.vol = {0: 3, 2: 5, 3: 7, 4: 6, 5: 5}
        self.calls = []
        self.ringer = 2

    def getStreamVolume(self, c): return self.vol[c]
    def getStreamMaxVolume(self, c): return 15 if c == 3 else 7
    def getStreamMinVolume(self, c): return 0
    def setStreamVolume(self, c, i, f): self.calls.append(("set", c, i, f)); self.vol[c] = i
    def adjustStreamVolume(self, c, d, f): self.calls.append(("adj", c, d, f))
    def getRingerMode(self): return self.ringer

    def setRingerMode(self, m):
        if m == 0:
            raise type("SecurityException", (Exception,), {})("Not allowed to change Do Not Disturb state")
        self.ringer = m


class _Notify:
    filt = 1
    def isNotificationPolicyAccessGranted(self): return False
    def getCurrentInterruptionFilter(self): return self.filt
    def setInterruptionFilter(self, f): self.filt = f


def _sound_env(mods, monkeypatch):
    am, nm = _Stream(), _Notify()
    classes = {"android.content.Context": SimpleNamespace(AUDIO_SERVICE="audio",
                                                          NOTIFICATION_SERVICE="notification"),
               "android.os.Build$VERSION": SimpleNamespace(SDK_INT=34)}
    ctx = SimpleNamespace(getSystemService=lambda name: {"audio": am, "notification": nm}[name])
    monkeypatch.setattr(mods.dev, "app_context", lambda: ctx)
    monkeypatch.setattr(mods.dev, "jclass", lambda n: classes.get(n, n))
    monkeypatch.setattr(mods.dev, "cast", lambda cls, o: o)
    return am, nm, mods.dev.AndroidDevicePlugin.__new__(mods.dev.AndroidDevicePlugin)


def test_sound_reads_and_sets(mods, monkeypatch):
    am, nm, p = _sound_env(mods, monkeypatch)
    out = p._sound()
    assert out["ok"] and "changed" not in out
    assert out["streams"]["media"] == {"pct": 47, "level": 7, "max": 15}
    assert out["ringer"] == "normal" and out["dnd"] == "off" and out["dnd_access"] is False
    out = p._sound(level=100)
    assert am.calls[-1] == ("set", 3, 15, 0) and out["changed"] == ["level"]
    p._sound(stream="ring", step="down")
    assert am.calls[-1] == ("adj", 2, -1, 0)
    assert p._sound(ringer="vibrate")["ringer"] == "vibrate"
    out = p._sound(ringer="silent")
    assert out["ok"] is False and "Do Not Disturb access" in out["error"] and "streams" in out
    assert p._sound(dnd="alarms")["dnd"] == "alarms"


# ---- media.* ----------------------------------------------------------------------

def test_pick_session(mods):
    ss = [{"package": "com.spotify.music", "app": "Spotify", "state": "paused"},
          {"package": "com.pandora.android", "app": "Pandora", "state": "playing"}]
    pick = mods.media.pick_session
    assert pick(ss)["app"] == "Pandora"
    assert pick(ss, "spotify")["package"] == "com.spotify.music"
    assert pick(ss, "com.spotify.music")["app"] == "Spotify"
    assert pick(ss, "youtube") is None
    assert pick([ss[0]])["app"] == "Spotify" and pick([]) is None


def test_media_control_rejects_unknown_actions(mods):
    p = mods.media.AndroidMediaPlugin.__new__(mods.media.AndroidMediaPlugin)
    assert "action is one of" in p._control(action="explode")["error"]
    assert p._control(action="play_pause")["error"] == "not an Android host"
    assert p._now()["error"] == "not an Android host"
    assert set(mods.media.KEYS) >= {"play", "pause", "toggle", "next", "previous"}
    json.dumps(mods.media.STATES)


def test_background_start_warns_only_without_an_exemption(mods, monkeypatch):
    flags = {"sdk": 34, "overlay": False, "a11y": False}
    classes = {"android.os.Build$VERSION": lambda: SimpleNamespace(SDK_INT=flags["sdk"]),
               "android.provider.Settings": lambda: SimpleNamespace(
                   canDrawOverlays=lambda ctx: flags["overlay"]),
               "systems.bake.rook.HidAccessibilityService": lambda: SimpleNamespace(
                   isEnabled=lambda: flags["a11y"])}
    monkeypatch.setattr(mods.dev, "jclass", lambda n: classes[n]())
    out = mods.dev._bal({"ok": True, "url": "x"}, object())
    assert out["ok"] and out["may_be_blocked"] is True and "Display over other apps" in out["note"]
    for k, v in (("overlay", True), ("a11y", True), ("sdk", 28)):
        f = {"sdk": 34, "overlay": False, "a11y": False, k: v}
        flags.update(f)
        assert mods.dev._bal({"ok": True}, object()) == {"ok": True}
