"""The Android caps' built-in plugins: clipboard-android, phone, sound-android, app-android and battery's
Android path (brief AP2); sms, call, contact, calendar, clock and location (brief AP3). On the Rook Android app they run under Chaquopy, whose `java` module reaches
Android; that is proved on an emulator (the pull request). Here: off the app they announce nothing and
break nothing, on a real worker over the real carrier; with a `java` module present they announce
0.5's cap names, each with a one-line description and a schema; and what they check before reaching
Android refuses as it should."""

import asyncio
import sys
import types

import pytest

from rook_hub.plugins import DEFAULTS
from test_default_plugins import Rig

ANDROID = {
    "app-android": ["app.launch", "app.list", "app.open_url", "app.settings", "app.share"],
    "clipboard-android": ["clipboard.get", "clipboard.set"],
    "phone": ["phone.find", "phone.find_stop", "phone.torch", "phone.vibrate"],
    "sound-android": ["sound.dnd", "sound.set", "sound.status"],
    "sms": ["sms.list", "sms.send"],
    "call": ["call.dial", "call.list"],
    "contact": ["contact.search"],
    "calendar": ["calendar.create", "calendar.delete", "calendar.list", "calendar.update"],
    "clock": ["clock.alarm", "clock.alarms", "clock.timer"],
    "location": ["location.get", "location.navigate", "location.show"],
}


def part(name, java=None):
    """A plugin's worker part, run as a worker runs it, with `java` as Chaquopy's module (None: absent)."""
    saved = sys.modules.pop("java", None)
    if java is not None:
        sys.modules["java"] = java
    try:
        namespace = {}
        exec(compile((DEFAULTS / name / "worker.py").read_text(), name, "exec"), namespace)
        return namespace
    finally:
        sys.modules.pop("java", None)
        if saved is not None:
            sys.modules["java"] = saved


def fake_java():
    """A stand-in for Chaquopy's `java` module: enough to import, nothing behind it."""
    java = types.ModuleType("java")
    java.jclass = java.cast = java.jint = java.dynamic_proxy = lambda *a: pytest.fail(f"reached Android with {a}")
    return java


def test_off_the_app_they_announce_nothing():
    for name in ANDROID:
        assert part(name)["CAPS"] == {}, name
    assert sorted(part("battery")["CAPS"]) == ["battery.status"]      # Linux's battery is still there


def test_on_the_app_they_announce_0_5s_caps_each_with_a_one_line_description_and_a_schema():
    for name, caps in ANDROID.items():
        declared = part(name, fake_java())["CAPS"]
        assert sorted(declared) == caps, name
        for cap in declared.values():
            assert cap.description and "\n" not in cap.description
            assert cap.schema["type"] == "object" and cap.schema.get("additionalProperties") is False


