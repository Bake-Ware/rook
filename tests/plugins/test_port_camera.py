"""camera through the worker host, with fake fswebcam / ffmpeg /
termux-camera-photo, and camera.list on Linux sysfs names."""

import base64

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host

JPEG = b"\xff\xd8\xff\xe0fake-cam\xff\xd9"
WRITE_JPEG = f"""
open(argv[-1], "wb").write({JPEG!r})
"""


def run(cap, args=None, os_name="linux"):
    async def body():
        host = Host(os_name=os_name)
        await host.load(PLUGINS / "camera")
        try:
            return await host.call(cap, args or {}, ME)
        finally:
            await host.close()
    return go(body)


def test_capture_with_fswebcam(bins):
    bins.add("fswebcam", WRITE_JPEG)
    res = run("camera.capture", {"camera": 2, "quality": 70})
    assert base64.b64decode(res["data"]) == JPEG and res["mime"] == "image/jpeg"
    argv = bins.calls("fswebcam")[0]["argv"]
    assert argv[:8] == ["-d", "/dev/video2", "--no-banner", "-q", "-r", "1280x720", "--jpeg", "70"]
    assert argv[-1].endswith("/c.jpg")


def test_falls_back_to_ffmpeg_when_fswebcam_fails(bins):
    bins.add("fswebcam", "sys.exit(1)")
    bins.add("ffmpeg", WRITE_JPEG)
    res = run("camera.snap", {"resolution": "640x480"})
    assert base64.b64decode(res["data"]) == JPEG
    argv = bins.calls("ffmpeg")[0]["argv"]
    assert argv[argv.index("-i") + 1] == "/dev/video0" and argv[argv.index("-video_size") + 1] == "640x480"


def test_no_tool_is_a_clean_error(bins):
    bins.only_these()
    with pytest.raises(CallError, match="ffmpeg"):
        run("camera.capture")


def test_termux_and_windows(bins):
    bins.add("termux-camera-photo", WRITE_JPEG)
    run("camera.capture", {"camera": 1}, os_name="termux")
    assert bins.calls("termux-camera-photo")[0]["argv"][:2] == ["-c", "1"]
    bins.add("ffmpeg", WRITE_JPEG)
    run("camera.capture", {"device": "USB Cam"}, os_name="windows")
    argv = bins.calls("ffmpeg")[0]["argv"]
    assert argv[argv.index("-f") + 1] == "dshow" and argv[argv.index("-i") + 1] == "video=USB Cam"


@pytest.mark.parametrize("args", [{"resolution": "1280x720 -vf x"}, {"device": 'a" -i "b'},
                                  {"device": "video=x:audio=y"}, {"camera": -1}])
def test_args_are_checked(bins, args):
    bins.add("fswebcam", WRITE_JPEG)
    with pytest.raises(CallError, match="bad args"):
        run("camera.capture", args)
    assert bins.calls("fswebcam") == []


def test_list_on_linux(tmp_path, monkeypatch):
    # /dev and /sys are the machine's: stand in for both.
    import glob as glob_mod
    import builtins
    devs = ["/dev/video10", "/dev/video0", "/dev/video1"]
    names = {"/sys/class/video4linux/video0/name": "Integrated Camera\n",
             "/sys/class/video4linux/video1/name": "Integrated Camera\n"}
    real_glob, real_open = glob_mod.glob, builtins.open
    monkeypatch.setattr(glob_mod, "glob", lambda p, **k: devs if p == "/dev/video*" else real_glob(p, **k))

    def fake_open(path, *a, **k):
        if str(path).startswith("/sys/class/video4linux/"):
            if path in names:
                f = tmp_path / "n"
                f.write_text(names[path])
                return real_open(f, *a, **k)
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    got = run("camera.list")
    assert got == [{"camera": 0, "name": "Integrated Camera"}, {"camera": 1, "name": "Integrated Camera"},
                   {"camera": 10, "name": "/dev/video10"}]
