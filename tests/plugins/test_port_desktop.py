"""notification-desktop and screen-desktop, with fake notify-send /
powershell and fake capture tools."""

import base64

import pytest

from port_helpers import on_worker
from rook.kit.ctx import KitError

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
# A fake capture tool: writes JPEG to its last argument.
WRITE_JPEG = f"""
open(argv[-1], "wb").write({JPEG!r})
"""


def run(plugin, cap, args=None, os_name="linux", hardware=("display",)):
    async def body():
        host = Host(os_name=os_name, hardware=hardware)
        await host.load(PLUGINS / plugin)
        try:
            return await host.call(cap, args or {}, ME)
        finally:
            await host.close()
    return go(body)


# -- notification-desktop (on the hub, for a worker with only shell.exec) ------

def notify(args, os_name="linux"):
    return on_worker("notification-desktop", "notification-desktop.notification.post", args, os_name=os_name)


def test_notify_send_takes_text_as_data(bins):
    bins.add("notify-send")
    notify({"text": "--urgency=critical $(reboot)\nline 2", "title": "-x"})
    _, seen = notify({"text": "plain"})
    (one, two) = bins.calls("notify-send")
    assert one["argv"] == ["-a", "rook", "--", "-x", "--urgency=critical $(reboot)\nline 2"]
    assert two["argv"] == ["-a", "rook", "--", "Rook", "plain"]
    assert [c for c, _ in seen] == ["shell.exec"]


def test_unknown_args_are_refused(bins):
    bins.add("notify-send")
    with pytest.raises(KitError, match="'sender' was unexpected"):
        notify({"text": "hi", "sender": "ops"})


def test_windows_reads_the_text_from_the_environment(bins):
    bins.add("powershell")
    msg = {"text": 'say "hi"; do shell script "rm -rf ~"', "title": "T'itle"}
    notify(msg, "windows")
    (win,) = bins.calls("powershell")
    assert win["env"]["ROOK_TEXT"] == msg["text"] and win["env"]["ROOK_TITLE"] == msg["title"]
    assert msg["text"] not in " ".join(win["argv"]) and "$env:ROOK_TEXT" in win["argv"][-1]


def test_no_desktop_is_the_tools_own_failure(bins):
    bins.add("notify-send", "print('cannot open display', file=sys.stderr); sys.exit(1)")
    with pytest.raises(KitError, match="^notify-send exited 1: cannot open display$"):
        notify({"text": "x"})


# -- screen-desktop (on the hub, for a worker with only shell.exec and file.read)

def shot(args=None, os_name="linux"):
    return on_worker("screen-desktop", "screen-desktop.screen.capture", args, os_name=os_name)


def is_jpeg_file(res):
    assert res["mime"] == "image/jpeg" and res["name"] == "s.jpg"
    assert base64.b64decode(res["data"]) == JPEG and res["bytes"] == len(JPEG)


def test_grim_first_with_quality_and_region(bins):
    bins.add("grim", WRITE_JPEG)
    is_jpeg_file(shot()[0])
    is_jpeg_file(shot({"quality": 40, "x": 10, "y": 20, "w": 300, "h": 200})[0])
    res, seen = shot({"x": 10, "y": 20})   # half a region: whole screen
    is_jpeg_file(res)
    a, b, c = (call["argv"] for call in bins.calls("grim"))
    assert a[:4] == ["-t", "jpeg", "-q", "85"] and len(a) == 5
    assert b[:6] == ["-t", "jpeg", "-q", "40", "-g", "10,20 300x200"]
    assert c == a[:4] + [c[-1]] and c[-1].startswith("/tmp/rook-shot-")
    # the tool, the read back, the temp file removed
    assert [cap for cap, _ in seen] == ["shell.exec", "file.read", "shell.exec"]
    assert seen[1][1] == {"path": c[-1], "encoding": "base64"} and seen[2][1]["argv"] == ["rm", "-f", c[-1]]


def test_a_failing_tool_hands_over_to_the_next(bins):
    bins.add("grim", "print('compositor doesn\\'t support wlr-screencopy'); sys.exit(1)")
    bins.add("scrot", WRITE_JPEG)
    bins.only_these("rm")
    is_jpeg_file(shot({"x": 0, "y": 0, "w": 5, "h": 5})[0])
    assert len(bins.calls("grim")) == 1
    assert bins.calls("scrot")[0]["argv"][:5] == ["-q", "85", "-o", "-a", "0,0,5,5"]


def test_no_capture_tool(bins):
    bins.only_these("rm")
    with pytest.raises(KitError, match="^not installed on this worker: grim, scrot, spectacle, "
                                       "gnome-screenshot, import$"):
        shot()


def test_last_failure_is_the_error(bins):
    bins.only_these("rm")
    bins.add("import", "print('import: unable to open X server', file=sys.stderr); sys.exit(1)")
    with pytest.raises(KitError, match="^import exited 1: import: unable to open X server$"):
        shot()


def test_windows_powershell_prints_the_jpeg(bins):
    bins.add("powershell", f"print(__import__('base64').b64encode({JPEG!r}).decode())")
    res, seen = shot(None, "windows")
    is_jpeg_file(res)
    assert [cap for cap, _ in seen] == ["shell.exec"]
    assert "CopyFromScreen" in bins.calls("powershell")[0]["argv"][-1]