def test_a_linux_worker_loads_them_announces_none_of_their_caps_and_serves_the_rest(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            loaded = {p["name"]: p for p in rig.hub.get_worker(rig.guid)["plugins"]}
            assert all(name in loaded and "error" not in loaded[name] for name in ANDROID), loaded
            announced = set(rig.announced())
            assert not announced & {cap for caps in ANDROID.values() for cap in caps}
            assert (await rig.ok("worker.ping"))["pong"] is True
    asyncio.run(go())


def refusal(name, cap, args):
    """What a cap says to `args` it refuses before reaching Android."""
    handler = part(name, fake_java())["CAPS"][cap].handler
    with pytest.raises(ValueError) as refused:
        asyncio.run(handler(args))
    return str(refused.value)


def test_what_they_refuse_before_reaching_android():
    assert "takes no argument seconds; it takes none" in refusal("phone", "phone.find_stop", {"seconds": 1})
    assert "1 or more" in refusal("phone", "phone.vibrate", {"ms": 0})
    assert "1 or more" in refusal("phone", "phone.find", {"seconds": 0})
    assert "on must be true or false" in refusal("phone", "phone.torch", {"on": "yes"})
    assert "give level, step, mute or ringer" in refusal("sound-android", "sound.set", {})
    assert "from 0 to 100" in refusal("sound-android", "sound.set", {"level": 101})
    assert "ringer must be one of" in refusal("sound-android", "sound.set", {"ringer": "loud"})
    assert "sound.dnd needs mode" in refusal("sound-android", "sound.dnd", {})
    assert "text must be a string" in refusal("clipboard-android", "clipboard.set", {"text": 3})
    assert "page must be one of" in refusal("app-android", "app.settings", {"page": "kitchen"})
    assert "app.launch needs app" in refusal("app-android", "app.launch", {})
    assert "params.n: a string" in refusal("app-android", "app.launch", {"app": "Maps", "params": {"n": 1.5}})
    assert "limit must be a whole number" in refusal("app-android", "app.list", {"limit": 0})


def test_no_bound_is_kept_from_0_5():
    """0.5 capped find at 120 s, vibrate at 5000 ms and step at 30, rang 30 s without seconds, and cut
    app.list at 200 (Bake, 17 November: none of that). vibrate without ms is 0.5's 300 ms (PR 465)."""
    schemas = {cap: c.schema for name in ANDROID for cap, c in part(name, fake_java())["CAPS"].items()}
    assert "maximum" not in schemas["phone.find"]["properties"]["seconds"]
    assert "default" not in schemas["phone.find"]["properties"]["seconds"]
    assert "maximum" not in schemas["phone.vibrate"]["properties"]["ms"]
    assert schemas["phone.vibrate"]["properties"]["ms"]["default"] == 300 and "required" not in schemas["phone.vibrate"]
    assert "minimum" not in schemas["sound.set"]["properties"]["step"]
    assert "default" not in schemas["app.list"]["properties"]["limit"]
    pick = part("app-android", fake_java())
    apps = [{"label": f"App {n}", "package": f"com.example.app{n}"} for n in range(500)]
    pick["launcher_apps"] = lambda: apps
    assert pick["_list"]("", None) == {"apps": apps, "truncated": False}
    assert pick["_list"]("", 3) == {"apps": apps[:3], "truncated": True}


def test_picking_an_app_and_a_volume_level():
    pick_app = part("app-android", fake_java())["pick_app"]
    apps = [{"label": "Maps", "package": "com.google.android.apps.maps"},
            {"label": "Messages", "package": "com.google.android.apps.messaging"},
            {"label": "Settings", "package": "com.android.settings"}]
    assert pick_app(apps, "maps")[0]["label"] == "Maps"
    assert pick_app(apps, "com.android.settings")[0]["label"] == "Settings"
    assert pick_app(apps, "m")[0] is None and len(pick_app(apps, "m")[1]) == 2
    assert pick_app(apps, "zzz") == (None, [])
    level_index = part("sound-android", fake_java())["level_index"]
    assert (level_index(0, 1, 15), level_index(50, 0, 15), level_index(100, 0, 15)) == (1, 8, 15)


def test_vibrate_without_ms_buzzes_300_ms_and_any_ms_given_as_is():
    """0.5's default (the steward's decision on pull request 465); no upper bound."""
    phone = part("phone", fake_java())
    phone["_vibrate"] = lambda ms: {"ms": ms}           # what reaches Android, without Android
    vibrate = phone["CAPS"]["phone.vibrate"].handler
    assert asyncio.run(vibrate({})) == {"ms": 300}
    assert asyncio.run(vibrate({"ms": 60000})) == {"ms": 60000}


def test_what_the_people_and_time_caps_refuse_before_reaching_android():
    assert "sms.send needs text" in refusal("sms", "sms.send", {"to": "5551234"})
    assert "to must be a phone number" in refusal("sms", "sms.send", {"to": "call me", "text": "hi"})
    assert "box must be one of" in refusal("sms", "sms.list", {"box": "trash"})
    assert "since must be epoch seconds" in refusal("sms", "sms.list", {"since": -1})
    assert "to must be a phone number" in refusal("call", "call.dial", {"to": "555; rm"})
    assert "limit must be a whole number" in refusal("call", "call.list", {"limit": 0})
    assert "query must be a string" in refusal("contact", "contact.search", {"query": 5})
    assert "calendar.create needs start" in refusal("calendar", "calendar.create", {"title": "x"})
    assert "event_id must be a whole number" in refusal("calendar", "calendar.delete", {"event_id": "1"})
    assert "takes no argument color" in refusal("calendar", "calendar.update", {"event_id": 1, "color": "red"})
    assert "seconds must be a whole number from 1" in refusal("clock", "clock.timer", {"seconds": 0})
    assert "hour must be a whole number from 0 to 23" in refusal("clock", "clock.alarm", {"hour": 24})
    assert "mode must be one of" in refusal("location", "location.navigate", {"to": "home", "mode": "fly"})
    assert "timeout must be a number of seconds above 0" in refusal("location", "location.get", {"timeout": 0})


def test_no_bound_of_the_people_and_time_caps_is_kept_from_0_5():
    """0.5 cut sms.list at 30, call.list at 30, contact.search at 40 and calendar.list at 20 by default,
    capped those limits, a timer at a day, location.get's wait at 25 s (8 by default), and lengths of
    texts and numbers (Bake, 17 November: none of that). A long text, a long number and a day-long timer
    pass the checks made before reaching Android."""
    schemas = {cap: c.schema for name in ANDROID for cap, c in part(name, fake_java())["CAPS"].items()}
    for cap in ("sms.list", "call.list", "contact.search", "calendar.list"):
        assert schemas[cap]["properties"]["limit"] == {"type": "integer", "minimum": 1}, cap
    assert "maximum" not in schemas["clock.timer"]["properties"]["seconds"]
    assert schemas["location.get"]["properties"]["timeout"].get("maximum") is None
    assert "default" not in schemas["location.get"]["properties"]["timeout"]
    for cap, field in (("sms.send", "text"), ("calendar.create", "title"), ("location.navigate", "to")):
        assert "maxLength" not in schemas[cap]["properties"][field], cap
    sms = part("sms", fake_java())
    sms["_send"] = lambda to, text: {"to": to, "parts": len(text)}
    assert asyncio.run(sms["CAPS"]["sms.send"].handler({"to": "+1 (555) 010-1815 / 22 22 22 22 22 22",
                                                        "text": "x" * 100000}))["parts"] == 100000
    clock = part("clock", fake_java())
    clock["_go"] = lambda cap, action, extras=None: extras
    assert asyncio.run(clock["CAPS"]["clock.timer"].handler({"seconds": 7 * 86400}))[
        "android.intent.extra.alarm.LENGTH"] == 7 * 86400


def test_calendar_times_are_phone_local_and_all_day_events_dates():
    """calendar's own time handling, with the phone six hours behind UTC (as the emulator was)."""
    calendar = part("calendar", fake_java())
    central = lambda ms: -6 * 3600 * 1000
    parse, iso = calendar["parse_time"], calendar["iso_local"]
    assert parse("2026-11-27T10:00", 0, central) == parse("2026-11-27T16:00Z", 0, central)
    assert parse(1795795200, 0, central) == 1795795200000 and parse(1795795200000, 0, central) == 1795795200000
    assert parse(None, 42, central) == 42 and parse("", 42, central) == 42
    assert iso(1795795200000, central) == "2026-11-27T10:00-06:00"
    with pytest.raises(ValueError, match="not a time"):
        parse("next tuesday", 0, central)
    day = 24 * 3600 * 1000
    rows = [{"title": "late", "begin": 1795795200000 + 8 * 3600 * 1000, "end": 1795795200000 + 9 * 3600 * 1000, "allDay": 0},
            {"title": "holiday", "begin": 1795737600000, "end": 1795737600000 + day, "allDay": 1},   # 2026-11-27, UTC midnight
            {"title": "early", "begin": 1795795200000, "end": 1795798800000, "allDay": 0},
            {"title": "yesterday", "begin": 1795795200000 - day, "end": 1795798800000 - day, "allDay": 0}]
    start, end = parse("2026-11-27T00:00", 0, central), parse("2026-11-28T00:00", 0, central)
    picked = calendar["select_rows"](rows, start, end, None, central)
    assert [r["title"] for r in picked] == ["holiday", "early", "late"]           # all of them: no default cut
    assert [r["title"] for r in calendar["select_rows"](rows, start, end, 1, central)] == ["holiday"]
    assert calendar["event_row"](rows[1], central)["start"] == "2026-11-27" == calendar["event_row"](rows[1], central)["end"]
