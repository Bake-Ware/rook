"""A file a hub plugin hands out once (rook.core2.hub.once): /once/<plugin>/<token>."""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from rook.core2.hub.doors.http import HttpDoor
from rook.core2.hub.once import MAX_AGE, OnceFiles

TOKEN = "0123456789abcdef" * 2


def _put(tmp_path: Path, token: str = TOKEN, body: bytes = b"sound", plugin: str = "remote-voice") -> Path:
    d = tmp_path / "plugins" / plugin / "once"
    d.mkdir(parents=True, exist_ok=True)
    (d / token).write_bytes(body)
    return d / token


def test_a_file_is_taken_once_and_only_by_its_whole_name(tmp_path: Path):
    once = OnceFiles(tmp_path)
    assert once.take("remote-voice", TOKEN) is None                          # no folder yet
    path = _put(tmp_path)
    (tmp_path / "plugins" / "remote-voice" / "last.raw").write_bytes(b"kept")
    for plugin, token in (("remote-voice", "short"), ("remote-voice", TOKEN.upper()), ("..", TOKEN),
                          ("remote-voice", "../last.raw"), ("other", TOKEN), ("remote-voice", TOKEN + ".taken")):
        assert once.take(plugin, token) is None, (plugin, token)
    got = once.take("remote-voice", TOKEN)
    assert got is not None and got.read_bytes() == b"sound" and not path.exists()
    assert once.take("remote-voice", TOKEN) is None                          # the second asker gets nothing
    once.done(got)
    assert not got.exists()
    link = path.with_name("f" * 32)
    link.symlink_to(tmp_path / "plugins" / "remote-voice" / "last.raw")
    assert once.take("remote-voice", "f" * 32) is None


def test_a_file_nobody_fetched_goes_away(tmp_path: Path):
    once = OnceFiles(tmp_path)
    old = _put(tmp_path, "a" * 32)
    os.utime(old, (time.time() - MAX_AGE - 5,) * 2)
    _put(tmp_path, "b" * 32)
    assert once.take("remote-voice", "a" * 32) is None and not old.exists()
    assert once.take("remote-voice", "b" * 32) is not None


def test_the_door_serves_it_once_without_a_token(tmp_path: Path):
    body = bytes(range(256)) * 5000                                         # > one chunk
    _put(tmp_path, body=body)
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
            st, _, out = await asyncio.to_thread(get, port, f"/once/remote-voice/{TOKEN[:-1]}")
            assert st == 404 and json.loads(out)["ok"] is False
            st, h, out = await asyncio.to_thread(get, port, f"/once/remote-voice/{TOKEN}")
            assert st == 200 and out == body and h["Content-Length"] == str(len(body))
            st, _, _ = await asyncio.to_thread(get, port, f"/once/remote-voice/{TOKEN}")
            assert st == 404
            assert list((tmp_path / "plugins" / "remote-voice" / "once").iterdir()) == []
        finally:
            await door.stop()

    asyncio.run(asyncio.wait_for(go(), 30))
