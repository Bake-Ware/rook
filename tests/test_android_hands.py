"""The phone's screen, input and automation caps (brief AP5): hid-android, ui-android, screen-android and
phone.unlock. On the Rook Android app they run under Chaquopy and reach the app's accessibility service
(Hands.kt) and its Bridge; that is proved on an emulator (the pull request), and the screen model they
read through is tested on the JVM (android/worker/src/test, ScreenTest.kt). Here: off the app they
announce nothing; with a `java` module present they announce 0.5's names, each with a one-line
description and a schema; a real Linux worker loads them with no error and announces none of their
caps; what they refuse before reaching Android; that none of 0.5's bounds is kept (ui.wait waits as long
as asked, and with no time given until it is true); which way screen.capture takes the picture; and
where phone.unlock's PIN comes from."""

import asyncio
import base64
import json
import sys
import time
import types

import pytest

from test_android_plugins import fake_java, part
from test_default_plugins import Rig

HANDS = {
    "hid-android": ["hid.click", "hid.drag", "hid.key", "hid.status", "hid.type"],
    "screen-android": ["screen.capture", "screen.keep_on", "screen.on", "screen.status"],
    "ui-android": ["ui.click", "ui.screen", "ui.scroll", "ui.type", "ui.wait"],
}


def test_off_the_app_they_announce_nothing():
    for name in [*HANDS, "phone"]:
        assert part(name)["CAPS"] == {}, name


def test_on_the_app_they_announce_0_5s_caps_each_with_a_one_line_description_and_a_schema():
    for name, caps in HANDS.items():
        declared = part(name, fake_java())["CAPS"]
        assert sorted(declared) == caps, name
        for cap in declared.values():
            assert cap.description and "\n" not in cap.description
            assert cap.schema["type"] == "object" and cap.schema.get("additionalProperties") is False
    phone = part("phone", fake_java())["CAPS"]
    assert sorted(phone) == ["phone.find", "phone.find_stop", "phone.torch", "phone.unlock", "phone.vibrate"]
    assert phone["phone.unlock"].schema["properties"]["pin"]["type"] == "string"


