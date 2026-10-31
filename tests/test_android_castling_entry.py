"""The Android app's Rook 0.5 entry (android/app/src/main/python/castling_entry.py)
and the APK staging it relies on (android/stage_worker.py), off the phone.
tests/e2e/test_phone_app_e2e.py runs the whole sign-in against a real hub."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ANDROID = Path(__file__).resolve().parents[1] / "android"
# Appended: a local APK build stages a copy of rook/ there, and the repo's must win.
sys.path.append(str(ANDROID / "app" / "src" / "main" / "python"))
sys.path.append(str(ANDROID))

import castling_entry  # noqa: E402
import stage_worker  # noqa: E402
from rook.core2.enroll import device  # noqa: E402


@pytest.fixture(scope="module")
def plugins_zip():
    return stage_worker.plugins_zip(stage_worker.phone_plugins())


def test_the_apk_carries_every_android_plugin_and_the_same_zip_twice(plugins_zip):
    folders = stage_worker.phone_plugins()
    names = {f.name for f in folders}
    assert len(folders) >= 17 and {"sms", "call", "battery-android", "voice-android"} <= names
    assert "battery" not in names and "shell" not in names
    assert stage_worker.plugins_zip(folders) == plugins_zip     # unpacked again only when it changes


def test_plugins_unpack_once_and_again_when_the_apk_changes(tmp_path, plugins_zip):
    files = str(tmp_path)
    target = castling_entry.unpack_plugins(files, plugins_zip)
    assert (target / "sms" / "plugin.yaml").is_file() and target == tmp_path / "rook05" / "plugins"
    (target / "left-alone").write_text("x")
    castling_entry.unpack_plugins(files, plugins_zip)
    assert (target / "left-alone").exists()
    other = stage_worker.plugins_zip([p for p in stage_worker.phone_plugins() if p.name != "sms"])
    castling_entry.unpack_plugins(files, other)
    assert not (target / "left-alone").exists() and not (target / "sms").exists()
    assert not (tmp_path / "rook05" / "plugins.new").exists()


def test_status_before_sign_in_writes_nothing(tmp_path):
    assert json.loads(castling_entry.status(str(tmp_path))) == {"enrolled": False}
    assert not (tmp_path / "rook05").exists()


def test_sign_in_refuses_plain_http_off_the_phone_and_a_second_home(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "_post", lambda *a: pytest.fail("no request expected"))
    got = json.loads(castling_entry.enroll(str(tmp_path), "http://192.168.1.5:8750", "phone"))
    assert not got["ok"] and "plain http" in got["error"]
    (tmp_path / "rook05" / "anchor.json").write_text(json.dumps({"hub": "0123456789abcdef"}))
    got = json.loads(castling_entry.enroll(str(tmp_path), "rook.example", "phone"))
    assert not got["ok"] and "already hub 0123456789abcdef" in got["error"]
    assert json.loads(castling_entry.status(str(tmp_path)))["enrolled"] is True


def test_a_waiting_sign_in_shows_the_code_and_cancels(tmp_path, monkeypatch):
    def post(url, path, body):
        if path == "/enroll/device":
            return {"ok": True, "device_code": "d" * 32, "user_code": "BCDF-GHJK", "hub": "0123456789abcdef"}
        return {"ok": True, "pending": True, "interval": 1}
    monkeypatch.setattr(device, "_post", post)
    said, out = [], []

    class Listener:
        def say(self, text):
            said.append(text)

    t = threading.Thread(target=lambda: out.append(castling_entry.enroll(str(tmp_path), "rook.example", "phone",
                                                                         Listener())), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not said:
        assert time.monotonic() < deadline, "no code shown"
        time.sleep(0.02)
    assert "approve code BCDF-GHJK on hub 0123456789abcdef" in said[0]
    castling_entry.cancel()
    t.join(10)
    assert not t.is_alive() and json.loads(out[0]) == {"ok": False, "error": "sign-in cancelled"}


def test_the_worker_wont_start_before_sign_in(tmp_path):
    with pytest.raises(RuntimeError, match="isn't signed in"):
        castling_entry.start(str(tmp_path))
