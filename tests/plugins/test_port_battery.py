"""battery through the worker host. Each OS variant runs against a fake of
its tool (the host is told which OS it is on), so every parse is exercised
on Linux."""

import json
import shutil
import subprocess

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host
from rook.kit.manifest import load

UPOWER = """\
  native-path:          BAT0
  power supply:         yes
  battery
    present:             yes
    state:               discharging
    energy:              41.2 Wh
    percentage:          87.5%
    icon-name:          'battery-full-symbolic'
"""
NO_BATTERY = """\
  power supply:         no
  unknown
    warning-level:       none
    percentage:          0%
    icon-name:          'battery-missing-symbolic'
"""


def run(os_name="linux", hardware=("battery",), cap="battery.status"):
    async def body():
        host = Host(os_name=os_name, hardware=hardware)
        await host.load(PLUGINS / "battery")
        try:
            return await host.call(cap, {}, ME)
        finally:
            await host.close()
    return go(body)


def test_linux_upower(bins):
    bins.add("upower", f"print({UPOWER!r})")
    assert run() == {"percent": 87.5, "status": "discharging"}
    assert bins.calls("upower")[0]["argv"] == ["-i", "/org/freedesktop/UPower/devices/DisplayDevice"]


def test_linux_no_battery_is_an_error(bins):
    bins.add("upower", f"print({NO_BATTERY!r})")
    with pytest.raises(CallError, match="battery-missing"):
        run()


def test_linux_failing_upower_falls_through_to_sysfs(bins):
    bins.add("upower", "sys.exit(1)")   # e.g. no upowerd running
    bins.only_these("sh", "tr", "cat")
    # This machine's /sys decides the rest: no BAT* means the fallback exits 3.
    try:
        assert set(run()) == {"percent", "status"}
    except CallError as e:
        assert e.message == "sh exited 3"   # the fallback ran and found no BAT*
    assert len(bins.calls("upower")) == 1


def test_sysfs_fallback_prints_upowers_shape(tmp_path):
    # The fallback's own shell, pointed at a fake /sys/class/power_supply.
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    spec = load(PLUGINS / "battery").caps["battery.status"]
    script = spec["action"]["exec"]["try"][1]["argv"][2]
    for name, status, cap in [("AC", None, None), ("BAT1", "Not charging", "64")]:
        d = tmp_path / name
        d.mkdir()
        if status:
            (d / "status").write_text(status + "\n")
            (d / "capacity").write_text(cap + "\n")
    out = subprocess.run([sh, "-c", script.replace("/sys/class/power_supply", str(tmp_path))],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    assert out.stdout == "state: not-charging\npercentage: 64%\n"
    empty = tmp_path / "none"
    empty.mkdir()
    out = subprocess.run([sh, "-c", script.replace("/sys/class/power_supply", str(empty))],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 3


def test_macos_pmset(bins):
    bins.add("pmset", r"""print("Now drawing from 'Battery Power'\n"
      " -InternalBattery-0 (id=4653155)\t85%; discharging; 4:12 remaining present: true")""")
    assert run("macos") == {"percent": 85.0, "status": "discharging"}
    assert bins.calls("pmset")[0]["argv"] == ["-g", "batt"]


def test_termux(bins):
    out = {"health": "GOOD", "percentage": 54, "plugged": "UNPLUGGED", "status": "DISCHARGING",
           "temperature": 27.5}
    bins.add("termux-battery-status", f"print({json.dumps(out)!r})")
    assert run("termux") == {"percent": 54.0, "status": "DISCHARGING"}


def test_windows_script_shape(bins):
    # The real script prints upower's two lines; the fake prints what it would.
    bins.add("powershell", "print('state: charging'); print('percentage: 55%')")
    assert run("windows") == {"percent": 55.0, "status": "charging"}
    argv = bins.calls("powershell")[0]["argv"]
    assert argv[:4] == ["-NoProfile", "-NonInteractive", "-Command", argv[3]]
    assert "Win32_Battery" in argv[3]


def test_needs_a_battery():
    async def body():
        host = Host(os_name="linux")
        p = await host.load(PLUGINS / "battery")
        assert p.reason == "missing hardware: battery"
        assert p.unavailable == {"battery.status": "missing hardware: battery"}
        await host.close()
    go(body)


def test_android_app_is_another_plugin():
    async def body():
        host = Host(os_name="android", hardware={"battery"})
        p = await host.load(PLUGINS / "battery")
        assert p.reason.startswith("not on android")
        await host.close()
    go(body)


def test_status_hook_rides_the_heartbeat(bins):
    bins.add("upower", f"print({UPOWER!r})")

    async def body():
        host = None

        async def dispatch(cap, args, caller):   # the hub's core.call, looped back
            return await host.call(cap, args, caller)
        host = Host(os_name="linux", hardware={"battery"}, dispatch=dispatch)
        await host.load(PLUGINS / "battery")
        announce = await host.announce()
        assert announce["status"] == {"battery": {"percent": 87.5, "status": "discharging"}}
        await host.close()
    go(body)