def test_a_linux_worker_loads_them_announces_none_of_their_caps_and_serves_the_rest(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            loaded = {p["name"]: p for p in rig.hub.get_worker(rig.guid)["plugins"]}
            assert all(name in loaded and "error" not in loaded[name] for name in [*HANDS, "phone"]), loaded
            assert not set(rig.announced()) & ({cap for caps in HANDS.values() for cap in caps} | {"phone.unlock"})
            assert (await rig.ok("worker.ping"))["pong"] is True
    asyncio.run(go())


def refusal(name, cap, args):
    """What a cap says to `args` it refuses before reaching Android."""
    handler = part(name, fake_java())["CAPS"][cap].handler
    with pytest.raises(ValueError) as refused:
        asyncio.run(handler(args))
    return str(refused.value)


def test_what_they_refuse_before_reaching_android():
    assert "a touch screen needs x and y" in refusal("hid-android", "hid.click", {"x": 3})
    assert "only the left button" in refusal("hid-android", "hid.click", {"x": 3, "y": 4, "button": "right"})
    assert "0 or more" in refusal("hid-android", "hid.click", {"x": -1, "y": 4})
    assert "presses only back, home, recents" in refusal("hid-android", "hid.key", {"key": "enter"})
    assert "no modifiers" in refusal("hid-android", "hid.key", {"key": "back", "mods": ["ctrl"]})
    assert "more than 0" in refusal("hid-android", "hid.drag", {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "duration": 0})
    assert "not a duration" in refusal("hid-android", "hid.drag", {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "duration": "soon"})
    assert "hid.type needs text" in refusal("hid-android", "hid.type", {})
    assert "give text, desc or id" in refusal("ui-android", "ui.click", {"text": "  "})
    assert "direction must be down or up" in refusal("ui-android", "ui.scroll", {"direction": "left"})
    assert "text must be a string" in refusal("ui-android", "ui.type", {"text": 3})
    assert "number of seconds, 0 or more" in refusal("ui-android", "ui.wait", {"timeout": -1})
    assert "takes no argument max" in refusal("ui-android", "ui.screen", {"max": 3})
    assert "screen.keep_on needs on" in refusal("screen-android", "screen.keep_on", {})
    assert "quality must be a whole number" in refusal("screen-android", "screen.capture", {"quality": "high"})
    assert "pin must be the PIN's digits" in refusal("phone", "phone.unlock", {"pin": "12a4"})


def test_a_duration_is_0_5s_forms():
    seconds = part("hid-android", fake_java())["seconds"]
    assert (seconds("300ms"), seconds("1.5s"), seconds("1m30s"), seconds(2)) == (0.3, 1.5, 90.0, 2.0)


def test_no_bound_is_kept_from_0_5():
    """0.5 cut ui.screen at 60 items and 40 lines (300 at most), waited 8 s (60 at most), capped wait, settle_ms,
    text, pixels, swipe lengths, keep-on minutes, hold_ms and quality, and gave PINs a 4-to-16 pattern (Bake,
    17 November: none of that)."""
    schemas = {cap: c.schema["properties"] for name in [*HANDS, "phone"] for cap, c in part(name, fake_java())["CAPS"].items()}
    for cap, arg in [("ui.screen", "limit"), ("ui.screen", "text_limit"), ("ui.wait", "timeout"),
                     ("screen.keep_on", "minutes"), ("screen.on", "hold_ms"), ("phone.unlock", "hold_ms")]:
        assert "default" not in schemas[cap][arg] and "maximum" not in schemas[cap][arg], (cap, arg)
    for cap, arg in [("ui.click", "wait"), ("ui.type", "wait"), ("ui.wait", "settle_ms"), ("hid.click", "x"),
                     ("hid.drag", "x2"), ("screen.capture", "quality")]:
        assert "maximum" not in schemas[cap][arg], (cap, arg)
    assert "maxLength" not in schemas["hid.type"]["text"] and "maxLength" not in schemas["ui.type"]["text"]
    assert "pattern" not in schemas["phone.unlock"]["pin"]


class Phone:
    """Stand-ins for the app's Bridge and Hands, enough for the Python side of the caps."""

    def __init__(self, appears_after=None, sdk=35, hands_on=True):
        self.count, self.appears_after, self.waits = 0, appears_after, []
        self.sdk, self.hands_on, self.asked = sdk, hands_on, []

    # Bridge
    def hands(self, cap):
        self.asked.append(("hands", cap))
        return None

    def awake(self, cap):
        self.asked.append(("awake", cap))

    def capture(self, cap):
        self.asked.append(("capture", cap))
        return None

    # Hands
    def on(self):
        return self.hands_on

    def changes(self):
        return self.count

    def awaitChange(self, seen, ms):
        self.waits.append(ms)
        if ms:
            time.sleep(ms / 1000)
        else:
            self.count += 1         # a change on screen
        return self.count

    def shown(self):
        return True

    def matchCount(self, text, desc, id, contains):
        return int(self.appears_after is not None and self.count >= self.appears_after)

    def quietMs(self):
        return 10 ** 6

    def front(self):
        return json.dumps({"package": "com.google.android.deskclock", "app": "Clock"})

    def rev(self):
        return "0123456789ab"

    def screen(self, limit, text_limit):
        self.asked.append(("screen", limit, text_limit))
        return json.dumps({"app": "Clock", "package": "com.google.android.deskclock",
                           "items": [{"text": "Stopwatch", "kind": "button", "x": 1, "y": 2}], "text": [], "rev": "r"})

    def screenshot(self, quality):
        return b"\xff\xd8 picture taken by accessibility"

    def jpeg(self, quality):
        return b"\xff\xd8 picture taken by media projection"


def on_the_phone(phone):
    java = types.ModuleType("java")
    version = types.SimpleNamespace(SDK_INT=phone.sdk)
    java.jclass = lambda name: version if name == "android.os.Build$VERSION" else phone
    java.cast = java.jint = lambda *a: pytest.fail(f"reached Android with {a}")
    return java


def run(name, cap, args, phone):
    return asyncio.run(part(name, on_the_phone(phone))["CAPS"][cap].handler(args))


def test_ui_wait_with_no_timeout_waits_until_it_is_true():
    phone = Phone(appears_after=200)
    out = run("ui-android", "ui.wait", {"text": "Stopwatch", "screen": False}, phone)
    assert out["matches"] == 1 and out["app"] == "Clock"
    assert len(phone.waits) == 200 and set(phone.waits) == {0}     # each wait was "until the screen changes"


def test_ui_wait_with_a_timeout_stops_then_with_the_screen_as_it_is():
    started = time.monotonic()
    with pytest.raises(ValueError) as late:
        run("ui-android", "ui.wait", {"text": "Never", "timeout": 0.2}, Phone())
    assert time.monotonic() - started >= 0.2
    assert "timed out after" in str(late.value) and "Clock: Stopwatch" in str(late.value)


def test_ui_screen_gives_everything_unless_asked():
    phone = Phone()
    assert run("ui-android", "ui.screen", {}, phone)["items"][0]["text"] == "Stopwatch"
    run("ui-android", "ui.screen", {"limit": 5, "text_limit": 0}, phone)
    assert [a for a in phone.asked if a[0] == "screen"] == [("screen", -1, -1), ("screen", 5, 0)]
    assert ("hands", "ui.screen") in phone.asked and ("awake", "ui.screen") in phone.asked


def test_screen_capture_takes_the_accessibility_screenshot_where_it_can_else_android_s_capture():
    on = run("screen-android", "screen.capture", {}, Phone())
    assert on["via"] == "accessibility" and on["mime"] == "image/jpeg" and on["name"] == "screen.jpg"
    assert base64.b64decode(on["data"]) == b"\xff\xd8 picture taken by accessibility" and on["bytes"] == len(base64.b64decode(on["data"]))
    for phone in (Phone(hands_on=False), Phone(sdk=29)):          # the service off, or Android 10 (no takeScreenshot)
        off = run("screen-android", "screen.capture", {"quality": 50}, phone)
        assert off["via"] == "media_projection" and ("capture", "screen.capture") in phone.asked
        assert base64.b64decode(off["data"]) == b"\xff\xd8 picture taken by media projection"


def test_phone_unlock_takes_the_pin_given_else_the_workers_config_and_never_says_it():
    phone = part("phone", fake_java())
    assert phone["_pin"]("0042") == ("0042", "given")
    assert phone["_pin"](None) == (None, None)                     # no WORKER: not on a worker
    phone["WORKER"] = types.SimpleNamespace(config={"unlock_pin": "1234"})
    assert phone["_pin"](None) == ("1234", "saved")
    phone["WORKER"] = types.SimpleNamespace(config={"unlock_pin": 1234})
    with pytest.raises(ValueError) as bad:
        phone["_pin"](None)
    assert "1234" not in str(bad.value)
    assert "secret:<name>" in phone["NO_PIN"]
