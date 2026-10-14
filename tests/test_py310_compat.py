"""Worker-shipped code keeps running on Python 3.10.

pyproject asks for 3.11, but no installer checked it and master's worker
(rook.core + rook.worker, the pyz bundle) ran on 3.10. Round-1 code added
``asyncio.timeout`` to every cap call (rook/core/registry.py), which would
have broken such a worker on its first call. These tests keep 3.11-only
APIs out of the bundle and check the 3.10 shim in rook/core/aiocompat.py.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rook.core import aiocompat

ROOT = Path(__file__).resolve().parent.parent
# What build_band_worker.py and android/stage_worker.py ship, plus the band
# client, which shares the worker's transport code and must not be the one
# piece of the wire path that needs 3.11.
SHIPPED = [ROOT / "rook" / "core", ROOT / "rook" / "worker", ROOT / "rook" / "cli" / "band_tui.py",
           ROOT / "rook" / "band_mcp" / "client.py"]

# Attribute or name -> what to use instead.
FORBIDDEN_ATTRS = {
    ("asyncio", "timeout"): "rook.core.aiocompat.timeout",
    ("asyncio", "timeout_at"): "rook.core.aiocompat.timeout",
    ("asyncio", "TaskGroup"): "asyncio.gather",
    ("asyncio", "Runner"): "asyncio.run",
    ("hashlib", "file_digest"): "a read loop",
    ("datetime", "UTC"): "datetime.timezone.utc",
    ("typing", "Self"): "a quoted class name",
    ("enum", "StrEnum"): "(str, Enum)",
}
FORBIDDEN_MODULES = {"tomllib": "a guarded import"}
FORBIDDEN_NAMES = {"ExceptionGroup", "BaseExceptionGroup"}


def _shipped_files():
    for base in SHIPPED:
        for path in ([base] if base.is_file() else base.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    rel = path.relative_to(ROOT)
    out = []
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alt = FORBIDDEN_ATTRS.get((node.value.id, node.attr))
            if alt:
                out.append(f"{where}: {node.value.id}.{node.attr} (3.11+; use {alt})")
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (node.module, alias.name) in FORBIDDEN_ATTRS:
                    out.append(f"{where}: from {node.module} import {alias.name} (3.11+)")
            if node.module.split(".")[0] in FORBIDDEN_MODULES:
                out.append(f"{where}: {node.module} (3.11+)")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    out.append(f"{where}: import {alias.name} (3.11+)")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            out.append(f"{where}: {node.id} (3.11+)")
        elif type(node).__name__ == "TryStar":
            out.append(f"{where}: except* (3.11+)")
    return out


def test_scan_covers_the_band_transport_and_wire_code():
    files = {p.relative_to(ROOT).as_posix() for p in _shipped_files()}
    for must in ("rook/worker/transports/telesthete_hub.py",
                 "rook/worker/transports/dispatch.py", "rook/worker/transports/writer.py",
                 "rook/worker/wire/codec.py", "rook/worker/wire/envelope.py",
                 "rook/worker/ota_drop.py", "rook/band_mcp/client.py"):
        assert must in files


def test_worker_bundle_uses_no_python_311_only_api():
    bad = [v for path in _shipped_files() if path.name != "aiocompat.py"
           for v in _violations(path)]
    assert bad == [], "\n".join(bad)


@pytest.fixture(params=["native", "backport"])
def timeout(request):
    if request.param == "native":
        return aiocompat.timeout
    return aiocompat._timeout_310


@pytest.mark.asyncio
async def test_timeout_expires_and_says_so(timeout):
    with pytest.raises(TimeoutError):
        async with timeout(0.01) as window:
            await asyncio.sleep(5)
    assert window.expired()


@pytest.mark.asyncio
async def test_timeout_lets_the_bodys_own_timeout_error_through(timeout):
    with pytest.raises(TimeoutError, match="mine"):
        async with timeout(5) as window:
            raise TimeoutError("mine")
    assert not window.expired()


@pytest.mark.asyncio
async def test_timeout_keeps_the_body_in_the_callers_task(timeout):
    var = contextvars.ContextVar("v", default="unset")
    me = asyncio.current_task()

    async def body():
        var.set("set")
        return asyncio.current_task()

    async with timeout(5):
        task = await body()
    assert task is me and var.get() == "set"


@pytest.mark.asyncio
async def test_timeout_none_never_fires(timeout):
    async with timeout(None) as window:
        await asyncio.sleep(0.01)
    assert not window.expired()


def _python310() -> str | None:
    found = shutil.which("python3.10")
    if found:
        return found
    uv = Path.home() / ".local" / "share" / "uv" / "python"
    for path in sorted(uv.glob("cpython-3.10*/bin/python3.10")) if uv.is_dir() else ():
        return str(path)
    return None


@pytest.mark.skipif(_python310() is None, reason="no Python 3.10 interpreter here")
def test_registry_call_times_out_on_python_310(tmp_path):
    """The real interpreter: rook.core imports and a cap call is bounded."""
    code = textwrap.dedent("""
        import asyncio, sys
        assert sys.version_info[:2] == (3, 10), sys.version
        from rook.core.registry import CapabilityRegistry, CapTimeout
        from rook.core.plugin import CapMeta

        async def slow():
            await asyncio.sleep(5)

        async def fast():
            return 42

        async def main():
            r = CapabilityRegistry()
            r.register("x.slow", slow, meta=CapMeta(timeout=0.05))
            r.register("x.fast", fast, meta=CapMeta(timeout=1))
            assert await r.call("x.fast") == 42
            try:
                await r.call("x.slow")
            except CapTimeout:
                print("ok")

        asyncio.run(main())
    """)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    res = subprocess.run([_python310(), "-c", code], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr
