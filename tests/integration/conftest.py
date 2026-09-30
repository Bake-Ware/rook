"""Fixtures for the opt-in integration suite (see docs/testing.md).

The suite runs only when asked: ``ROOK_IT=1`` or ``-m integration``. It then
either attaches to a running test hub (``ROOK_IT_HUB_ENV=<data>/test-hub.env``,
written by ``scripts/test-hub.sh start``) or boots a throwaway one with
``scripts/test-hub.sh`` in a temporary directory and removes it afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "test-hub.sh"


def _enabled(config) -> bool:
    if os.environ.get("ROOK_IT", "") not in ("", "0"):
        return True
    return "integration" in (config.getoption("markexpr", "") or "")


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    mine = [i for i in items if here in Path(str(i.fspath)).parents]
    for item in mine:
        item.add_marker(pytest.mark.integration)
    if _enabled(config):
        return
    skip = pytest.mark.skip(reason="integration suite is opt-in: set ROOK_IT=1 or pass -m integration")
    for item in mine:
        item.add_marker(skip)


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


class Hub:
    """Connection details for a running test hub plus a small MCP helper."""

    def __init__(self, env: dict[str, str]):
        self.env = env
        self.url = env["ROOK_IT_MCP_URL"]
        self.token = env["ROOK_IT_TOKEN"]
        self.workers = [w for w in env.get("ROOK_IT_WORKERS", "").split(",") if w]
        self.knowledge = env.get("ROOK_IT_KNOWLEDGE", "0") == "1"

    @asynccontextmanager
    async def session(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        headers = {"Authorization": f"Bearer {self.token}"}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    yield s

    async def acall(self, session, tool: str, **args):
        res = await session.call_tool(tool, args)
        text = "".join(getattr(c, "text", "") for c in res.content)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def run(self, coro_fn, timeout: float = 90.0):
        """Run ``coro_fn(session)`` inside a fresh MCP session."""
        async def go():
            async with self.session() as s:
                return await coro_fn(s)
        return asyncio.run(asyncio.wait_for(go(), timeout))

    def call(self, tool: str, **args):
        return self.run(lambda s: self.acall(s, tool, **args))


def _wait_for_workers(hub: Hub, deadline_s: float = 90.0) -> None:
    want = set(hub.workers)
    end = time.time() + deadline_s
    seen: set[str] = set()
    while time.time() < end:
        try:
            roster = hub.call("rook_workers")
            seen = {w.get("name") for w in roster} if isinstance(roster, list) else set()
            if want <= seen:
                return
        except Exception:  # noqa: BLE001 — MCP may still be starting
            pass
        time.sleep(2)
    raise RuntimeError(f"test workers never appeared: want {sorted(want)}, saw {sorted(seen)}")


@pytest.fixture(scope="session")
def hub():
    env_file = os.environ.get("ROOK_IT_HUB_ENV")
    if env_file:
        h = Hub(read_env_file(env_file))
        _wait_for_workers(h)
        yield h
        return

    if not shutil.which("bash"):
        pytest.skip("bash is required to boot the test hub")
    data = tempfile.mkdtemp(prefix="rook-it-")
    base = int(os.environ.get("ROOK_IT_PORT_BASE") or random.randrange(20000, 40000, 3))
    env = {**os.environ, "PYTHON": os.environ.get("PYTHON", sys.executable)}
    started = subprocess.run(
        ["bash", str(SCRIPT), "start", "--data", data, "--port-base", str(base), "--workers", "2"],
        env=env, capture_output=True, text=True, timeout=120)
    if started.returncode != 0:
        shutil.rmtree(data, ignore_errors=True)
        pytest.fail(f"test hub failed to start:\n{started.stdout}\n{started.stderr}")
    try:
        h = Hub(read_env_file(Path(data) / "test-hub.env"))
        _wait_for_workers(h)
        yield h
    finally:
        subprocess.run(["bash", str(SCRIPT), "reset", "--data", data], env=env,
                       capture_output=True, text=True, timeout=60)
