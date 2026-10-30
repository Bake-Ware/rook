"""The phone plugins (``platforms: [android]``, the Rook Android app) on 0.5:
manifests, and every cap through the worker host with Chaquopy's ``java``
faked (phone_fakes.FakeJava). Nothing here needs a phone."""

import json
import sys

import pytest

from phone_fakes import Intent, java  # noqa: F401  (the fixture)
from port_helpers import CONTRACTS, ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host
from rook.kit.ctx import Caller, Ctx, KitError
from rook.kit.manifest import check, load, trust_class

PHONE = sorted(p.parent.name for p in PLUGINS.glob("*/plugin.yaml") if "\nplatforms: [android]\n" in p.read_text())


class Phone:
    """Calls caps through a worker host that thinks it runs on Android."""

    def __init__(self, data):
        self.data = data
        self.events: list[tuple[str, dict]] = []

    def call(self, cap, **args):
        async def body():
            host = Host(os_name="android", data_root=self.data, emit=lambda e, p, plugin=None: None)
            await host.load(PLUGINS / cap.partition(".")[0])
            try:
                return await host.call(cap, args, ME)
            finally:
                await host.close()
        return go(body)

    def err(self, cap, **args) -> str:
        with pytest.raises(CallError) as e:
            self.call(cap, **args)
        return e.value.message

    def hook(self, plugin, ref, settings=None):
        """Run a hook function as the host would, with this phone's events and data."""
        from rook.core2.worker.loader import load_folder
        p = load_folder(PLUGINS / plugin, os_name="android")
        ctx = Ctx(p.manifest, Caller(plugin=plugin, why="test hook"), settings=settings,
                  emit=lambda e, payload, plugin=None: self.events.append((e, payload)),
                  data_dir=self.data / plugin)
        try:
            return p.resolve(ref)(ctx)
        finally:
            p.forget_modules()


@pytest.fixture
def phone(java, tmp_path):
    return Phone(tmp_path / "data")


# -- manifests ------------------------------------------------------------------

def test_the_phone_plugins_are_there():
    assert PHONE == ["app-android", "battery-android", "calendar", "call", "clipboard-android", "clock",
                     "contact", "hid-android", "location", "media-android", "notification-android", "phone",
                     "screen-android", "sms", "sound-android", "ui-android", "voice-android"]


