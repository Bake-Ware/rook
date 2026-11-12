"""Helpers for the 0.5 plugin ports (rook/plugins/<name>): fake binaries on
PATH, a hard timeout around every test body, the core contracts."""

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from rook.kit.ctx import Caller
from rook.kit.manifest import _yaml

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "rook" / "plugins"
CONTRACTS = _yaml.loads((REPO / "rook" / "core2" / "builtin" / "core.yaml").read_text())["contracts"]
ME = Caller(person="bake", agent="claude", band="b1", why="testing a plugin port")


def go(body, timeout=20):
    """Run ``body()`` under a hard timeout: a hung fake never hangs the suite."""
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


class Bins:
    """A directory of fake programs, first on PATH. Each records its argv and
    the ROOK_* environment to ``<name>.calls`` (one JSON line per run)."""

    def __init__(self, root: Path, monkeypatch):
        self.dir = root
        self.dir.mkdir()
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def add(self, name: str, body: str = "") -> Path:
        """A fake ``name`` running ``body`` (Python, with ``argv`` = its args)."""
        path = self.dir / name
        log = self.dir / f"{name}.calls"
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            f"with open({str(log)!r}, 'a') as f:\n"
            "    f.write(json.dumps({'argv': argv, 'env': {k: v for k, v in os.environ.items()"
            " if k.startswith('ROOK_') or k == 'LANG'}}) + '\\n')\n"
            + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def calls(self, name: str) -> list[dict]:
        log = self.dir / f"{name}.calls"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def only_these(self, *keep: str) -> None:
        """PATH is just the fakes, plus links to the real ``keep`` programs."""
        import shutil
        for name in keep:
            real = shutil.which(name)
            if real and not (self.dir / name).exists():
                (self.dir / name).symlink_to(real)
        self.monkeypatch.setenv("PATH", str(self.dir))


@pytest.fixture
def bins(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("fake binaries are POSIX scripts")
    return Bins(tmp_path / "bin", monkeypatch)


class FakeNet:
    """The Rust worker's net.open / net.udp / serial.open and stream.read /
    write / close, with the same contract, over real sockets (asyncio).
    ``serial``: {port: fn(bytes) -> reply bytes} stands in for serial devices."""

    def __init__(self, serial: dict | None = None):
        self.streams: dict[str, dict] = {}
        self.serial = serial or {}
        self.sent: list[tuple[bytes, str | None]] = []     # every stream.write: (data, to)

    def _new(self) -> tuple[str, dict]:
        import secrets
        sid = "s_" + secrets.token_hex(8)
        st = self.streams[sid] = {"events": [], "closed": False, "changed": asyncio.Event()}
        return sid, st

    def _event(self, st, data: bytes, by=None):
        import base64
        st["events"].append({"seq": len(st["events"]) + 1, "type": "bytes",
                             "b64": base64.b64encode(data).decode(), "by": by})
        st["changed"].set()

    async def call(self, cap: str, a: dict):
        import base64
        import ssl
        if cap == "net.open":
            tls = None
            if a.get("tls"):
                tls = ssl.create_default_context()
                if a.get("verify") is False:
                    tls.check_hostname, tls.verify_mode = False, ssl.CERT_NONE
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(
                    a["host"], a["port"], ssl=tls, server_hostname=a["host"] if tls else None), 5)
            except (OSError, asyncio.TimeoutError) as e:
                raise RuntimeError(f"can't connect to {a['host']}:{a['port']}: {e}") from None
            sid, st = self._new()
            st["send"] = lambda data, to=None: (writer.write(data), None)[1]
            st["stop"] = writer.close

            async def pump():
                try:
                    while chunk := await reader.read(65536):
                        self._event(st, chunk)
                except OSError:
                    pass
                st["closed"] = True
                st["changed"].set()
            st["task"] = asyncio.ensure_future(pump())
            return {"stream": sid}
        if cap == "net.udp":
            sid, st = self._new()
            fake = self

            class Proto(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    fake._event(st, data, f"{addr[0]}:{addr[1]}")
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                Proto, local_addr=("0.0.0.0", 0), allow_broadcast=True)
            st["send"] = lambda data, to: transport.sendto(data, (to.rpartition(":")[0], int(to.rpartition(":")[2])))
            st["stop"] = transport.close
            return {"stream": sid}
        if cap == "serial.open":
            if a["port"] not in self.serial:
                raise RuntimeError(f"can't open {a['port']}: no such device")
            sid, st = self._new()
            device = self.serial[a["port"]]
            st["send"] = lambda data, to=None: [self._event(st, r) for r in [device(data)] if r]
            st["stop"] = lambda: None
            return {"stream": sid}
        st = self.streams.get(a.get("stream"))
        if st is None:
            raise RuntimeError(f"unknown stream: {a.get('stream')}")
        if cap == "stream.write":
            body = a["body"]
            data = base64.b64decode(body["bytes"]) if "bytes" in body else body["text"].encode()
            self.sent.append((data, body.get("to")))
            st["send"](data, body.get("to"))
            return {"seq": len(st["events"])}
        if cap == "stream.read":
            after = a.get("after", 0)
            if len(st["events"]) <= after and not st["closed"] and a.get("wait"):
                st["changed"].clear()
                try:
                    await asyncio.wait_for(st["changed"].wait(), a["wait"])
                except asyncio.TimeoutError:
                    pass
            return {"events": st["events"][after:], "next": len(st["events"]), "dropped": 0,
                    "closed": st["closed"] and len(st["events"]) >= after}
        if cap == "stream.close":
            st["closed"] = True
            st["stop"]()
            return {}
        raise RuntimeError(f"unknown capability: {cap}")


