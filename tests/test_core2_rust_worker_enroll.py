"""`rook-worker enroll` against a real hub, with no Python on the device's
side: with a one-time code, and by asking and waiting for a Join tap. Each
time the worker then starts from the files it wrote and comes online."""
from __future__ import annotations

import asyncio
import json
import secrets

import pytest
import yaml

pytest.importorskip("telesthete")
from rook.core2.hub.runtime import Config, Hub  # noqa: E402

from test_core2_minimal_worker import Relay, door, until  # noqa: E402
from test_core2_rust_worker import binary  # noqa: E402,F401  (the fixture)


def test_the_rust_worker_enrolls_with_a_code_and_with_a_tap(tmp_path, binary):  # noqa: F811
    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}},
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": secrets.token_hex(24), "relay": addr}]}))
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        url = f"http://127.0.0.1:{hub.doors[0].port}"
        running = []

        async def call(cap, **args):
            return await door(hub, token, "/call", {"worker": "rook", "cap": cap, "args": args,
                                                    "why": "Test enrolling the Rust worker."})

        async def online(name):
            async def check():
                got = (await door(hub, token, "/workers"))["workers"]
                return next((w for w in got if w["name"] == name and w["online"]), None)
            return await until(check, 30)

        async def start(config):
            running.append(await asyncio.create_subprocess_exec(str(binary), "--config", str(config)))

        try:
            # With a one-time code (what "Add this computer" and an agent with a token do).
            code = (await door(hub, token, "/enroll/code", {}))["code"]
            coded = tmp_path / "coded" / "worker.yaml"
            coded.parent.mkdir()
            coded.write_text("log: {level: info}\n")              # kept: enrollment only adds its keys
            p = await asyncio.create_subprocess_exec(str(binary), "--config", str(coded), "enroll", url,
                                                     "--code", code, "--name", "coded",
                                                     stdout=asyncio.subprocess.PIPE)
            out, _ = await p.communicate()
            assert p.returncode == 0 and b"Enrolled coded" in out, out
            raw = yaml.safe_load(coded.read_text())
            assert raw["log"] == {"level": "info"} and raw["name"] == "coded" and raw["data"] == "data"
            anchor = json.loads((coded.parent / "anchor.json").read_text())
            assert anchor["hub"] == hub.hub_id and anchor["root"].startswith("ed25519:")
            bands = json.loads((coded.parent / "data" / "bands.json").read_text())
            assert list(bands) == [anchor["band"]] and bands[anchor["band"]]["relay"] == addr
            assert json.loads((coded.parent / "data" / "enrollment.json").read_text())["owner"] == "person:bake"
            again = await asyncio.create_subprocess_exec(str(binary), "--config", str(coded), "enroll", url,
                                                         stderr=asyncio.subprocess.PIPE)
            _, err = await again.communicate()
            assert again.returncode == 1 and b"already hub" in err, err
            await start(coded)
            assert (await online("coded"))["id"] == raw["worker_id"]

            # Without one: it asks, waits, and someone taps Join on the Workers page.
            asked = tmp_path / "asked" / "worker.yaml"
            p = await asyncio.create_subprocess_exec(str(binary), "--config", str(asked), "enroll", url,
                                                     "--name", "asked", stdout=asyncio.subprocess.PIPE)

            async def waiting():
                return (await call("hub.joins"))["result"]["devices"]
            device = (await until(waiting, 20))[0]
            assert device["name"] == "asked"
            assert (await call("hub.join", request=device["id"]))["ok"]
            out, _ = await asyncio.wait_for(p.communicate(), 20)
            assert p.returncode == 0, out
            assert b"asked is waiting for someone to let this machine in on the Workers page" in out, out
            await start(asked)
            await online("asked")
        finally:
            for proc in running:
                proc.terminate()
                await proc.wait()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 120))
