"""The real Host behind the real Installer (rook.core2.worker.install): hub
prepare/sign/push over the in-memory transport, then load, checks, updates
and rollback on the host."""

import asyncio

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.hub import distribute as d  # noqa: E402
from rook.core2.worker import install as w  # noqa: E402
from rook.core2.worker.host import Host  # noqa: E402
from rook.kit.ctx import Caller  # noqa: E402

HUB_SK = SigningKey(b"\x02" * 32)
HUB_ID = wire.key_id(bytes(HUB_SK.verify_key))
WID = "3f0a" * 8
ENV = d.WorkerEnv(worker_id=WID, os="linux", python="3.11", packages={})
ME = Caller(person="bake", band="b1", why="test")


def go(body, timeout=15):
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


def plugin(root, name, version, *, answer="ok"):
    folder = root / f"src-{name}-{version}"
    (folder / "scripts").mkdir(parents=True)
    (folder / "plugin.yaml").write_text(
        f"manifest: 1\nplugin: {name}\nversion: {version}\n"
        "caps:\n  ping:\n    doc: Answer.\n    risk: read\n    scope: self\n    run: scripts/main.py:ping\n"
        f"check: [{name}.ping]\n")
    body = "def ping(ctx):\n    return {'answer': ANSWER, 'version': VERSION}\n"
    if answer == "fail":
        body = "def ping(ctx):\n    raise ctx.Error('device not answering')\n"
    (folder / "scripts" / "main.py").write_text(f"ANSWER = {answer!r}\nVERSION = {version!r}\n" + body)
    return folder


async def ship(folder, inst):
    transport = d.MemoryTransport({WID: inst})
    return await d.install(folder, ENV, transport, signer=HUB_SK, hub=HUB_ID, resolve=lambda r, e: [])


def test_installer_drives_the_real_host(tmp_path):
    async def body():
        host = Host(os_name="linux")
        keys = {HUB_ID: wire.pub_text(HUB_SK)}
        inst = w.Installer(tmp_path / "plugins", worker_id=WID, host=host,
                           keys=lambda hub: keys if hub == HUB_ID else {}, approves=lambda hub: hub == HUB_ID)

        assert (await ship(plugin(tmp_path, "tv", "1.0.0"), inst))["ok"]
        # Same version, same script name, another plugin: install's
        # <plugin>/<version>/ layout must not mix their modules up.
        assert (await ship(plugin(tmp_path, "lamp", "1.0.0", answer="lamp"), inst))["ok"]
        assert await host.call("tv.ping", {}, ME) == {"answer": "ok", "version": "1.0.0"}
        assert await host.call("lamp.ping", {}, ME) == {"answer": "lamp", "version": "1.0.0"}

        assert (await ship(plugin(tmp_path, "tv", "1.1.0"), inst))["ok"]
        assert (await host.call("tv.ping", {}, ME))["version"] == "1.1.0"

        bad = await ship(plugin(tmp_path, "tv", "1.2.0", answer="fail"), inst)
        assert not bad["ok"] and "device not answering" in bad["reason"]
        assert (await host.call("tv.ping", {}, ME))["version"] == "1.1.0"   # rolled back

        await inst.rollback("tv")
        assert (await host.call("tv.ping", {}, ME))["version"] == "1.0.0"
        assert (await host.call("lamp.ping", {}, ME))["answer"] == "lamp"
        await host.close()
    go(body)
