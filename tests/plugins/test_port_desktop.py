"""notification-desktop and screen-desktop through the worker host, with fake
notify-send / osascript / powershell and fake capture tools."""

import base64

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
# A fake capture tool: writes JPEG to its last argument (or $ROOK_OUT).
WRITE_JPEG = f"""
out = os.environ.get("ROOK_OUT") or argv[-1]
open(out, "wb").write({JPEG!r})
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


# -- notification-desktop -------------------------------------------------------

def notify(args, os_name="linux"):
    return run("notification-desktop", "notification-desktop.notification.post", args, os_name)


def test_notify_send_takes_text_as_data(bins):
    bins.add("notify-send")
    notify({"text": "--urgency=critical $(reboot)\nline 2", "title": "-x"})
    notify({"text": "plain"})
    (one, two) = bins.calls("notify-send")
    assert one["argv"] == ["-a", "rook", "--", "-x", "--urgency=critical $(reboot)\nline 2"]
    assert two["argv"] == ["-a", "rook", "--", "Rook", "plain"]


def test_unknown_args_are_refused(bins):
    bins.add("notify-send")
    with pytest.raises(CallError, match="'sender' was unexpected"):
        notify({"text": "hi", "sender": "ops"})


def test_macos_and_windows_read_the_text_from_the_environment(bins):
    bins.add("osascript")
    bins.add("powershell")
    msg = {"text": 'say "hi"; do shell script "rm -rf ~"', "title": "T'itle"}
    notify(msg, "macos")
    notify(msg, "windows")
    (mac,), (win,) = bins.calls("osascript"), bins.calls("powershell")
    for call in (mac, win):
        assert call["env"]["ROOK_TEXT"] == msg["text"]
        assert call["env"]["ROOK_TITLE"] == msg["title"]
        assert msg["text"] not in " ".join(call["argv"])
    assert mac["argv"][0] == "-e" and 'system attribute "ROOK_TEXT"' in mac["argv"][1]
    assert "$env:ROOK_TEXT" in win["argv"][-1]


def test_notify_needs_a_display(bins):
    bins.add("notify-send")
    with pytest.raises(CallError, match="^unavailable: .*missing hardware: display"):
        run("notification-desktop", "notification-desktop.notification.post", {"text": "x"},
            hardware=())
    assert bins.calls("notify-send") == []


# -- screen-desktop ---------------------------------------------------------------

def shot(args=None, os_name="linux"):
    return run("screen-desktop", "screen-desktop.screen.capture", args, os_name)


def is_jpeg_file(res):
    assert res["mime"] == "image/jpeg" and res["name"] == "s.jpg"
    assert base64.b64decode(res["data"]) == JPEG and res["bytes"] == len(JPEG)


def test_grim_first_with_quality_and_region(bins):
    bins.add("grim", WRITE_JPEG)
    is_jpeg_file(shot())
    is_jpeg_file(shot({"quality": 40, "x": 10, "y": 20, "w": 300, "h": 200}))
    is_jpeg_file(shot({"x": 10, "y": 20}))   # half a region: whole screen
    a, b, c = (call["argv"] for call in bins.calls("grim"))
    assert a[:4] == ["-t", "jpeg", "-q", "85"] and len(a) == 5
    assert b[:6] == ["-t", "jpeg", "-q", "40", "-g", "10,20 300x200"]
    assert c == a[:4] + [c[-1]]


def test_a_failing_tool_hands_over_to_the_next(bins):
    bins.add("grim", "print('compositor doesn\\'t support wlr-screencopy'); sys.exit(1)")
    bins.add("scrot", WRITE_JPEG)
    is_jpeg_file(shot({"x": 0, "y": 0, "w": 5, "h": 5}))
    assert len(bins.calls("grim")) == 1
    assert bins.calls("scrot")[0]["argv"][:5] == ["-q", "85", "-o", "-a", "0,0,5,5"]


def test_no_capture_tool(bins):
    bins.only_these()
    with pytest.raises(CallError, match="^not installed on this worker: grim, scrot, spectacle, "
                                        "gnome-screenshot, import$"):
        shot()


def test_last_failure_is_the_error(bins):
    bins.only_these()
    bins.add("import", "print('import: unable to open X server', file=sys.stderr); sys.exit(1)")
    with pytest.raises(CallError, match="unable to open X server"):
        shot()


def test_macos_screencapture(bins):
    bins.add("screencapture", WRITE_JPEG)
    is_jpeg_file(shot({"x": 1, "y": 2, "w": 3, "h": 4}, "macos"))
    assert bins.calls("screencapture")[0]["argv"][:5] == ["-x", "-t", "jpg", "-R", "1,2,3,4"]


def test_windows_powershell_writes_rook_out(bins):
    bins.add("powershell", WRITE_JPEG)
    is_jpeg_file(shot(None, "windows"))
    call = bins.calls("powershell")[0]
    assert call["env"]["ROOK_OUT"].endswith("s.jpg") and "CopyFromScreen" in call["argv"][-1]

