"""adb (Android over adb) with a fake ``adb`` on PATH that answers like a
real one for one attached serial.

TODO(castling:worker-host): the host can't run appliance caps yet (no
instance values reach Host.call or ctx; spec 5.7), so ``call`` below does
what it will: the loader's bound cap, the host's arg check and defaults, then
the script with ``ctx.appliance`` set. Switch to host.call once it takes an
appliance. ``adb.scan`` needs no instance and goes through the host.
"""

import base64
import copy

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import Host
from rook.core2.worker.loader import load_folder
from rook.kit.ctx import Ctx, FileResult, KitError, invoke

SERIAL = "R52N30ABCDE"
PNG = b"\x89PNG\r\n\x1a\nfake-png"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
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
SERIAL, PNG, DUMP, BATTERY = {SERIAL!r}, {PNG!r}, {DUMP!r}, {BATTERY!r}
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
if cmd == "exec-out" and rest == ["screencap", "-p"]:
    out.write(PNG); sys.exit(0)
if cmd == "exec-out" and rest[:2] == ["uiautomator", "dump"]:
    out.write(DUMP.encode()); sys.exit(0)
if cmd == "shell" and rest[:2] == ["-T", "--"]:
    remote = rest[2]
    if remote == "dumpsys battery":
        out.write(BATTERY.encode()); sys.exit(0)
    if remote.startswith("sleep"):
        time.sleep(30)
    if remote == "flood":
        out.write(b"y" * (3 << 20)); out.flush(); time.sleep(30)
    if remote == "gone":
        sys.stderr.write("adb: device 'x' not found\\n"); sys.exit(1)
    if remote.startswith("input") and "fail" in remote:
        sys.stderr.write("Error: bad input\\n"); sys.exit(2)
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    out.write(("ran:" + remote + "|in:" + data).encode())
    sys.exit(3 if remote.endswith("exit 3") else 0)
