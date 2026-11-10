"""battery on the hub, for a worker that serves only shell.exec. Each OS
variant runs against a fake of its tool (the worker is said to be on that
OS), so every parse is exercised on Linux."""

import json
import shutil
import subprocess

import pytest

from port_helpers import PLUGINS, on_worker
from rook.kit.ctx import KitError
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


def run(os_name="linux"):
    return on_worker("battery", "battery.status", os_name=os_name)[0]


def test_a_workers_cap_run_on_the_hub():
    m = load(PLUGINS / "battery")
    assert m.data["runs_on"] == "hub" and m.data["uses"] == ["shell.exec"]
    assert m.caps["battery.status"]["on"] == "worker" and "action" not in m.caps["battery.status"]
    assert "requires" not in m.data and "hooks" not in m.data


def test_linux_upower(bins):
    bins.add("upower", f"print({UPOWER!r})")
    out, seen = on_worker("battery", "battery.status")
    assert out == {"percent": 87.5, "status": "discharging"}
    assert seen[0][0] == "shell.exec" and seen[0][1]["env"] == {"LANG": "C"}
    assert bins.calls("upower")[0]["argv"] == ["-i", "/org/freedesktop/UPower/devices/DisplayDevice"]
    assert bins.calls("upower")[0]["env"]["LANG"] == "C"


def test_linux_no_battery_is_an_error(bins):
    bins.add("upower", f"print({NO_BATTERY!r})")
    with pytest.raises(KitError, match="battery-missing"):
        run()


def test_linux_failing_upower_falls_through_to_sysfs(bins):
    bins.add("upower", "sys.exit(1)")   # e.g. no upowerd running
    bins.only_these("sh", "tr", "cat")
    # This machine's /sys decides the rest: no BAT* means the fallback exits 3.
    try:
        assert set(run()) == {"percent", "status"}
    except KitError as e:
        assert e.message == "sh exited 3"   # the fallback ran and found no BAT*
    assert len(bins.calls("upower")) == 1


def test_sysfs_fallback_prints_upowers_shape(tmp_path):
    # The fallback's own shell, pointed at a fake /sys/class/power_supply.
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    import importlib.util
    spec = importlib.util.spec_from_file_location("battery_script", PLUGINS / "battery" / "scripts" / "battery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    script = mod.SYSFS[2]
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
