"""adb (Android over adb) on the hub, for a delegate worker that serves only
shell.exec, with a fake ``adb`` on its PATH that answers like a real one for
one attached serial."""

import base64
import io

import pytest
from PIL import Image

from port_helpers import PLUGINS, on_worker
from rook.kit.ctx import KitError
from rook.kit.manifest import load


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


SERIAL = "R52N30ABCDE"
PNG = _png(2400, 1080)
DUMP = (
    '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
    '<node package="com.android.settings" text="" bounds="[0,0][1080,2400]">'
    '<node text="Wi-Fi" clickable="true" bounds="[0,400][1080,500]"/>'
    '<node text="" content-desc="Navigate up" clickable="true" bounds="[0,100][100,200]"/>'
    '<node text="Bluetooth" bounds="[0,600][1080,700]"/>'
    '<node text="" bounds="[0,800][10,810]"/>'
    '</node></hierarchy>')
BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 87
  temperature: 251
"""

# The fake adb: argv[0:2] == ["-s", serial] for device commands.
FAKE_ADB = f"""
import sys, time
SERIAL, PNG64, DUMP, BATTERY = {SERIAL!r}, {base64.encodebytes(PNG)!r}, {DUMP!r}, {BATTERY!r}
out = sys.stdout.buffer
if argv[:2] == ["devices", "-l"]:
    print("List of devices attached")
    print(SERIAL + "          device usb:1-1 product:a52 model:SM_A525F device:a52q transport_id:3")
    print("emulator-5554          unauthorized transport_id:4")
    sys.exit(0)
assert argv[0] == "-s", argv
serial, cmd, rest = argv[1], argv[2], argv[3:]
if serial == "UNAUTH":
    sys.stderr.write("error: device unauthorized.\\n"); sys.exit(1)
if serial != SERIAL:
    sys.stderr.write(f"error: device '{{serial}}' not found\\n"); sys.exit(1)
if cmd == "get-state":
    print("device"); sys.exit(0)
if cmd == "exec-out" and rest == ["screencap -p | base64"]:
    out.write(PNG64); sys.exit(0)
if cmd == "exec-out" and rest[:2] == ["uiautomator", "dump"]:
    out.write(DUMP.encode()); sys.exit(0)
if cmd == "shell" and rest[:2] == ["-T", "--"]:
    remote = rest[2]
    if remote == "dumpsys battery":
        out.write(BATTERY.encode()); sys.exit(0)
    if remote.startswith("sleep"):
        time.sleep(30)
    if remote == "flood":
        out.write(b"y" * (3 << 20)); sys.exit(0)
    if remote == "gone":
        sys.stderr.write("adb: device 'x' not found\\n"); sys.exit(1)
    if remote.startswith("input") and "fail" in remote:
        sys.stderr.write("Error: bad input\\n"); sys.exit(2)
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    out.write(("ran:" + remote + "|in:" + data).encode())
    sys.exit(3 if remote.endswith("exit 3") else 0)