def on_worker(plugin: str, cap: str, args: dict | None = None, *, os_name: str = "linux",
              appliance: dict | None = None, settings: dict | None = None, net: FakeNet | None = None,
              data: Path | None = None):
    """Run a hub plugin's ``on: worker`` cap as the hub does, for a worker that
    serves only the primitives: the real shell and file plugins with their
    streams (shell.open, the worker's stream.*), and ``net`` (a FakeNet: the
    Rust worker's net.*, serial.open and stream.*). ``ctx.worker`` names it
    and ``ctx.call_on`` reaches it. Returns (result, [(cap, args)] it
    called); a failure raises KitError. ``settings``: the plugin's hub
    settings; ``data``: the hub's plugin data folder (the plugin's is data/<plugin>)."""
    from types import SimpleNamespace

    from rook.core2.hub.plugins import HubPlugins
    from rook.core2.worker.builtins import handlers
    from rook.core2.worker.host import CallError, Host
    from rook.kit.contracts import Contracts
    from rook.kit.ctx import KitError
    from rook.kit.records import RecordStore
    from rook.streams import Streams

    async def body():
        streams = Streams(host="w1")
        worker = Host(os_name=os_name, streams=streams)
        await worker.load(PLUGINS / "shell")
        await worker.load(PLUGINS / "file")
        builtins = handlers(None, streams, None)
        fake = net or FakeNet()
        seen = []

        async def call(who, cap_, args_, uses=None, target=None, contract=None):
            assert target == "w1", target
            seen.append((cap_, args_))
            if cap_.startswith("stream.") and args_.get("stream") not in fake.streams:
                try:
                    return {"ok": True, "result": await builtins[cap_](SimpleNamespace(caller=ME), **args_)}
                except KitError as e:
                    return {"ok": False, "error": e.message}
            if cap_.split(".")[0] in ("net", "serial", "stream"):
                try:
                    return {"ok": True, "result": await fake.call(cap_, args_)}
                except RuntimeError as e:
                    return {"ok": False, "error": str(e)}
            try:
                return {"ok": True, "result": await worker.call(cap_, args_, ME)}
            except CallError as e:
                return {"ok": False, "error": e.message}
        hub = HubPlugins(records=RecordStore(), contracts=Contracts(), call=call,
                         settings={plugin: settings} if settings else None, data_dir=data)
        await hub.load(PLUGINS / plugin)
        try:
            return await hub.run(cap, args, ME, worker={"id": "w1", "name": "kaiju", "os": os_name},
                                 appliance=appliance), seen
        finally:
            await hub.close()
            await worker.close()
            for st in fake.streams.values():
                st["stop"]()
    return go(body)