@pytest.mark.parametrize("name", PHONE)
def test_manifest_is_clean_and_code(name):
    data, issues = check(PLUGINS / name, CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
    m = load(PLUGINS / name, CONTRACTS)
    assert m.plugin == name and (PLUGINS / name / "docs" / "manual.md").is_file()
    assert trust_class(PLUGINS / name) == "code"
    for cap, spec in m.caps.items():
        assert spec["doc"] and spec["risk"] and spec["scope"] in ("local", "external") and "run" in spec, cap
        if spec["risk"] == "read":
            assert not {"physical", "sends_as", "destructive"} & set(spec["effects"]), cap


def test_effects_say_what_the_caps_do():
    caps = {c: s for n in PHONE for c, s in load(PLUGINS / n, CONTRACTS).caps.items()}
    for cap in ("sms.send", "call.dial", "notification-android.notification.act"):
        assert "sends_as" in caps[cap]["effects"] and caps[cap]["scope"] == "external", cap
    for cap in ("sms.list", "contact.search", "call.list", "location.get", "calendar.list",
                "notification-android.notification.list"):
        assert "personal" in caps[cap]["effects"], cap
    for cap in ("sms.list", "notification-android.notification.list", "notification-android.notification.get",
                "ui-android.ui.screen", "screen-android.screen.capture", "voice-android.voice.ask"):
        assert "taints" in caps[cap]["effects"], cap
    assert "destructive" in caps["calendar.delete"]["effects"]
    assert caps["phone.unlock"]["args"]["pin"]["type"] == "secret"
    sms, notes = load(PLUGINS / "sms", CONTRACTS), load(PLUGINS / "notification-android", CONTRACTS)
    assert sms.data["emits"]["sms.received"]["taints"] is True
    assert notes.data["emits"]["notification-android.received"]["taints"] is True
    assert not notes.data["emits"]["notification-android.card"].get("taints")


def test_device_intent_is_split_and_voice_is_the_device_contract():
    names = {c for n in PHONE for c in load(PLUGINS / n, CONTRACTS).caps}
    assert {"call.dial", "clock.timer", "clock.alarm", "clock.alarms", "location.navigate", "location.show",
            "location.get", "app-android.app.share", "app-android.app.settings"} <= names
    provides = load(PLUGINS / "voice-android", CONTRACTS).data["provides"]
    assert provides["voice.speak"] == "voice-android.voice.speak" and CONTRACTS["voice.speak"]["route"] == "target"


def test_they_bind_on_android_and_nowhere_else(java):
    async def body():
        for os_name, served in (("android", True), ("linux", False)):
            host = Host(os_name=os_name)
            for name in PHONE:
                p = await host.load(PLUGINS / name)
                if served:
                    assert not p.unavailable and p.reason is None, name
                else:
                    assert not p.caps and p.reason == "not on linux (runs on android)", name
            await host.close()
    go(body)


def test_off_the_phone_every_helper_says_so(monkeypatch):
    from rook.kit import android
    monkeypatch.setitem(sys.modules, "java", None)   # import java raises ImportError
    with pytest.raises(KitError, match="not an Android host"):
        android.context()


# -- texts, contacts, calls -----------------------------------------------------------

INBOX = [{"address": "+15550001", "body": "new one", "date": 1_700_000_300_000, "type": 1, "read": 0},
         {"address": "+15550002", "body": "older", "date": 1_700_000_100_000, "type": 1, "read": 1}]


def test_sms_list_needs_the_grant_and_reads_the_box(phone, java):
    assert "READ_SMS not granted" in phone.err("sms.list")
    java.granted.add("android.permission.READ_SMS")
    java.tables["content://sms/inbox"] = INBOX
    got = phone.call("sms.list", limit=5, since=1_700_000_000)
    assert got["messages"][0] == {"from": "+15550001", "body": "new one", "ts": 1_700_000_300.0, "box": "inbox",
                                  "read": False}
    uri, cols, where, args, order = java.queries[-1]
    assert (where, args, order) == ("date >= ?", ["1700000000000"], "date DESC")
    assert "bad args" in phone.err("sms.list", box="trash")


def test_sms_send_splits_long_texts(phone, java):
    java.granted.add("android.permission.SEND_SMS")
    sm = java.cls("android.telephony.SmsManager").getDefault.return_value
    sm.divideMessage.return_value.size.return_value = 3
    assert phone.call("sms.send", to="+1 555 0100", text="x" * 400) == {"to": "+1 555 0100", "parts": 3}
    assert sm.sendMultipartTextMessage.called and not sm.sendTextMessage.called
    assert "bad args" in phone.err("sms.send", to="; rm -rf", text="hi")


def test_sms_watch_announces_only_what_is_new(phone, java):
    java.tables["content://sms/inbox"] = INBOX
    phone.hook("sms", "scripts/sms.py:watch")          # no grant: nothing at all
    assert phone.events == [] and not (phone.data / "sms").exists()
    java.granted.add("android.permission.READ_SMS")
    phone.hook("sms", "scripts/sms.py:watch")          # first look: only notes the newest
    assert phone.events == [] and (phone.data / "sms" / "inbox.cursor").read_text() == "1700000300000"
    java.tables["content://sms/inbox"] = [{"address": "+15550003", "body": "hello", "date": 1_700_000_400_000}]
    phone.hook("sms", "scripts/sms.py:watch")
    assert phone.events == [("sms.received", {"from": "+15550003", "body": "hello", "ts": 1_700_000_400.0})]
    assert java.queries[-1][2:4] == ("date > ?", ["1700000300000"])
    assert (phone.data / "sms" / "inbox.cursor").read_text() == "1700000400000"


def test_contact_search_dedupes(phone, java):
    java.granted.add("android.permission.READ_CONTACTS")
    java.tables["content://com.android.contacts/data/phones"] = [
        {"display_name": "Ann", "data1": "1"}, {"display_name": "Ann", "data1": "1"},
        {"display_name": "Bob", "data1": "2"}]
    assert phone.call("contact.search", query="a")["contacts"] == [{"name": "Ann", "number": "1"},
                                                                   {"name": "Bob", "number": "2"}]
    assert java.queries[-1][3] == ["%a%", "%a%"]
    assert phone.call("contact.search", limit=1)["contacts"] == [{"name": "Ann", "number": "1"}]


def test_call_list_and_dial(phone, java):
    java.granted |= {"android.permission.READ_CALL_LOG"}
    java.tables["content://call_log/calls"] = [{"number": "5", "name": None, "type": 3, "duration": 0,
                                                "date": 1_700_000_000_000}]
    assert phone.call("call.list")["calls"] == [{"number": "5", "name": "", "direction": "missed",
                                                 "duration_s": 0, "ts": 1_700_000_000.0}]
    assert "CALL_PHONE not granted" in phone.err("call.dial", to="+1 (555) 0100")
    java.granted.add("android.permission.CALL_PHONE")
    java.ctx.getPackageManager.return_value.resolveActivity.return_value.activityInfo.packageName = "com.dialer"
    assert phone.call("call.dial", to="+1 (555) 0100") == {"to": "+1 (555) 0100", "app": "com.dialer"}
    i = java.started[-1]
    assert i.action == "android.intent.action.CALL" and i.data == "tel:+1%20%28555%29%200100"


# -- calendar -------------------------------------------------------------------

def script(plugin, name):
    """A plugin script's module, as one load of the plugin has it."""
    from rook.core2.worker.loader import load_folder
    p = load_folder(PLUGINS / plugin, os_name="android")
    return next(m for path, m in p.modules.items() if path.name == name).__dict__


def test_calendar_times():
    g = script("calendar", "calendar.py")
    utc, cst = (lambda ms: 0), (lambda ms: -6 * 3600_000)
    assert g["parse_time"]("2026-10-05T14:00Z", 0, utc) == 1_791_208_800_000
    assert g["parse_time"]("2026-10-05T14:00", 0, cst) == 1_791_208_800_000 + 6 * 3600_000
    assert g["parse_time"](1_791_208_800, 0) == 1_791_208_800_000           # seconds
    assert g["parse_time"](None, 42) == 42
    with pytest.raises(KitError, match="not a time"):
        g["parse_time"]("tuesday", 0)
    # An all-day event on Oct 5 belongs to a window inside Oct 5 local time, even west of UTC.
    day = {"title": "Fair", "allDay": 1, "begin": 1_791_158_400_000, "end": 1_791_244_800_000}
    begin = g["parse_time"]("2026-10-05T09:00", 0, cst)
    assert g["select_rows"]([day], begin, begin + 3600_000, 5, cst) == [day]
    assert g["event_row"](day, cst)["start"] == "2026-10-05" and g["event_row"](day, cst)["end"] == "2026-10-05"


def test_calendar_changes_need_the_owner_switch(phone, java):
    java.granted |= {"android.permission.READ_CALENDAR", "android.permission.WRITE_CALENDAR"}
    java.cls("java.util.TimeZone").getDefault.return_value.getOffset.return_value = 0
    assert "Let the assistant act" in phone.err("calendar.delete", event_id=7)
    java.prefs["assistant_actions"] = True
    java.tables["content://com.android.calendar/events/7"] = [{"dtstart": 1_000_000, "dtend": 4_600_000,
                                                               "title": "Dentist", "allDay": 0}]
    java.ctx.getContentResolver.return_value.delete.return_value = 1
    assert phone.call("calendar.delete", event_id=7) == {"event_id": 7, "title": "Dentist"}
    java.tables["content://com.android.calendar/events/8"] = []        # a gone event: an empty cursor
    assert "no event 8" in phone.err("calendar.delete", event_id=8)


def test_calendar_create_picks_the_primary_writable_calendar(phone, java):
    java.granted |= {"android.permission.WRITE_CALENDAR"}
    java.prefs["assistant_actions"] = True
    tz = java.cls("java.util.TimeZone").getDefault.return_value
    tz.getOffset.return_value, tz.getID.return_value = 0, "UTC"
    java.tables["content://com.android.calendar/calendars"] = [
        {"_id": 1, "calendar_displayName": "Holidays", "account_name": "g", "isPrimary": 0,
         "calendar_access_level": 200, "visible": 1},
        {"_id": 2, "calendar_displayName": "Me", "account_name": "me@x", "isPrimary": 1,
         "calendar_access_level": 700, "visible": 1}]
    values: dict = {}
    cv = java.cls("android.content.ContentValues").return_value
    cv.put.side_effect = lambda k, v: values.__setitem__(k, v)
    cv.getAsLong.side_effect = lambda k: values[k]
    java.cls("java.lang.Long").side_effect = int
    java.cls("java.lang.Integer").side_effect = int
    java.cls("android.content.ContentUris").parseId.return_value = 99
    got = phone.call("calendar.create", title="Lunch", start="2026-10-05T12:00Z")
    assert got == {"event_id": 99, "calendar": "Me", "start": "2026-10-05T12:00+00:00", "end": "2026-10-05T13:00+00:00"}
    assert values["calendar_id"] == 2 and values["dtend"] - values["dtstart"] == 3600_000


def test_calendar_update_keeps_the_length_when_moved(phone, java):
    java.granted |= {"android.permission.WRITE_CALENDAR"}
    java.prefs["assistant_actions"] = True
    tz = java.cls("java.util.TimeZone").getDefault.return_value
    tz.getOffset.return_value, tz.getID.return_value = 0, "UTC"
    java.tables["content://com.android.calendar/events/7"] = [{"dtstart": 0, "dtend": 1800_000, "title": "Call",
                                                               "allDay": 0}]
    values: dict = {}
    cv = java.cls("android.content.ContentValues").return_value
    cv.put.side_effect = lambda k, v: values.__setitem__(k, v)
    cv.size.side_effect = lambda: len(values)
    java.cls("java.lang.Long").side_effect = int
    java.cls("java.lang.Integer").side_effect = int
    java.ctx.getContentResolver.return_value.update.return_value = 1
    assert phone.call("calendar.update", event_id=7, start=3600) == {"event_id": 7, "title": "Call"}
    assert (values["dtstart"], values["dtend"], values["allDay"]) == (3600_000, 5400_000, 0)


# -- place and time ---------------------------------------------------------------

def test_location(phone, java):
    bridge = java.app("LocationBridge")
    bridge.get.return_value = json.dumps({"ok": True, "lat": 1.5, "lon": 2.5, "stale": False})
    assert phone.call("location.get", timeout=3) == {"lat": 1.5, "lon": 2.5, "stale": False}
    assert bridge.get.call_args.args[1] == 3.0
    bridge.get.return_value = json.dumps({"ok": False, "error": "location is off"})
    assert phone.err("location.get") == "location is off"
    phone.call("location.navigate", to="1 Main St, Austin", mode="walk")
    assert java.started[-1].data == ("https://www.google.com/maps/dir/?api=1&destination=1%20Main%20St%2C%20Austin"
                                     "&travelmode=walking&dir_action=navigate")
    phone.call("location.show", to="Zilker Park")
    assert java.started[-1].data == "geo:0,0?q=Zilker%20Park"


def test_clock(phone, java):
    phone.call("clock.timer", seconds=300, label="tea")
    i = java.started[-1]
    assert i.action == "android.intent.action.SET_TIMER"
    assert i.extras == {"android.intent.extra.alarm.LENGTH": ("jint", 300),   # getIntExtra: an int, not a long
                        "android.intent.extra.alarm.SKIP_UI": True, "android.intent.extra.alarm.MESSAGE": "tea"}
    phone.call("clock.alarm", hour=6, minute=30, start=False)
    assert java.started[-1].extras["android.intent.extra.alarm.HOUR"] == ("jint", 6)
    assert java.started[-1].extras["android.intent.extra.alarm.SKIP_UI"] is False
    assert "bad args" in phone.err("clock.alarm", hour=25)
    java.ctx.startActivity.side_effect = type("ActivityNotFoundException", (Exception,), {})
    assert phone.err("clock.alarms") == "no app on this phone handles that"


# -- the phone itself ---------------------------------------------------------------

def test_phone_find_vibrate_torch(phone, java):
    java.app("FindDeviceBridge").ring.return_value = json.dumps({"ok": True, "ringing": True, "seconds": 5})
    assert phone.call("phone.find", seconds=5) == {"ringing": True, "seconds": 5}
    vib = java.service("VIBRATOR_MANAGER_SERVICE").getDefaultVibrator.return_value
    assert phone.call("phone.vibrate", ms=200) == {"ms": 200} and vib.vibrate.called
    cm = java.service("CAMERA_SERVICE")
    cm.getCameraIdList.return_value = ["0", "1"]
    cm.getCameraCharacteristics.side_effect = lambda c: {"0": {}, "1": {"flash": True}}[c]
    java.cls("android.hardware.camera2.CameraCharacteristics").FLASH_INFO_AVAILABLE = "flash"
    assert phone.call("phone.torch", on=False) == {"on": False}
    cm.setTorchMode.assert_called_with("1", False)


def _screen(java, w=1000, h=2000):
    metrics = java.cls("android.util.DisplayMetrics").return_value
    metrics.widthPixels, metrics.heightPixels = w, h
    hid = java.app("HidAccessibilityService")
    hid.isEnabled.return_value = True
    hid.tap.return_value = True
    return hid


def test_phone_unlock_types_the_pin_and_never_says_it(phone, java):
    hid = _screen(java)
    java.app("KeepAwake").keyguardLocked.return_value = False
    got = phone.call("phone.unlock", pin="2580", pre_swipe=False)
    assert got == {"unlocked": True, "pin": "given", "taps_ok": True}
    assert [c.args for c in hid.tap.call_args_list] == [(500, 1282), (500, 1436), (500, 1590), (500, 1744)]
    assert "2580" not in json.dumps(got)
    assert "bad args" in phone.err("phone.unlock", pin="12ab")
    pins = java.app("UnlockPin")
    pins.isSet.return_value = False
    assert "no unlock PIN saved" in phone.err("phone.unlock")
    pins.isSet.return_value = True
    pins.tapStored.return_value = json.dumps({"ok": True, "digits": 6, "taps_ok": True})
    java.app("KeepAwake").keyguardLocked.return_value = True
    got = phone.call("phone.unlock")
    assert got["pin"] == "saved" and got["unlocked"] is False and "still locked" in got["note"]
    pins.noteUnlockResult.assert_called_with(java.ctx, False, False)


def test_screen_driving_caps_wake_the_phone_and_unlock_it(phone, java):
    hid = _screen(java)
    keep = java.app("KeepAwake")
    keep.activity.return_value = json.dumps({"ok": True, "auto": True, "started": True, "woke": True,
                                             "locked": True, "auto_unlock": True})
    keep.screenOn.return_value = True
    keep.keyguardLocked.return_value = False
    pins = java.app("UnlockPin")
    pins.isSet.return_value = True
    pins.tapStored.return_value = json.dumps({"ok": True, "taps_ok": True})
    phone.call("hid-android.hid.click", x=10, y=20)
    assert keep.activity.call_args.args == (java.ctx, "hid.click")
    pins.noteUnlockResult.assert_called_with(java.ctx, True, True)   # automatic, and it worked
    hid.tap.assert_called_with(10, 20)
    keep.activity.side_effect = RuntimeError("bridge gone")           # never fails the cap
    assert phone.call("hid-android.hid.key", key="back") == {}


def test_screen_android(phone, java):
    java.app("ScreenCaptureBridge").captureJpeg.return_value = b"\xff\xd8jpeg"
    got = phone.call("screen-android.screen.capture", quality=50)
    assert got["mime"] == "image/jpeg" and got["bytes"] == 6
    java.app("ScreenCaptureBridge").captureJpeg.assert_called_with(50)
    java.app("ScreenCaptureBridge").captureJpeg.return_value = None
    assert "no screen capture consent" in phone.err("screen-android.screen.capture")
    java.app("KeepAwake").engage.return_value = json.dumps({"ok": True, "on": True, "remaining_s": 600})
    assert phone.call("screen-android.screen.keep_on", on=True, minutes=10) == {"on": True, "remaining_s": 600}
    java.cls("android.provider.Settings").canDrawOverlays.return_value = False
    assert "Display over other apps" in phone.err("screen-android.screen.on")


def test_keep_awake_settings_reach_the_app_only_when_set(phone, java):
    keep = java.app("KeepAwake")
    phone.hook("screen-android", "scripts/screen.py:start")
    assert not keep.configure.called
    java.cls("java.lang.Integer").side_effect = lambda v: ("Integer", v)
    phone.hook("screen-android", "scripts/screen.py:start", settings={"keep_awake_idle_minutes": 5})
    keep.configure.assert_called_with(java.ctx, None, ("Integer", 5))


def test_battery(phone, java):
    bm = java.service("BATTERY_SERVICE")
    bm.getIntProperty.return_value = 81
    bm.isCharging.return_value = False
    batt = java.ctx.registerReceiver.return_value
    BM = java.cls("android.os.BatteryManager")
    extras = {BM.EXTRA_STATUS: 3, BM.EXTRA_PLUGGED: 0, BM.EXTRA_TEMPERATURE: 291, BM.EXTRA_LEVEL: 81,
              BM.EXTRA_SCALE: 100}
    batt.getIntExtra.side_effect = lambda k, d: extras.get(k, d)
    assert phone.call("battery-android.battery.status") == {"percent": 81, "charging": False, "status": "discharging",
                                                            "plugged": False, "temp_c": 29.1}


# -- screen content: ui and hid -----------------------------------------------------

SUMMARY = {"package": "com.app", "items": [{"label": "Send", "kind": "button", "at": [100, 200]}],
           "text": ["Hello"], "rev": "r1"}


def test_ui_screen_fits_the_contract(phone, java):
    hid = _screen(java)
    hid.screenSummary.return_value = json.dumps(SUMMARY)
    hid.screenApp.return_value = ["com.app", "App"]
    got = phone.call("ui-android.ui.screen", limit=5)
    assert got["app"] == "App" and got["package"] == "com.app" and got["rev"] == "r1"
    assert got["items"] == [{"text": "Send", "kind": "button", "x": 100, "y": 200}]
    hid.screenSummary.assert_called_with(5, 40)
    hid.isEnabled.return_value = False
    assert "accessibility service not enabled" in phone.err("ui-android.ui.screen")


def test_ui_click_reports_near_matches_and_can_return_the_screen(phone, java):
    hid = _screen(java)
    hid.screenApp.return_value = ["com.app", "App"]
    hid.clickNode.return_value = json.dumps({"ok": False, "error": "no visible node matches", "near": ["Sent"]})
    assert phone.err("ui-android.ui.click", text="Send") == 'no visible node matches {"near": ["Sent"]}'
    assert "give text, desc or id" in phone.err("ui-android.ui.click")
    hid.clickNode.return_value = json.dumps({"ok": True, "clicked": "Send"})
    revs = iter(["r1", "r2", "r2", "r2"])
    hid.screenRev.side_effect = lambda: next(revs, "r2")
    hid.quietMs.return_value = 900
    hid.screenSummary.return_value = json.dumps(SUMMARY)
    got = phone.call("ui-android.ui.click", text="Send", screen=True)
    assert got["clicked"] == "Send" and got["app"] == "App"
    assert got["screen"]["changed"] is True and got["screen"]["items"][0]["text"] == "Send"
    hid.clickNode.assert_called_with("Send", None, None, False, 0, False)


def test_ui_type_and_wait(phone, java):
    hid = _screen(java)
    hid.typeInto.return_value = json.dumps({"ok": True, "field": "Message"})
    assert phone.call("ui-android.ui.type", text="secret words", label="Message", submit=True)["field"] == "Message"
    hid.typeInto.assert_called_with("Message", None, False, -1, "secret words", False, True)
    hid.screenApp.return_value = ["com.app", "App"]
    hid.matchCount.return_value = 0
    hid.screenSummary.return_value = json.dumps(SUMMARY)
    got = phone.call("ui-android.ui.wait", text="Done", gone=True, screen=False)
    assert got["matches"] == 0 and got["app"] == "App"
    msg = phone.err("ui-android.ui.wait", text="Done", timeout=0.05)
    assert msg.startswith("timed out after") and "App: Send" in msg


def test_hid(phone, java):
    hid = _screen(java)
    assert phone.call("hid-android.hid.status") == {"backend": "android-accessibility", "ready": True}
    assert "needs x and y" in phone.err("hid-android.hid.click")
    assert "only the left button" in phone.err("hid-android.hid.click", x=1, y=1, button="right")
    hid.swipe.return_value = True
    phone.call("hid-android.hid.drag", x1=1, y1=2, x2=3, y2=4, duration="1.5s")
    hid.swipe.assert_called_with(1, 2, 3, 4, 1500)
    assert "only back, home, recents" in phone.err("hid-android.hid.key", key="enter")
    assert "only back, home, recents" in phone.err("hid-android.hid.key", key="back", mods=["ctrl"])
    hid.typeText.return_value = False
    assert "no focused field" in phone.err("hid-android.hid.type", text="hi")


# -- apps, clipboard, sound, media ------------------------------------------------------

def _launcher(java, apps):
    pm = java.ctx.getPackageManager.return_value
    found = pm.queryIntentActivities.return_value
    infos = []
    for label, pkg in apps:
        info = type("Info", (), {})()
        info.activityInfo = type("A", (), {"packageName": pkg, "loadLabel": lambda self, pm, l=label: l})()
        infos.append(info)
    found.size.return_value = len(infos)
    found.get.side_effect = infos.__getitem__
    return pm


def test_app_launch_by_name(phone, java):
    pm = _launcher(java, [("Maps", "com.google.maps"), ("Messages", "com.msg"), ("Mail", "com.mail")])
    pm.getLaunchIntentForPackage.side_effect = lambda p: Intent("MAIN") if p != "com.none" else None
    pm.resolveActivity.return_value = None
    assert phone.call("app-android.app.launch", app="maps") == {"package": "com.google.maps"}
    assert "no single app is called 'm'" in phone.err("app-android.app.launch", app="m")
    assert phone.call("app-android.app.launch", app="com.mail", params={"q": "x", "n": 2})["package"] == "com.mail"
    assert java.started[-1].extras == {"q": "x", "n": ("jint", 2)}
    assert "params.f" in phone.err("app-android.app.launch", app="com.mail", params={"f": 1.5})


def test_app_open_url_says_when_android_may_hide_it(phone, java):
    java.cls("android.provider.Settings").canDrawOverlays.return_value = False
    java.app("HidAccessibilityService").isEnabled.return_value = False
    got = phone.call("app-android.app.open_url", url="tel:5550100")
    assert got["may_be_blocked"] is True and java.started[-1].data == "tel:5550100"
    got = phone.call("app-android.app.open_url", url="https://x.test", screen=True)
    assert got["screen"] == {"error": "accessibility service not enabled (allow it in the Rook app)"}
    assert "needs package" in phone.err("app-android.app.settings", page="app")
    phone.call("app-android.app.settings", page="app", package="com.x")
    assert java.started[-1].action == "android.settings.APPLICATION_DETAILS_SETTINGS"
    assert java.started[-1].data == "package:com.x"


def test_clipboard(phone, java):
    cm = java.service("CLIPBOARD_SERVICE")
    cm.getPrimaryClip.return_value = None
    assert phone.call("clipboard-android.clipboard.get")["text"] is None
    assert phone.call("clipboard-android.clipboard.set", text="hi") == {"chars": 2}


def test_sound(phone, java):
    am, nm = java.service("AUDIO_SERVICE"), java.service("NOTIFICATION_SERVICE")
    am.getStreamMaxVolume.return_value, am.getStreamMinVolume.return_value = 15, 0
    am.getStreamVolume.return_value, am.getRingerMode.return_value = 5, 2
    nm.getCurrentInterruptionFilter.return_value, nm.isNotificationPolicyAccessGranted.return_value = 1, False
    got = phone.call("sound-android.sound.set", level=40, step=-2)
    am.setStreamVolume.assert_called_with(3, 6, 0)
    assert [c.args for c in am.adjustStreamVolume.call_args_list] == [(3, -1, 0), (3, -1, 0)]
    assert got["ringer"] == "normal" and got["dnd"] == "off" and got["streams"]["media"]["max"] == 15
    assert "give level, step, mute or ringer" in phone.err("sound-android.sound.set")
    nm.setInterruptionFilter.side_effect = type("SecurityException", (Exception,), {})("denied")
    assert "Do Not Disturb access" in phone.err("sound-android.sound.dnd", mode="total")


def test_media(phone, java):
    def session(pkg, state, pos=10_000):
        c = java.cls(f"controller-{pkg}")
        c.getPackageName.return_value = pkg
        c.getPlaybackState.return_value.getState.return_value = state
        c.getPlaybackState.return_value.getPosition.return_value = pos
        c.getMetadata.return_value = None
        return c
    paused, playing = session("com.pod", 2), session("com.music", 3)
    found = java.service("MEDIA_SESSION_SERVICE").getActiveSessions.return_value
    found.size.return_value = 2
    found.get.side_effect = [paused, playing].__getitem__
    java.ctx.getPackageManager.return_value.getApplicationLabel.side_effect = lambda info: "X"
    got = phone.call("media-android.media.pause")
    assert got["via"] == "session" and got["was"] == "playing"
    got = phone.call("media-android.media.seek", by=-5, package="com.pod")
    paused.getTransportControls.return_value.seekTo.assert_called_with(5000)
    java.service("MEDIA_SESSION_SERVICE").getActiveSessions.side_effect = type("SecurityException", (Exception,), {})
    assert phone.call("media-android.media.next") == {"via": "key"}
    assert "notification access" in phone.err("media-android.media.play", package="com.pod")


# -- notifications ---------------------------------------------------------------------

def test_notification_post_plain_and_card(phone, java):
    got = phone.call("notification-android.notification.post", text="Build done", channel="digest")
    assert got["channel"] == "rook_digest"
    java.app("AttentionChannels").ensure.assert_called_with(java.ctx)
    store = java.app("AttentionStore")
    store.post.return_value = json.dumps({"ok": True, "id": "q1", "posted": True})
    assert phone.call("notification-android.notification.post", id="q1", text="Deploy 1234?",
                      body="For: the hub\n\n\nRisk: restart", options=["Now", "Tonight"],
                      actions=["choose", "explain"]) == {"id": "q1", "posted": True}
    card = json.loads(store.post.call_args.args[1])
    assert card["line"] == "Deploy 1234?" and card["body"] == "For: the hub\nRisk: restart"
    assert card["actions"] == ["choose", "explain"] and card["channel"] == "approvals"
    assert "go together" in phone.err("notification-android.notification.post", id="q2", text="?",
                                      options=["a"])
    assert "different" in phone.err("notification-android.notification.post", id="q2", text="?",
                                    options=["a", "a"], actions=["choose"])


LISTENED = [{"key": "k2", "package": "com.chat", "title": "Ann", "text": "on my way", "ts": 20.0, "posted_ms": 20_000},
            {"key": "k1", "package": "systems.bake.rook", "title": "Rook", "text": "own", "ts": 15.0,
             "posted_ms": 15_000},
            {"key": "k0", "package": "com.mail", "title": "Old", "text": "old", "ts": 5.0, "posted_ms": 5_000}]


def test_notification_reading_and_acting(phone, java):
    lst = java.app("RookNotificationListener")
    lst.isEnabled.return_value = False
    assert "notification access" in phone.err("notification-android.notification.list")
    lst.isEnabled.return_value = True
    lst.snapshotJson.return_value = json.dumps(LISTENED)
    got = phone.call("notification-android.notification.list", packages=["com.mail"])
    assert [n["key"] for n in got["notifications"]] == ["k0"]
    lst.actionsJson.return_value = json.dumps({"ok": True, "key": "k2", "actions": [{"index": 1, "title": "Reply",
                                                                                     "takes_text": True}]})
    assert phone.call("notification-android.notification.get", key="k2")["notification"]["title"] == "Ann"
    assert "Let the assistant act" in phone.err("notification-android.notification.act", key="k2", action="Reply")
    java.prefs["assistant_actions"] = True
    lst.act.return_value = json.dumps({"ok": True, "action": "Reply", "app": "com.chat"})
    assert phone.call("notification-android.notification.act", key="k2", action="reply", text="ok") == \
        {"action": "Reply", "app": "com.chat"}
    lst.act.assert_called_with(java.ctx, "k2", 1, "ok")
    assert "it has: Reply" in phone.err("notification-android.notification.act", key="k2", action="Archive")


def test_notification_watch_announces_new_ones_but_not_its_own(phone, java):
    lst = java.app("RookNotificationListener")
    lst.isEnabled.return_value = True
    lst.snapshotJson.return_value = json.dumps(LISTENED[2:])
    phone.hook("notification-android", "scripts/notification.py:watch")
    assert phone.events == []                                    # first look: history isn't news
    lst.snapshotJson.return_value = json.dumps(LISTENED)
    phone.hook("notification-android", "scripts/notification.py:watch")
    assert phone.events == [("notification-android.received", {"key": "k2", "package": "com.chat", "title": "Ann",
                                                                "text": "on my way", "ts": 20.0})]
    phone.hook("notification-android", "scripts/notification.py:watch")
    assert len(phone.events) == 1


def test_card_answers_become_events(phone, java):
    store = java.app("AttentionStore")
    store.poll.return_value = json.dumps({"ok": True, "events": [{"seq": 3, "type": "decision", "id": "old"}],
                                          "last": 3})
    phone.hook("notification-android", "scripts/notification.py:watch_cards")
    assert phone.events == []
    pages = iter([{"ok": True, "events": [{"seq": 4, "type": "decision", "id": "q1", "decision": "choose",
                                           "choice": "Tonight", "ts": 9.0},
                                          {"seq": 5, "type": "notice", "id": "n", "summary": "x"}],
                   "last": 5, "more": True},
                  {"ok": True, "events": [{"seq": 6, "type": "acked", "id": "a1", "ts": 10.0}], "last": 6}])
    store.poll.side_effect = lambda ctx, since: json.dumps(next(pages))
    phone.hook("notification-android", "scripts/notification.py:watch_cards")
    assert phone.events == [("notification-android.card", {"id": "q1", "type": "decision", "decision": "choose",
                                                            "choice": "Tonight", "ts": 9.0}),
                            ("notification-android.card", {"id": "a1", "type": "acked", "ts": 10.0})]
    assert [c.args[1] for c in store.poll.call_args_list[-2:]] == [3, 5]


# -- voice --------------------------------------------------------------------------

def test_voice_split_text():
    split = script("voice-android", "voice.py")["split_text"]
    chunks = split("One sentence here. " * 40, 100)
    assert all(len(c) <= 100 for c in chunks) and all(c.endswith(".") for c in chunks)
    assert split("   ") == []


def test_voice_speak_and_ask(phone, java):
    b = java.app("SpeakBridge")
    b.speak.side_effect = lambda *a: json.dumps({"ok": True, "id": f"s{b.speak.call_count}", "state": "queued"})
    b.speakReply.return_value = json.dumps({"ok": True, "id": "r1"})
    b.status.side_effect = lambda i: json.dumps({"ok": True, "state": "done", "done": True, "via": "server",
                                                 **({"reply_state": "received", "reply": {"text": "yes"}}
                                                    if i == "r1" else {})})
    got = phone.call("voice-android.voice.speak", text="Short line.", interrupt=True)
    assert got["spoken"] is True and got["via"] == "server" and got["chunks"] == 1
    assert b.speak.call_args.args[1:] == ("Short line.", "", 1.0, 1.0, True)
    b.chat.assert_called_with("Short line.")
    got = phone.call("voice-android.voice.ask", text="Ship it?", chat=False)
    assert got["reply"] == {"text": "yes"} and got["reply_state"] == "received"
    assert b.speakReply.call_args.args[-1] == 8
    assert phone.call("voice-android.voice.speak", text="later", wait=False)["state"] == "queued"
    b.speak.side_effect = None
    b.speak.return_value = json.dumps({"ok": False, "error": "no engine"})
    assert phone.err("voice-android.voice.speak", text="x") == "no engine"


def test_voice_waits_no_longer_than_timeout(phone, java, monkeypatch):
    b = java.app("SpeakBridge")
    b.speak.return_value = json.dumps({"ok": True, "id": "s1"})
    b.status.return_value = json.dumps({"ok": True, "state": "speaking", "done": False})
    got = phone.call("voice-android.voice.speak", text="long", timeout=1)
    assert got["timed_out"] is True and got["spoken"] is False
