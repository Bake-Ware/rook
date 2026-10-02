"""Live conformance: candidate workers against a real reference hub (opt-in).

Boots one throwaway hub for the module with ``scripts/test-hub.sh`` (no
workers, band risk ceiling ``write`` so band peers may post to chat rooms),
then runs ``conformance/harness.py`` against each candidate:

* the Python reference candidate (``conformance/reference_worker.py``), always;
* the TypeScript and Rust example ports, with ``ROOK_PORTS=1`` and the
  toolchain on PATH (node >= 23.6, cargo).

The session ``hub`` fixture's hub is not reused: it runs with the default
band ceiling (``read``), which skips the chat checks.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test-hub.sh"
sys.path.insert(0, str(ROOT / "conformance"))


@pytest.fixture(scope="module")
def write_hub():
    if not shutil.which("bash"):
        pytest.skip("bash is required to boot the test hub")
    data = tempfile.mkdtemp(prefix="rook-it-conf-")
    base = random.randrange(20000, 40000, 3)
    env = {**os.environ, "PYTHON": os.environ.get("PYTHON", sys.executable)}
    r = subprocess.run(["bash", str(SCRIPT), "start", "--data", data, "--port-base", str(base),
                        "--workers", "0", "--no-dashboard", "--no-knowledge",
                        "--band-max-risk", "write"],
                       env=env, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        shutil.rmtree(data, ignore_errors=True)
        pytest.fail(f"test hub failed to start:\n{r.stdout}\n{r.stderr}")
    try:
        yield str(Path(data) / "test-hub.env")
    finally:
        subprocess.run(["bash", str(SCRIPT), "reset", "--data", data], env=env,
                       capture_output=True, text=True, timeout=60)


def _run(env_file: str, candidate: str, identity: str, tmp_path) -> None:
    import harness
    code = harness.main(["--candidate", candidate, "--hub-env", env_file,
                         "--identity", identity, "--log", str(tmp_path / "candidate.log")])
    log = (tmp_path / "candidate.log").read_text(errors="replace")[-3000:]
    assert code == 0, f"conformance failed; candidate log tail:\n{log}"


def _ports() -> bool:
    return os.environ.get("ROOK_PORTS", "") not in ("", "0")


def test_python_reference_candidate(write_hub, tmp_path):
    _run(write_hub, f"{sys.executable} conformance/reference_worker.py", "agent:conformance-py",
         tmp_path)


@pytest.mark.skipif(not _ports(), reason="port runs are opt-in: set ROOK_PORTS=1")
def test_typescript_port(write_hub, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not found")
    _run(write_hub, "node examples/ports/typescript/src/main.ts", "agent:conformance-ts", tmp_path)


@pytest.mark.skipif(not _ports(), reason="port runs are opt-in: set ROOK_PORTS=1")
def test_rust_port(write_hub, tmp_path):
    if not shutil.which("cargo"):
        pytest.skip("cargo not found")
    port = ROOT / "examples" / "ports" / "rust"
    b = subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=port,
                       capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr
    _run(write_hub, str(port / "target" / "release" / "rook-port"), "agent:conformance-rs",
         tmp_path)
