"""hid-desktop on the hub, for a worker with only the primitives, with fake
xdotool / wtype / ydotool / powershell on its PATH (each records its argv and
stdin)."""

import pytest

from port_helpers import on_worker
from rook.kit.ctx import KitError

# A fake input tool: logs stdin to <name>.stdin; exits $FAIL_<NAME> if set.
TOOL = """
name = os.path.basename(sys.argv[0])
data = sys.stdin.read() if not sys.stdin.isatty() else ""
with open(os.path.join(os.path.dirname(sys.argv[0]), name + ".stdin"), "a") as f:
    f.write(data)
code = int(os.environ.get("FAIL_" + name.upper(), "0"))
if code:
    print(name + ": cannot open display", file=sys.stderr)
sys.exit(code)
"""


def run(cap, args=None, os_name="linux"):
    return on_worker("hid-desktop", f"hid-desktop.{cap}", args, os_name=os_name)[0]


def tools(bins, *names):
    for n in names:
        bins.add(n, TOOL)
    bins.only_these()


def argvs(bins, name):
    return [c["argv"] for c in bins.calls(name)]


def stdin(bins, name):
    p = bins.dir / f"{name}.stdin"
    return p.read_text() if p.exists() else None


def test_type_sends_text_on_stdin_not_argv(bins):
    tools(bins, "xdotool")
    run("hid.type", {"text": "hunter2 $(reboot) -x"})
    run("hid.type", {"text": "go", "enter": True})
    assert argvs(bins, "xdotool") == [["type", "--clearmodifiers", "--file", "-"]] * 2
    assert stdin(bins, "xdotool") == "hunter2 $(reboot) -xgo\n"


def test_type_falls_through_a_failing_tool(bins, monkeypatch):
    tools(bins, "xdotool", "wtype")
    monkeypatch.setenv("FAIL_XDOTOOL", "1")
    run("hid.type", {"text": "hi"})
    assert argvs(bins, "wtype") == [["-"]]
    assert stdin(bins, "wtype") == "hi"


def test_type_without_any_tool_is_a_clean_error(bins):
    bins.only_these()
    with pytest.raises(KitError, match="not installed on this worker: xdotool, wtype, ydotool"):
        run("hid.type", {"text": "hi"})


def test_move(bins):
    tools(bins, "ydotool")
    run("hid.move", {"x": 10, "y": 20})
    assert argvs(bins, "ydotool") == [["mousemove", "--absolute", "-x", "10", "-y", "20"]]
    with pytest.raises(KitError, match="bad args"):
        run("hid.move", {"x": -1, "y": 0})


def test_key_xdotool(bins):
    tools(bins, "xdotool")
    out = run("hid.key", {"key": "enter", "mods": ["ctrl", "meta"]})
    run("hid.key", {"key": "Print"})
    assert argvs(bins, "xdotool") == [["key", "--clearmodifiers", "ctrl+super+Return"],
                                      ["key", "--clearmodifiers", "Print"]]
    assert out["backend"] == "xdotool"


def test_key_wtype_releases_in_reverse(bins):
    tools(bins, "wtype")
    run("hid.key", {"key": "a", "mods": ["ctrl", "shift"]})
    assert argvs(bins, "wtype") == [["-M", "ctrl", "-M", "shift", "-k", "a", "-m", "shift", "-m", "ctrl"]]


def test_key_ydotool_uses_input_codes(bins):
    tools(bins, "ydotool")
    run("hid.key", {"key": "c", "mods": ["ctrl"]})
    run("hid.key", {"key": "0"})
    assert argvs(bins, "ydotool") == [["key", "29:1", "46:1", "46:0", "29:0"], ["key", "11:1", "11:0"]]
    with pytest.raises(KitError, match="ydotool: unknown key Print"):
        run("hid.key", {"key": "Print"})


def test_key_refuses_syntax(bins):
    tools(bins, "xdotool")
    with pytest.raises(KitError, match="bad args"):
        run("hid.key", {"key": "a+ctrl"})
    with pytest.raises(KitError, match="bad args"):
        run("hid.key", {"key": "a", "mods": ["hyper"]})
    assert argvs(bins, "xdotool") == []


def test_click(bins):
    tools(bins, "xdotool")
    run("hid.click", {})
    run("hid.click", {"x": 5, "y": 6, "button": "right"})
    assert argvs(bins, "xdotool") == [["click", "1"], ["mousemove", "5", "6", "click", "3"]]
    with pytest.raises(KitError, match="x and y together"):
        run("hid.click", {"x": 5})


def test_click_ydotool(bins):
    tools(bins, "ydotool")
    run("hid.click", {"x": 1, "y": 2, "button": "middle"})
    assert argvs(bins, "ydotool") == [["mousemove", "--absolute", "-x", "1", "-y", "2"], ["click", "0xC2"]]


def test_drag_xdotool_is_one_chain(bins):
    tools(bins, "xdotool")
    run("hid.drag", {"x1": 1, "y1": 2, "x2": 30, "y2": 40, "duration": "50ms"})
    assert argvs(bins, "xdotool") == [["mousemove", "1", "2", "mousedown", "1", "sleep", "0.05",
                                       "mousemove", "30", "40", "mouseup", "1"]]


def test_drag_ydotool_always_releases(bins, monkeypatch):
    tools(bins, "ydotool")
    run("hid.drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 0, "button": "right"})
    assert argvs(bins, "ydotool") == [["mousemove", "--absolute", "-x", "1", "-y", "2"], ["click", "0x41"],
                                      ["mousemove", "--absolute", "-x", "3", "-y", "4"], ["click", "0x81"]]


def test_status(bins):
    tools(bins, "wtype", "ydotool")
    assert run("hid.status") == {"backend": "wtype", "pointer": "ydotool", "platform": "linux"}


def test_status_without_tools(bins):
    bins.only_these()
    out = run("hid.status")
    assert out["backend"] is None and out["pointer"] is None


def ps_seq(bins):
    return [c["env"]["ROOK_SEQ"] for c in bins.calls("powershell")]


def test_windows_keys_clicks_and_drags_are_one_powershell_sequence(bins):
    tools(bins, "powershell")
    assert run("hid.key", {"key": "a", "mods": ["ctrl", "shift"]}, "windows") == {"key": "a", "mods": ["ctrl", "shift"]}
    run("hid.click", {"x": 5, "y": 6, "button": "right"}, "windows")
    run("hid.click", {}, "windows")
    run("hid.drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": "50ms"}, "windows")
    assert ps_seq(bins) == ["k:17:1 k:16:1 k:65:1 k:65:0 k:16:0 k:17:0", "p:5:6 m:8 m:16", "m:2 m:4",
                            "p:1:2 m:2 s:50 p:3:4 m:4"]
    assert "keybd_event" in bins.calls("powershell")[0]["argv"][-1]
    with pytest.raises(KitError, match="unknown key: Print"):
        run("hid.key", {"key": "Print"}, "windows")
    assert run("hid.status", None, "windows") == {"backend": "sendinput", "pointer": "sendinput", "platform": "win32"}


def test_windows_type_and_move(bins):
    tools(bins, "powershell")
    run("hid.type", {"text": "a+b", "enter": True}, "windows")
    run("hid.move", {"x": 7, "y": 8}, "windows")
    type_, move = bins.calls("powershell")
    assert "SendKeys" in type_["argv"][-1] and stdin(bins, "powershell") == "a+b\n"
    assert (move["env"]["ROOK_X"], move["env"]["ROOK_Y"]) == ("7", "8")
