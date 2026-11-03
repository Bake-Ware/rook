"""The phone app's update feed on castling's http door (rook.core2.hub.apkfeed):
/apk.json and /apk with no token, stable or beta, the answers 0.4's site gave."""
from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from rook.core2.hub.apkfeed import ApkFeed
from rook.core2.hub.doors.http import HttpDoor


def drop(data: Path, folder: str, code: int, body: bytes, **over) -> dict:
    d = data / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "rook-worker.apk").write_bytes(body)
    m = {"package": "systems.bake.rook", "version_code": code, "version_name": f"0.4.{code}",
         "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), "url": "https://rook.example/apk"}
    m.update(over)
    (d / "rook-worker-apk.json").write_text(json.dumps(m))
    return m


def test_stable_and_beta_follow_0_4s_rule(tmp_path: Path):
    feed = ApkFeed(tmp_path)
    assert feed.pick() is None and feed.pick("beta") is None                 # nothing dropped: 404
    stable = drop(tmp_path, "apk", 40, b"stable app")
    assert feed.pick()[1] == stable and feed.pick("beta")[1] == stable       # no beta: stable for everyone
    beta = drop(tmp_path, "apk-beta", 48, b"beta app")
    assert feed.pick()[1] == stable and feed.pick("nightly")[1] == stable
    apk, m = feed.pick("beta")
    assert m == beta and apk.read_bytes() == b"beta app"
    drop(tmp_path, "apk", 49, b"newer stable")                               # stable passed the beta
    assert feed.pick("beta")[1]["version_code"] == 49
    (tmp_path / "apk" / "rook-worker.apk").unlink()                          # no stable: the beta alone
    assert feed.pick("beta")[1] == beta and feed.pick() is None


def test_a_file_that_does_not_match_its_manifest_is_not_served(tmp_path: Path):
    feed = ApkFeed(tmp_path)
    drop(tmp_path, "apk", 40, b"stable app", sha256="0" * 64)
    assert feed.pick() is None                                              # wrong hash
    drop(tmp_path, "apk", 40, b"stable app", size=3)
    assert feed.pick() is None                                              # wrong size
    drop(tmp_path, "apk", 40, b"stable app", package="com.other")
    assert feed.pick() is None
    drop(tmp_path, "apk", "40", b"stable app")
    assert feed.pick() is None                                              # version_code not an int
    m = drop(tmp_path, "apk", 40, b"stable app")
    assert feed.pick()[1] == m
    (tmp_path / "apk" / "rook-worker.apk").write_bytes(b"stable apq")       # swapped under it, same size
    assert feed.pick() is None
    hashed = []
    real = hashlib.file_digest
    try:
        hashlib.file_digest = lambda f, alg: hashed.append(1) or real(f, alg)
        assert feed.pick() is None and feed.pick() is None and hashed == []   # a bad file isn't hashed again
    finally:
        hashlib.file_digest = real
    (tmp_path / "apk" / "rook-worker-apk.json").write_text("{" + " " * 20000 + "}")
    assert feed.pick() is None                                              # bigger than the app takes


def test_the_door_serves_the_feed_without_a_token(tmp_path: Path):
    body = bytes(range(256)) * 9000                                         # > one chunk
    stable = drop(tmp_path, "apk", 40, body)
    beta = drop(tmp_path, "apk-beta", 48, b"beta" * 10)
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
            st, _, out = await asyncio.to_thread(get, port, "/apk.json")
            assert st == 200 and json.loads(out) == stable
            st, h, out = await asyncio.to_thread(get, port, "/apk")
            assert st == 200 and out == body and h["Content-Length"] == str(len(body))
            assert h["Content-Type"] == "application/vnd.android.package-archive"
            st, _, out = await asyncio.to_thread(get, port, "/apk.json?channel=beta")
            assert json.loads(out) == beta
            st, _, out = await asyncio.to_thread(get, port, "/apk?channel=beta")
            assert out == b"beta" * 10
            hub.dispatcher.closed_doors.add("http")                         # the kill switch covers it
            st, _, _ = await asyncio.to_thread(get, port, "/apk.json")
            assert st == 503
            hub.dispatcher.closed_doors.clear()
            for f in ("apk", "apk-beta"):
                (tmp_path / f / "rook-worker.apk").unlink()
            st, _, out = await asyncio.to_thread(get, port, "/apk.json")
            assert st == 404 and json.loads(out)["ok"] is False
        finally:
            await door.stop()

    asyncio.run(asyncio.wait_for(go(), 30))
