"""The Rust worker updates itself (docs/castling-updates.md, "A one-file
worker"): a real hub signs a folder of built files and rolls it out over the
band; the worker, run by a supervisor as a service manager would, pulls it,
checks the signature chain from its anchor, restarts into it on trial and
confirms. A release that won't start is dropped after three boots, and
`worker.update {version}` switches back by hand."""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("telesthete")
from rook.core2 import wire  # noqa: E402
from rook.core2.hub.runtime import Config, Hub  # noqa: E402
from rook.core2.transport import band_of  # noqa: E402

from test_core2_minimal_worker import Relay, door, until  # noqa: E402
from test_core2_rust_worker import binary, enrolled  # noqa: E402,F401  (the fixture)

SUPERVISE = Path(__file__).resolve().parent / "e2e" / "supervise.py"
ARTIFACT = "rook-worker-linux-x86_64"


@pytest.mark.skipif(not sys.platform.startswith("linux") or __import__("platform").machine() != "x86_64",
                    reason="the release here is built for linux-x86_64")
def test_the_rust_worker_takes_a_release_and_drops_one_that_will_not_start(tmp_path, binary):  # noqa: F811
    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}))
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        config = enrolled(tmp_path / "w", psk, addr, secrets.token_hex(16))
        (tmp_path / "w" / "anchor.json").write_text(json.dumps(
            {"hub": hub.hub_id, "root": wire.pub_text(hub.root), "band": band_of(psk)}))
        base = tmp_path / "bin" / "rook-worker"          # the installed binary: never overwritten
        base.parent.mkdir()
        shutil.copy2(binary, base)
        proc = await asyncio.create_subprocess_exec(sys.executable, str(SUPERVISE), str(base), "--config", str(config))

        async def call(worker, cap, **args):
            return await door(hub, token, "/call", {"worker": worker, "cap": cap, "args": args,
                                                    "why": "Test core updates of the Rust worker."})

        async def me():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "rusty" and x["online"]), None)

        async def on(version):
            async def check():
                w = await me()
                return w if w and w["version"] == version else None
            return await until(check, 60)

        async def rollout(source, version):
            out = await call("rook", "update.rollout", source=str(source), version=version)
            assert out["ok"], out

            async def ended():
                ro = (await call("rook", "update.status"))["result"]["rollout"]
                return ro if ro["status"] != "running" else None
            return await until(ended, 120)

        def release(name, data):
            folder = tmp_path / name
            folder.mkdir()
            (folder / ARTIFACT).write_bytes(data)
            return folder
        try:
            w = await on("0.5.0")
            assert w["facts"]["artifact"] == ARTIFACT, w

            # A second build (the same program with bytes appended: it runs, its hash differs).
            ro = await rollout(release("good", base.read_bytes() + b"\0second build"), "0.5.1")
            assert ro["status"] == "done", ro
            await on("0.5.1")
            rel = (await call("rusty", "worker.status"))["result"]["release"]
            assert (rel["running"], rel["current"], rel["previous"], rel["trial"]) == ("0.5.1", "0.5.1", None, None), rel
            assert base.read_bytes() == binary.read_bytes()

            # One that never comes up: three boots under the supervisor, then 0.5.1 again.
            ro = await rollout(release("broken", b"#!/bin/sh\nexit 3\n"), "0.5.2")
            assert ro["status"] == "failed", ro
            assert "did not come up" in ro["workers"][0]["reason"], ro
            await on("0.5.1")
            rel = (await call("rusty", "worker.status"))["result"]["release"]
            assert (rel["running"], rel["failed"]["version"]) == ("0.5.1", "0.5.2"), rel

            # Back to the installed binary by hand, then to the kept 0.5.1.
            out = await call("rusty", "worker.update", version="installed")
            assert out["ok"] and out["result"]["from"] == "0.5.1", out
            await on("0.5.0")
            out = await call("rusty", "worker.update", version="0.5.1")
            assert out["ok"], out
            await on("0.5.1")

            # A release of the hub's own source code isn't offered to it.
            out = await call("rook", "update.rollout", version="0.5.3", source=str(Path(__file__).resolve().parents[1]))
            assert not out["ok"] and f"0.5.3 has no {ARTIFACT}" in out["error"], out
        finally:
            proc.terminate()
            await proc.wait()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 300))
