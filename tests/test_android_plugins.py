"""The Android caps' built-in plugins (brief AP2): clipboard-android, phone, sound-android, app-android,
and battery's Android path. On the Rook Android app they run under Chaquopy, whose `java` module reaches
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
    java.jclass = java.cast = java.jint = lambda *a: pytest.fail(f"reached Android with {a}")
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