sys.stderr.write("fake adb: unexpected " + repr(argv) + "\\n"); sys.exit(99)
"""

@pytest.fixture
def adb(bins):
    bins.add("adb", FAKE_ADB)
    bins.only_these()
    return bins


def call(cap, args=None, instance=None):
    """``cap`` called on the appliance: the script on the hub, adb on its delegate."""
    inst = {"name": "playground-tablet", "settings": {"serial": SERIAL}} if instance is None else instance
    return on_worker("adb", cap, args, appliance=inst)[0]


def remotes(adb):
    """The device-side shell lines run, in order."""
    return [c["argv"][-1] for c in adb.calls("adb") if c["argv"][2:5] == ["shell", "-T", "--"]]


def test_a_delegates_caps_run_on_the_hub():
    m = load(PLUGINS / "adb")
    assert m.data["runs_on"] == "hub" and m.data["uses"] == ["shell.exec"] and "requires" not in m.data
    assert {c["on"] for c in m.caps.values()} == {"worker"}


def test_scan_on_the_worker(adb):
    out, seen = on_worker("adb", "adb.scan")
    assert out == {"items": [
        {"serial": SERIAL, "state": "device", "model": "SM_A525F"},
        {"serial": "emulator-5554", "state": "unauthorized"}]}
    assert seen == [("shell.exec", {"argv": ["adb", "devices", "-l"], "timeout": 10})]
    assert adb.calls("adb")[0]["argv"] == ["devices", "-l"]


def test_adb_missing_is_said_plainly(bins):
    bins.only_these()
    with pytest.raises(KitError, match="adb is not installed on this worker"):
        on_worker("adb", "adb.scan")


def test_every_device_call_names_the_serial(adb):
    call("adb.battery.status")
    call("adb.hid.click", {"x": 1, "y": 2})
    assert {tuple(c["argv"][:2]) for c in adb.calls("adb")} == {("-s", SERIAL)}


def test_serial_defaults_to_the_instance_name(adb):
    call("adb.battery.status", instance={"name": SERIAL})
    assert adb.calls("adb")[0]["argv"][:2] == ["-s", SERIAL]


@pytest.mark.parametrize("instance", [{}, {"name": "-s x"}, {"settings": {"serial": "a b"}}, {"settings": {"serial": "x;reboot"}}])
def test_no_or_bad_instance(adb, instance):
    with pytest.raises(KitError, match="no device instance"):
        call("adb.battery.status", instance=instance)
    assert adb.calls("adb") == []


def test_device_states(adb):
    with pytest.raises(KitError, match="tap Allow"):
        call("adb.hid.click", {"x": 1, "y": 1}, instance={"name": "t", "settings": {"serial": "UNAUTH"}})
    with pytest.raises(KitError, match="not connected") as e:
        call("adb.hid.click", {"x": 1, "y": 1}, instance={"name": "t", "settings": {"serial": "OTHER"}})
    assert e.value.retry
    assert remotes(adb) == []   # nothing ran on a device that isn't ready


def test_shell_exec_argv_is_quoted(adb):
    res = call("adb.shell.exec", {"argv": ["echo", "a b", "$(reboot)", ";ls"], "stdin": "hi"})
    assert res == {"code": 0, "stdout": "ran:echo 'a b' '$(reboot)' ';ls'|in:hi", "stderr": ""}


def test_shell_exec_cmd_env_cwd(adb):
    res = call("adb.shell.exec", {"cmd": "ls | wc -l; exit 3", "cwd": "/sdcard/my dir",
                                  "env": {"A": "x y", "B": "$HOME"}})
    assert res["code"] == 3
    assert remotes(adb)[-1] == ("export A='x y'\nexport B='$HOME'\ncd '/sdcard/my dir' || exit 1\n"
                                "ls | wc -l; exit 3")


@pytest.mark.parametrize("args,match", [
    ({}, "argv or cmd"),
    ({"argv": ["ls"], "cmd": "ls"}, "argv or cmd"),
    ({"argv": []}, "argv is empty"),
    ({"cmd": "ls", "env": {"A B": "x"}}, "bad environment name"),
    ({"cmd": "ls", "env": {"A": 1}}, "object of strings"),
])
def test_shell_exec_refusals(adb, args, match):
    with pytest.raises(KitError, match=match):
        call("adb.shell.exec", args)
    assert remotes(adb) == []


def test_shell_exec_timeout_is_a_clean_error(adb):
    with pytest.raises(KitError, match="timed out after 1s; ended on the device"):
        call("adb.shell.exec", {"cmd": "sleep 60", "timeout": 1})


def test_shell_exec_cuts_at_the_output_limit(adb):
    res = call("adb.shell.exec", {"cmd": "flood", "timeout": 20})
    assert res["truncated"] is True and len(res["stdout"]) == 1 << 20


def test_shell_exec_device_gone(adb):
    with pytest.raises(KitError, match="left adb while the command ran"):
        call("adb.shell.exec", {"cmd": "gone"})


def test_battery(adb):
    assert call("adb.battery.status") == {"percent": 87, "charging": True, "plugged": "usb",
                                          "status": "charging", "temp_c": 25.1}


def test_screen_capture_is_a_full_size_jpeg(adb):
    res = call("adb.screen.capture", {"quality": 40})
    assert res["mime"] == "image/jpeg" and res["name"] == "screen.jpg"
    img = Image.open(io.BytesIO(base64.b64decode(res["data"])))
    assert (img.format, img.size) == ("JPEG", (2400, 1080))   # its pixels are hid.click's x, y


def test_ui_screen(adb):
    res = call("adb.ui.screen")
    assert res == {"app": "com.android.settings", "items": [
        {"text": "Navigate up", "x": 50, "y": 150, "clickable": True},
        {"text": "Wi-Fi", "x": 540, "y": 450, "clickable": True},
        {"text": "Bluetooth", "x": 540, "y": 650}]}
    assert call("adb.ui.screen", {"limit": 1})["items"] == res["items"][:1]


def test_type_escapes_for_input_text(adb):
    call("adb.hid.type", {"text": "it's a $(test)\nline 2", "enter": True})
    assert remotes(adb)[-1] == ("input text 'it'\"'\"'s%sa%s$(test)' && input keyevent KEYCODE_ENTER"
                                " && input text line%s2 && input keyevent KEYCODE_ENTER")


def test_type_refuses_non_ascii(adb):
    with pytest.raises(KitError, match="ASCII only"):
        call("adb.hid.type", {"text": "héllo"})
    with pytest.raises(KitError, match="bad args"):
        call("adb.hid.type", {"text": "x" * 2001})


def test_keys(adb):
    call("adb.hid.key", {"key": "home"})
    call("adb.hid.key", {"key": "a", "mods": ["ctrl", "shift", "ctrl"]})
    call("adb.hid.key", {"key": "KEYCODE_CAMERA"})
    assert remotes(adb) == ["input keyevent KEYCODE_HOME",
                            "input keycombination KEYCODE_CTRL_LEFT KEYCODE_SHIFT_LEFT KEYCODE_A",
                            "input keyevent KEYCODE_CAMERA"]


@pytest.mark.parametrize("args", [{"key": "a;reboot"}, {"key": "a b"}, {"key": ""},
                                  {"key": "a", "mods": ["hyper"]}])
def test_key_args_refused(adb, args):
    with pytest.raises(KitError, match="bad args"):
        call("adb.hid.key", args)


def test_unknown_key(adb):
    with pytest.raises(KitError, match="unknown key 'hyperdrive'"):
        call("adb.hid.key", {"key": "hyperdrive"})
    with pytest.raises(KitError, match="unknown key '_'"):
        call("adb.hid.key", {"key": "_"})


def test_click_and_drag(adb):
    call("adb.hid.click", {"x": 540, "y": 450})
    call("adb.hid.drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})
    call("adb.hid.drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 60})
    assert remotes(adb) == ["input tap 540 450", "input swipe 1 2 3 4 300", "input swipe 1 2 3 4 10000"]


@pytest.mark.parametrize("cap,args,match", [
    ("adb.hid.click", {}, "needs x and y"),
    ("adb.hid.click", {"x": 5}, "needs x and y"),
    ("adb.hid.click", {"x": 5, "y": 5, "button": "right"}, "no right button"),
    ("adb.hid.drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "button": "middle"}, "no middle button"),
])
def test_touch_refusals(adb, cap, args, match):
    with pytest.raises(KitError, match=match):
        call(cap, args)
    assert remotes(adb) == []


def test_input_failure_is_an_error(adb):
    with pytest.raises(KitError, match="Error: bad input"):
        call("adb.hid.type", {"text": "fail"})


def test_no_04_names():
    """castling-protocol (Bake): "Clean cut: no 0.4/0.5 compatibility." """
    caps = load(PLUGINS / "adb").caps
    for old in ("android.shell.exec", "android.ui.text", "android.hid.key_combo", "android.hid.mouse.click"):
        assert old not in caps