sys.stderr.write("fake adb: unexpected " + repr(argv) + "\\n"); sys.exit(99)
"""

PLUGIN = load_folder(PLUGINS / "adb", os_name="linux")


@pytest.fixture
def adb(bins):
    bins.add("adb", FAKE_ADB)
    bins.only_these()     # no real ImageMagick: tests add a fake one when they want it
    return bins


def call(cap, args=None, instance=None, timeout=20):
    c = PLUGIN.caps[cap]
    args = dict(args or {})
    errors = list(c.validator.iter_errors(args))
    if errors:
        raise ValueError(f"bad args: {errors[0].message}")
    for key, spec in c.spec.get("args", {}).items():
        if key not in args and "default" in spec:
            args[key] = copy.deepcopy(spec["default"])
    args.update(copy.deepcopy(c.bind))

    async def body():
        ctx = Ctx(PLUGIN.manifest, ME, cap=cap)
        ctx.appliance = {"name": "playground-tablet", "serial": SERIAL} if instance is None else instance
        return await invoke(c.func, ctx, args)
    return go(body, timeout)


def remotes(adb):
    """The device-side shell lines run, in order."""
    return [c["argv"][-1] for c in adb.calls("adb") if c["argv"][2:5] == ["shell", "-T", "--"]]


def test_scan_through_the_host(adb):
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "adb")
        try:
            return await host.call("adb.scan", {}, ME)
        finally:
            await host.close()
    assert go(body) == {"items": [
        {"serial": SERIAL, "state": "device", "model": "SM_A525F"},
        {"serial": "emulator-5554", "state": "unauthorized"}]}
    assert adb.calls("adb")[0]["argv"] == ["devices", "-l"]


def test_every_device_call_names_the_serial(adb):
    call("adb.battery.status")
    call("adb.hid.click", {"x": 1, "y": 2})
    assert {tuple(c["argv"][:2]) for c in adb.calls("adb")} == {("-s", SERIAL)}


def test_serial_defaults_to_the_instance_name(adb):
    call("adb.battery.status", instance={"name": SERIAL})
    assert adb.calls("adb")[0]["argv"][:2] == ["-s", SERIAL]


@pytest.mark.parametrize("instance", [{}, {"name": "-s x"}, {"serial": "a b"}, {"serial": "x;reboot"}])
def test_no_or_bad_instance(adb, instance):
    with pytest.raises(KitError, match="no device instance"):
        call("adb.battery.status", instance=instance)
    assert adb.calls("adb") == []


def test_device_states(adb):
    with pytest.raises(KitError, match="tap Allow"):
        call("adb.hid.click", {"x": 1, "y": 1}, instance={"name": "t", "serial": "UNAUTH"})
    with pytest.raises(KitError, match="not connected") as e:
        call("adb.hid.click", {"x": 1, "y": 1}, instance={"name": "t", "serial": "OTHER"})
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
        call("adb.shell.exec", {"cmd": "sleep 60", "timeout": 1}, timeout=15)


def test_shell_exec_stops_at_the_output_limit(adb):
    res = call("adb.shell.exec", {"cmd": "flood", "timeout": 20}, timeout=15)
    assert res["truncated"] is True and len(res["stdout"]) == 1 << 20


def test_shell_exec_device_gone(adb):
    with pytest.raises(KitError, match="left adb while the command ran"):
        call("adb.shell.exec", {"cmd": "gone"})


def test_battery(adb):
    assert call("adb.battery.status") == {"percent": 87, "charging": True, "plugged": "usb",
                                          "status": "charging", "temp_c": 25.1}


def test_screen_capture_png_without_imagemagick(adb):
    res = call("adb.screen.capture")
    assert isinstance(res, FileResult) and res.mime == "image/png"
    assert res.data == PNG


def test_screen_capture_jpeg_with_imagemagick(adb):
    adb.add("convert", f"open(argv[-1], 'wb').write({JPEG!r})\n")
    res = call("adb.screen.capture", {"quality": 40})
    assert (res.data, res.mime) == (JPEG, "image/jpeg")
    assert adb.calls("convert")[0]["argv"][1:5] == ["-resize", "1600x1600>", "-quality", "40"]


def test_screen_capture_through_the_host_returns_bytes(adb):
    # The host turns a file result into {data, mime}; the call needs the
    # instance, so drive the bound function the way host._exec will.
    async def body():
        host = Host(os_name="linux")
        p = await host.load(PLUGINS / "adb")
        c = p.caps["adb.screen.capture"]
        ctx = host._ctx(p, ME, cap=c.name)
        ctx.appliance = {"name": "t", "serial": SERIAL}
        from rook.core2.worker.host import _jsonable
        try:
            return _jsonable(await invoke(c.func, ctx, {}))
        finally:
            await host.close()
    res = go(body)
    assert base64.b64decode(res["data"]) == PNG and res["mime"] == "image/png"


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
    with pytest.raises(ValueError, match="bad args"):
        call("adb.hid.type", {"text": "x" * 2001})


def test_keys(adb):
    call("adb.hid.key", {"key": "home"})
    call("adb.hid.key", {"key": "a", "mods": ["ctrl", "shift", "ctrl"]})
    call("android.hid.key_combo", {"key": "KEYCODE_CAMERA"})
    assert remotes(adb) == ["input keyevent KEYCODE_HOME",
                            "input keycombination KEYCODE_CTRL_LEFT KEYCODE_SHIFT_LEFT KEYCODE_A",
                            "input keyevent KEYCODE_CAMERA"]


@pytest.mark.parametrize("args", [{"key": "a;reboot"}, {"key": "a b"}, {"key": ""},
                                  {"key": "a", "mods": ["hyper"]}])
def test_key_args_refused(adb, args):
    with pytest.raises(ValueError, match="bad args"):
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


def test_aliases_reach_the_caps():
    for old, new in [("android.shell.exec", "adb.shell.exec"), ("android.ui.text", "adb.ui.screen"),
                     ("android.hid.key_combo", "adb.hid.key"), ("android.hid.mouse.click", "adb.hid.click")]:
        assert PLUGIN.caps[old] is PLUGIN.caps[new]


def test_adb_missing_makes_the_plugin_unavailable(bins):
    bins.only_these()

    async def body():
        host = Host(os_name="linux")
        p = await host.load(PLUGINS / "adb")
        try:
            return p.caps, p.unavailable
        finally:
            await host.close()
    caps, unavailable = go(body)
    assert caps == {} and "adb.scan" in unavailable and "adb" in unavailable["adb.scan"]
