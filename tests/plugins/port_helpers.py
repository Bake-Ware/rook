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


def on_worker(plugin: str, cap: str, args: dict | None = None, *, os_name: str = "linux",
              appliance: dict | None = None, settings: dict | None = None):
    """Run a hub plugin's ``on: worker`` cap as the hub does, for a worker that
    serves only the real shell and file plugins (the primitives): ``ctx.worker`` names it and
    ``ctx.call_on`` reaches it. Returns (result, [(cap, args)] it called);
    a failure raises KitError. ``settings``: the plugin's hub settings."""
    from rook.core2.hub.plugins import HubPlugins
    from rook.core2.worker.host import CallError, Host
    from rook.kit.contracts import Contracts
    from rook.kit.records import RecordStore

    async def body():
        worker = Host(os_name=os_name)
        await worker.load(PLUGINS / "shell")
        await worker.load(PLUGINS / "file")
        seen = []

        async def call(who, cap_, args_, uses=None, target=None, contract=None):
            assert target == "w1", target
            seen.append((cap_, args_))
            try:
                return {"ok": True, "result": await worker.call(cap_, args_, ME)}
            except CallError as e:
                return {"ok": False, "error": e.message}
        hub = HubPlugins(records=RecordStore(), contracts=Contracts(), call=call,
                         settings={plugin: settings} if settings else None)
        await hub.load(PLUGINS / plugin)
        try:
            return await hub.run(cap, args, ME, worker={"id": "w1", "name": "kaiju", "os": os_name},
                                 appliance=appliance), seen
        finally:
            await hub.close()
            await worker.close()
    return go(body)
