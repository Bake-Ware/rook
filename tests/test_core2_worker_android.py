"""The 0.5 worker inside the Android app (rook/core2/worker/android.py), off
the phone: a real AndroidWorker on the loopback relay with the phone plugins,
and the entry points the app's service calls from its threads. Hard timeouts
on everything."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.transport import LoopbackRelay, band_of  # noqa: E402
from rook.core2.worker import android  # noqa: E402
from rook.core2.worker.runtime import Config  # noqa: E402

PLUGINS = Path(__file__).resolve().parents[1] / "rook" / "plugins"
PHONE = sorted(p.parent for p in PLUGINS.glob("*/plugin.yaml") if "\nplatforms: [android]\n" in p.read_text())
PSK = "android-worker-band"
ROOT = SigningKey(b"\x31" * 32)


def go(body, seconds=20):
    async def main():
        return await asyncio.wait_for(body(), seconds)
    return asyncio.run(main())


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_ROOT", "/system")   # what every Android app process has
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setitem(sys.modules, "java", None)   # off the phone: hooks that touch Java fail quietly
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": wire.key_id(bytes(ROOT.verify_key)), "root": wire.pub_text(ROOT),
                                  "band": band_of(PSK)}))
    return Config(data=tmp_path / "data", anchor=anchor, name="bakephone", worker_id="5a" * 16,
                  plugins=[*PHONE, PLUGINS / "battery"])


def test_android_worker_serves_the_phone_plugins_and_installs_no_packages(cfg):
    relay = LoopbackRelay()
    cfg.bands = [{"psk": PSK}]

    async def body():
        w = android.AndroidWorker(cfg, make_link=lambda spec: relay.link(spec["psk"]))
        await w.start()
        try:
            phone = {p.name for p in PHONE}
            loaded = {n: p for n, p in w.host.plugins.items() if n in phone}
            assert set(loaded) == phone and all(not p.unavailable for p in loaded.values())
            assert w.host.plugins["battery"].reason.startswith("not on android")
            assert "sms.send" in w.gate.caps and "battery.status" not in w.gate.caps
            with pytest.raises(RuntimeError, match="built into the APK"):
                w.installer.install_python(["websockets>=12"])
            link = w.transport._bands[band_of(PSK)].link
            await w.redial()
            assert w.transport.bands == [band_of(PSK)] and w.transport._bands[band_of(PSK)].link is not link
        finally:
            await w.stop()
    go(body)


def test_the_service_starts_kicks_and_stops_it_from_its_threads(cfg, monkeypatch):
    monkeypatch.setattr(android, "setup_logging", lambda opts: None)   # leaves pytest's log capture alone
    assert android.kick() is False
    t = threading.Thread(target=android.start, args=(cfg,), daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not (android._running and android._running[2].started):
        assert time.monotonic() < deadline and t.is_alive(), "worker didn't start"
        time.sleep(0.02)
    assert android.kick() is True
    android.stop()
    t.join(15)
    assert not t.is_alive() and android._running is None
    assert android.kick() is False


def test_a_stop_while_starting_ends_it_once_up(cfg, monkeypatch):
    monkeypatch.setattr(android, "setup_logging", lambda opts: None)
    starting, release = threading.Event(), threading.Event()
    real_start = android.AndroidWorker.start

    async def slow_start(self):
        starting.set()
        await asyncio.to_thread(release.wait, 10)
        await real_start(self)
    monkeypatch.setattr(android.AndroidWorker, "start", slow_start)
    t = threading.Thread(target=android.start, args=(cfg,), daemon=True)
    t.start()
    assert starting.wait(10)
    android.stop()
    release.set()
    t.join(15)
    assert not t.is_alive() and android._running is None
