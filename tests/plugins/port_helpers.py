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
