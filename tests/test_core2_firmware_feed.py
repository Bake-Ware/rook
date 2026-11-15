"""Firmware for the band's small devices on the hub's http door
(rook.core2.hub.firmware): /firmware.json and /firmware/<name>.bin with no token."""
from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from rook.core2.hub.doors.http import HttpDoor
from rook.core2.hub.firmware import FirmwareFeed


def test_only_bin_files_by_plain_names_are_served(tmp_path: Path):
    feed = FirmwareFeed(tmp_path)
    assert feed.listing() == {"files": []}                                   # no folder yet
    d = tmp_path / "firmware"
    d.mkdir()
    (d / "remote-bridge-0.8.0.bin").write_bytes(b"build")
    (d / "notes.txt").write_text("not firmware")
    (d / ".hidden.bin").write_bytes(b"x")
    (tmp_path / "secret.bin").write_bytes(b"outside")
    (d / "link.bin").symlink_to(tmp_path / "secret.bin")
    got = feed.listing()["files"]
    assert [f["name"] for f in got] == ["remote-bridge-0.8.0.bin"]
    assert got[0]["sha256"] == hashlib.sha256(b"build").hexdigest() and got[0]["size"] == 5
    assert feed.file("remote-bridge-0.8.0.bin").read_bytes() == b"build"
    for bad in ("notes.txt", ".hidden.bin", "link.bin", "../secret.bin", "a/b.bin", "", "missing.bin"):
        assert feed.file(bad) is None


def test_the_door_serves_firmware_without_a_token(tmp_path: Path):
    body = bytes(range(256)) * 5000                                         # > one chunk
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "remote-bridge.bin").write_bytes(body)
    (tmp_path / "secret.bin").write_bytes(b"outside")
    hub = SimpleNamespace(data=tmp_path, dispatcher=SimpleNamespace(closed_doors=set()))

    def get(port, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    async def go():
        door = HttpDoor(hub, {"bind": "127.0.0.1:0", "web": False})
        await door.start()
        try:
            port = door.port
            st, _, out = await asyncio.to_thread(get, port, "/firmware.json")
            files = json.loads(out)["files"]
            assert st == 200 and files[0]["name"] == "remote-bridge.bin"
            assert files[0]["sha256"] == hashlib.sha256(body).hexdigest()
            st, h, out = await asyncio.to_thread(get, port, "/firmware/remote-bridge.bin")
            assert st == 200 and out == body and h["Content-Length"] == str(len(body))
            assert h["Content-Type"] == "application/octet-stream"
            for path in ("/firmware/missing.bin", "/firmware/..%2Fsecret.bin", "/firmware/%2e%2e%2fsecret.bin"):
                st, _, out = await asyncio.to_thread(get, port, path)
                assert st == 404 and json.loads(out)["ok"] is False, path
            hub.dispatcher.closed_doors.add("http")                         # the kill switch covers it
            st, _, _ = await asyncio.to_thread(get, port, "/firmware.json")
            assert st == 503
        finally:
            await door.stop()

    asyncio.run(asyncio.wait_for(go(), 30))
