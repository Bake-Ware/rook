"""camera on the hub, for a worker with only the primitives, with fake
fswebcam / ffmpeg / termux-camera-photo; camera.list's Linux script run
against a stand-in /dev and /sys."""

import base64
import shutil
import subprocess

import pytest

from port_helpers import PLUGINS, on_worker
from rook.kit.ctx import KitError

JPEG = b"\xff\xd8\xff\xe0fake-cam\xff\xd9"
WRITE_JPEG = f"""
open(argv[-1], "wb").write({JPEG!r})
"""


def run(cap, args=None, os_name="linux"):
    return on_worker("camera", cap, args, os_name=os_name)


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setenv("TMPDIR", str(folder))
    monkeypatch.setenv("TEMP", str(folder))
    return folder


def test_capture_with_fswebcam(bins, tmp_env):
    bins.add("fswebcam", WRITE_JPEG)
    res, seen = run("camera.capture", {"camera": 2, "quality": 70})
    assert base64.b64decode(res["data"]) == JPEG and res["mime"] == "image/jpeg" and res["name"] == "c.jpg"
    argv = bins.calls("fswebcam")[0]["argv"]
    assert argv[:8] == ["-d", "/dev/video2", "--no-banner", "-q", "-r", "1280x720", "--jpeg", "70"]
    assert argv[-1].startswith(f"{tmp_env}/rook-cam-") and argv[-1].endswith(".jpg")
    # where to write, the tool, the read back, the temp file removed
    assert [c for c, _ in seen] == ["shell.env", "shell.exec", "file.read", "shell.exec"]
    assert seen[-1][1]["argv"] == ["rm", "-f", argv[-1]] and list(tmp_env.iterdir()) == []


def test_falls_back_to_ffmpeg_when_fswebcam_fails(bins, tmp_env):
    bins.add("fswebcam", "sys.exit(1)")
    bins.add("ffmpeg", WRITE_JPEG)
    res, _ = run("camera.capture", {"resolution": "640x480"})
    assert base64.b64decode(res["data"]) == JPEG
    argv = bins.calls("ffmpeg")[0]["argv"]
    assert argv[argv.index("-i") + 1] == "/dev/video0" and argv[argv.index("-video_size") + 1] == "640x480"


def test_no_tool_is_a_clean_error(bins, tmp_env):
    bins.only_these("rm")
    with pytest.raises(KitError, match="^not installed on this worker: fswebcam, ffmpeg$"):
        run("camera.capture")


def test_termux_and_windows(bins, tmp_env):
    bins.add("termux-camera-photo", WRITE_JPEG)
    run("camera.capture", {"camera": 1}, os_name="termux")
    assert bins.calls("termux-camera-photo")[0]["argv"][:2] == ["-c", "1"]
    bins.add("ffmpeg", WRITE_JPEG)
    bins.add("cmd")
    _, seen = run("camera.capture", {"device": "USB Cam"}, os_name="windows")
    argv = bins.calls("ffmpeg")[0]["argv"]
    assert argv[argv.index("-f") + 1] == "dshow" and argv[argv.index("-i") + 1] == "video=USB Cam"
    assert argv[-1].startswith(f"{tmp_env}\\rook-cam-")
    assert seen[-1][1]["argv"] == ["cmd", "/c", "del", "/q", argv[-1]]


@pytest.mark.parametrize("args", [{"resolution": "1280x720 -vf x"}, {"device": 'a" -i "b'},
                                  {"device": "video=x:audio=y"}, {"camera": -1}])
def test_args_are_checked(bins, args):
    bins.add("fswebcam", WRITE_JPEG)
    with pytest.raises(KitError, match="bad args"):
        run("camera.capture", args)
    assert bins.calls("fswebcam") == []


def test_list_on_linux(bins):
    bins.add("sh", r"print('10\t/dev/video10'); print('0\tIntegrated Camera'); print('1\tIntegrated Camera')")
    got, seen = run("camera.list")
    assert got == [{"camera": 0, "name": "Integrated Camera"}, {"camera": 1, "name": "Integrated Camera"},
                   {"camera": 10, "name": "/dev/video10"}]
    assert seen[0][1]["argv"][:2] == ["sh", "-c"]


def test_linux_list_script(tmp_path):
    # The script itself, pointed at a stand-in /dev and /sys.
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    import importlib.util
    spec = importlib.util.spec_from_file_location("camera_script", PLUGINS / "camera" / "scripts" / "camera.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dev, sys_ = tmp_path / "dev", tmp_path / "sys"
    dev.mkdir()
    for n in ("video0", "video10", "videox"):
        (dev / n).touch()
    (sys_ / "video0").mkdir(parents=True)
    (sys_ / "video0" / "name").write_text("Integrated Camera\n")
    script = mod.LINUX_LIST.replace("/dev/video", f"{dev}/video").replace("/sys/class/video4linux", str(sys_))
    out = subprocess.run([sh, "-c", script], capture_output=True, text=True, timeout=10)
    assert sorted(out.stdout.splitlines()) == ["0\tIntegrated Camera", f"10\t{dev}/video10"]


def test_list_on_windows_and_termux(bins):
    bins.add("ffmpeg", r"""sys.stderr.write('[dshow @ 0] "USB Cam" (video)\n[dshow @ 0] "Mic" (audio)\n'); sys.exit(1)""")
    assert run("camera.list", os_name="windows")[0] == [{"camera": 0, "name": "USB Cam"}]
    bins.add("termux-camera-info", 'print(\'[{"id": "0", "facing": "back"}]\')')
    assert run("camera.list", os_name="termux")[0] == [{"camera": 0, "name": "back", "facing": "back"}]
    (bins.dir / "termux-camera-info").unlink()
    (bins.dir / "ffmpeg").unlink()
    bins.only_these()
    assert run("camera.list", os_name="termux")[0][0]["name"] == "back"   # no termux-api: the default pair
    with pytest.raises(KitError, match="ffmpeg is needed"):
        run("camera.list", os_name="windows")
