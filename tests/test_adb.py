"""The adb delegate plugin: Android devices as device workers, against a fake adb."""

import asyncio
import base64
import json
import os
import sys
import textwrap

import pytest

from rook.core import authz
from rook.core.plugin import SettingsView, validate_caps
from rook.core.pluginkit import ArgError, CapError
from rook.worker.devices import DeviceTable
from rook.worker.plugins import adb

# A stand-in for the adb binary: logs its argv, answers from state.json.
FAKE_ADB = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    state = json.load(open(os.path.join(here, "state.json")))
    args = sys.argv[1:]
    with open(os.path.join(here, "calls.jsonl"), "a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[:2] == ["devices", "-l"]:
        print("List of devices attached")
        for serial, st in state["devices"].items():
            print(serial + "    " + st + " product:x model:Tab_S9 device:y transport_id:1")
        sys.exit(0)
    serial = args[1]
    st = state["devices"].get(serial)
    if st is None:
        print("adb: device '%s' not found" % serial, file=sys.stderr); sys.exit(1)
    if st != "device":
        print("adb: device %s" % st, file=sys.stderr); sys.exit(1)
    rest = args[2:]
    if rest == ["get-state"]:
        print("device"); sys.exit(0)
    if rest[:3] == ["exec-out", "screencap", "-p"]:
        sys.stdout.buffer.write(b"\\x89PNG fake"); sys.exit(0)
    if rest[:2] == ["exec-out", "uiautomator"]:
        sys.stdout.write("UI hierchary dumped to: /dev/tty" + state.get("ui", "")); sys.exit(0)
    if rest[:3] == ["shell", "-T", "--"]:
        remote = rest[3]
        if "sleep" in remote:
            time.sleep(5)
        reply = state.get("shell", {{}}).get(remote)
        if reply is None:
            sys.stdout.write(sys.stdin.read() if not sys.stdin.isatty() else "")
            sys.exit(0)
        sys.stdout.write(reply.get("out", "")); sys.stderr.write(reply.get("err", ""))
        sys.exit(reply.get("code", 0))
    sys.exit(2)
""")

UI = ('<?xml version="1.0"?><hierarchy rotation="0">'
      '<node text="" content-desc="" bounds="[0,0][1080,2340]">'
      '<node text="Settings" clickable="true" bounds="[100,900][300,1000]"/>'
      '<node text="" content-desc="Search" bounds="[0,100][200,200]"/>'
      '<node text="  " bounds="[0,0][1,1]"/></node></hierarchy>')


class Fake:
    def __init__(self, tmp_path, devices):
        self.dir = tmp_path
        self.state = {"devices": devices, "shell": {}, "ui": UI}
        self.path = tmp_path / "adb"
        self.path.write_text(FAKE_ADB.format(python=sys.executable))
        self.path.chmod(0o755)
        self.save()

    def save(self):
        (self.dir / "state.json").write_text(json.dumps(self.state))

    def calls(self):
        f = self.dir / "calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def _plugin(fake, spec="playground-tablet=R52N30ABCDE"):
    p = adb.AdbPlugin()
    p.__dict__["_settings"] = SettingsView(p, {"devices": spec, "path": str(fake.path)})
    return p


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path, {"R52N30ABCDE": "device", "OTHERPHONE01": "device"})


async def _started(p):
    await p.start()
    # The first poll runs at once; wait for it to land.
    for _ in range(100):
        dev = p.devices()[0]
        if dev.state.get("adb") != "missing":
            break
        await asyncio.sleep(0.05)
    return p.devices()[0]


def test_device_list_parsing():
    assert adb.parse_devices("playground-tablet=R52N30ABCDE, emu=emulator-5554\n") == [
        ("playground-tablet", "R52N30ABCDE"), ("emu", "emulator-5554")]
    assert adb.parse_devices("") == []
    for bad in ("R52N30ABCDE", "=R52", "a=b c", "a=x;rm", "a=1,A=2", "a=1,b=1", "bad/name=1"):
        with pytest.raises(ValueError):
            adb.parse_devices(bad)


def test_adb_devices_output_parsing():
    text = ("* daemon started successfully\nList of devices attached\n"
            "R52N30ABCDE    device usb:1-1 product:gts9 model:SM_X710 device:gts9\n"
            "0123456789     unauthorized usb:1-2 transport_id:4\n\n")
    assert adb.parse_adb_devices(text) == {
        "R52N30ABCDE": {"state": "device", "model": "SM_X710"},
        "0123456789": {"state": "unauthorized"}}


def test_ui_items_are_readable_text_with_tap_points():
    assert adb.ui_items(UI) == [
        {"text": "Search", "x": 100, "y": 150},
        {"text": "Settings", "x": 200, "y": 950, "clickable": True}]
    with pytest.raises(CapError):
        adb.ui_items("<hierarchy")


def test_plugin_is_off_without_devices_or_adb(fake, monkeypatch):
    monkeypatch.delenv("ROOK_ADB_DEVICES", raising=False)
    p = adb.AdbPlugin()
    p.__dict__["_settings"] = SettingsView(p, {"path": str(fake.path)})
    assert not p.available() and "adb.devices" in p.unavailable_reason()
    p.__dict__["_settings"] = SettingsView(p, {"devices": "t=R52", "path": "/nonexistent/adb"})
    assert not p.available() and "adb" in p.unavailable_reason()
    assert _plugin(fake).available()


def test_device_caps_use_the_android_app_names_and_tiers():
    dev = adb.AndroidDevice(adb.AdbPlugin(), "playground-tablet", "R52N30ABCDE")
    caps = dev.caps()
    assert sorted(caps) == sorted(adb.AndroidDevice.declared_caps()) == [
        "battery.status", "device.info", "hid.key_combo", "hid.mouse.click", "hid.mouse.drag",
        "hid.type", "screenshot.capture", "shell.exec", "ui.text"]
    assert validate_caps(caps) == []
    # Never below the floor the same names have elsewhere (Python plugins, the app).
    floor = authz.declared_floor(["rook.worker.plugins"])
    for name, fn in caps.items():
        risk = fn._rook_cap_meta.risk
        want = authz.CORE.get(name, floor.get(name, ("read", ())))[0]
        assert authz.TIERS.index(risk) >= authz.TIERS.index(want), name
    assert caps["shell.exec"]._rook_cap_meta.risk == "exec"


def test_device_table_announces_a_device_worker(fake):
    p = _plugin(fake)
    p._devices = {"r52n30abcde": adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")}
    table = DeviceTable("a" * 32)
    [entry] = table.refresh([p])
    msg = table.announce(entry, {"version": "x"})
    assert msg["name"] == "playground-tablet" and msg["device"] == {"type": "android", "driver": "adb"}
    assert "shell.exec" in msg["caps"] and msg["tiers"]["shell.exec"] == "exec"
    assert msg["hb"] == {"android": {"on": False, "adb": "missing"}}


@pytest.mark.asyncio
async def test_poll_heartbeat_and_only_listed_serials(fake):
    p = _plugin(fake)
    dev = await _started(p)
    try:
        for _ in range(100):
            if dev.state.get("battery") is not None:
                break
            await asyncio.sleep(0.05)
        assert dev.state["adb"] == "device" and dev.info()["model"] == "Tab_S9"
        listed = await p._list()
        assert [i["name"] for i in listed["items"]] == ["playground-tablet"]
        scan = await p._scan()
        assert {i["serial"]: i["configured_as"] for i in scan["items"]} == {
            "OTHERPHONE01": None, "R52N30ABCDE": "playground-tablet"}
        await dev.shell_exec(cmd="true")
        # Every device call names the listed serial; the other phone is never addressed.
        for call in fake.calls():
            assert call[:2] == ["devices", "-l"] or call[:2] == ["-s", "R52N30ABCDE"], call
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_shell_exec_reply_quoting_env_cwd_stdin(fake):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    fake.state["shell"]["echo hi; exit 3"] = {"out": "hi\n", "err": "oops\n", "code": 3}
    fake.save()
    r = await dev.shell_exec(cmd="echo hi; exit 3")
    assert r == {"ok": False, "code": 3, "stdout": "hi\n", "stderr": "oops\n"}
    r = await dev.shell_exec(argv=["cat"], stdin="piped\n")
    assert r["ok"] and r["stdout"] == "piped\n"
    await dev.shell_exec(argv=["echo", "it's $HOME"], cwd="/data/local/tmp", env={"A": "x y"})
    remote = fake.calls()[-1]
    assert remote[:5] == ["-s", "R52N30ABCDE", "shell", "-T", "--"]
    assert remote[5] == ("export A='x y'\ncd /data/local/tmp || exit 1\n"
                         "echo 'it'\"'\"'s $HOME'")
    for bad in (dict(), dict(cmd="x", argv=["y"]), dict(cmd="x", shell="bash"),
                dict(argv="ls"), dict(cmd="x", env={"A B": "1"})):
        with pytest.raises(ArgError):
            await dev.shell_exec(**bad)


@pytest.mark.asyncio
async def test_shell_exec_timeout_and_output_cap(fake, monkeypatch):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    r = await dev.shell_exec(cmd="sleep 30", timeout=1)
    assert r["ok"] is False and r["error"] == "timeout"
    monkeypatch.setattr(adb, "OUTPUT_CAP", 4)
    r = await dev.shell_exec(argv=["cat"], stdin="123456789" * 100000)
    assert r["stdout"] == "1234" and r["truncated"] and not r["ok"] and r["code"] is None


@pytest.mark.asyncio
async def test_unplugged_mid_command_is_not_the_commands_exit_code(fake):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    fake.state["shell"]["long job"] = {"err": "adb: device 'R52N30ABCDE' not found\n", "code": 1}
    fake.state["shell"]["false"] = {"err": "error: nope\n", "code": 1}
    fake.save()
    r = await dev.shell_exec(cmd="long job")
    assert r["ok"] is False and r["error"] == "disconnected" and dev.state["adb"] == "missing"
    r = await dev.shell_exec(cmd="false")
    assert r == {"ok": False, "code": 1, "stdout": "", "stderr": "error: nope\n"}


@pytest.mark.asyncio
async def test_two_callers_typing_never_interleave(fake):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    await asyncio.gather(dev.hid_type("one\ntwo"), dev.hid_type("three\nfour"))
    remotes = sorted(c[5] for c in fake.calls() if c[2:3] == ["shell"])
    assert remotes == ["input text one && input keyevent KEYCODE_ENTER && input text two",
                       "input text three && input keyevent KEYCODE_ENTER && input text four"]


@pytest.mark.asyncio
async def test_unauthorized_or_missing_device_fails_plainly(fake):
    fake.state["devices"]["R52N30ABCDE"] = "unauthorized"
    fake.save()
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    with pytest.raises(CapError, match="tap Allow"):
        await dev.shell_exec(cmd="id")
    assert dev.heartbeat() == {"on": False, "paired": False, "adb": "unauthorized"}
    # Only get-state ran: nothing tried to authorize or run the command.
    assert [c[2:] for c in fake.calls()] == [["get-state"]]
    del fake.state["devices"]["R52N30ABCDE"]
    fake.save()
    with pytest.raises(CapError, match="not connected"):
        await dev.device_info()


@pytest.mark.asyncio
async def test_info_battery_screen_and_ui(fake):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    fake.state["shell"].update({
        ("getprop ro.product.manufacturer; getprop ro.product.model; "
         "getprop ro.build.version.release; getprop ro.build.version.sdk; "
         "wm size | tail -n1; dumpsys power | grep -m1 mWakefulness=; cat /proc/uptime"):
            {"out": "samsung\nSM-X710\n14\n34\nPhysical size: 1600x2560\n"
                    "  mWakefulness=Asleep\n1234.5 99.0\n"},
        "dumpsys battery": {"out": "Current Battery Service state:\n  AC powered: false\n"
                                   "  USB powered: true\n  status: 2\n  level: 57\n"
                                   "  temperature: 291\n"}})
    fake.save()
    info = await dev.device_info()
    assert info == {"name": "playground-tablet", "serial": "R52N30ABCDE", "adb": "device",
                    "manufacturer": "samsung", "model": "SM-X710", "android": "14", "sdk": 34,
                    "awake": False, "screen": {"width": 1600, "height": 2560}, "uptime_s": 1234}
    assert await dev.battery_status() == {"percent": 57, "charging": True, "plugged": "usb",
                                          "status": "charging", "temp_c": 29.1}
    shot = await dev.screenshot_capture()
    assert shot["format"] in ("png", "jpeg") and shot["bytes"] > 0
    assert base64.b64decode(shot["data"])
    ui = await dev.ui_text()
    assert ui["text"] == "Search\nSettings" and ui["items"][1]["clickable"]


@pytest.mark.asyncio
async def test_input_commands(fake):
    p = _plugin(fake)
    dev = adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")
    await dev.hid_type("50% off\nok")
    await dev.hid_key_combo("home")
    await dev.hid_key_combo("ctrl+a")
    await dev.hid_mouse_click(200, 950)
    await dev.hid_mouse_drag(1, 2, 3, 4, duration=0.5)
    remotes = [c[5] for c in fake.calls() if c[2:3] == ["shell"]]
    assert remotes == ["input text 50%%soff && input keyevent KEYCODE_ENTER && input text ok",
                       "input keyevent KEYCODE_HOME",
                       "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A",
                       "input tap 200 950", "input swipe 1 2 3 4 500"]
    for call, args in ((dev.hid_type, ("héllo",)), (dev.hid_key_combo, ("hyper",)),
                       (dev.hid_mouse_click, (-1, 5))):
        with pytest.raises(ArgError):
            await call(*args)


class _Transport:
    band_id = bytes(16)

    def __init__(self):
        self.sent = []

    async def send(self, data, route=0):
        self.sent.append(json.loads(data))


@pytest.mark.asyncio
async def test_worker_answers_shell_exec_for_the_tablet_in_its_name(fake, tmp_path, monkeypatch):
    from rook.core.devices import device_worker_id
    from rook.worker import admin, audit, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    w = core.Worker(_Transport(), enabled=["info", "shell"], name="hub-box")
    p = _plugin(fake)
    p._devices = {"r52n30abcde": adb.AndroidDevice(p, "playground-tablet", "R52N30ABCDE")}
    w.plugins.append(p)
    await w.announce()
    tablet = device_worker_id(w.worker_id, "adb", "r52n30abcde")
    ann = next(m for m in w.transport.sent if m.get("worker_id") == tablet)
    assert ann["name"] == "playground-tablet" and ann["delegate"] == w.worker_id
    fake.state["shell"]["id"] = {"out": "uid=2000(shell)\n"}
    fake.save()
    before = len(w.transport.sent)
    msg = {"id": "m1", "cap": "shell.exec", "target": tablet, "args": {"cmd": "id"}}
    await w._on_message(json.dumps(msg).encode(), ())
    [reply] = w.transport.sent[before:]
    # The tablet ran it (adb -s <serial>), not the delegate's own shell.
    assert reply["from"] == tablet and reply["result"]["stdout"] == "uid=2000(shell)\n"
    assert fake.calls()[-1][:2] == ["-s", "R52N30ABCDE"]
