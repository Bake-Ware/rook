"""scripts/local-hub.sh with stub binaries: finds a cargo-installed relay that
is not on PATH yet, passes an http:// dashboard domain and prints the Install
page URL. Never starts a real hub."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local-hub.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt",
                                reason="needs bash")

STUB = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> \"$STUB_LOG\"\n"


def _stub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB, encoding="utf-8")
    path.chmod(0o755)


def _run(tmp_path, with_cargo_hub=True, with_rook=True):
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    if with_cargo_hub:
        _stub(home / ".cargo" / "bin" / "telesthete-hub")
    if with_rook:
        _stub(bin_dir / "rook-dashboard")
        _stub(bin_dir / "rook-mcp")
    log = tmp_path / "calls.log"
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log),
           "PYTHON": sys.executable, "PYTHONPATH": str(ROOT),
           "ROOK_DATA_DIR": str(tmp_path / "data")}
    # Own session: the script's exit trap runs `kill 0` on its process group.
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, cwd=tmp_path, capture_output=True,
                          text=True, timeout=60, start_new_session=True)
    return proc, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_uses_cargo_bin_relay_and_prints_install_url(tmp_path):
    proc, calls = _run(tmp_path)
    assert "missing" not in proc.stderr, proc.stderr
    assert "telesthete-hub" in calls
    dashboard = next(line for line in calls.splitlines() if line.startswith("rook-dashboard"))
    assert "--domain http://127.0.0.1:7005" in dashboard
    assert "--hub-public 127.0.0.1:8765" in dashboard
    assert "Install a worker:  http://127.0.0.1:7005/#install" in proc.stdout


def test_reports_what_is_missing(tmp_path):
    proc, _calls = _run(tmp_path, with_cargo_hub=False, with_rook=False)
    assert proc.returncode == 1
    assert "missing on PATH: telesthete-hub rook-dashboard rook-mcp" in proc.stderr
