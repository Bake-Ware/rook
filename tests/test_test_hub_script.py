"""Guards of scripts/test-hub.sh: it must refuse anything that could be a live
install. These run in the normal suite and never start a hub."""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test-hub.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(*args, home=None):
    env = {**os.environ, "PYTHON": sys.executable, "TELESTHETE_HUB": "/nonexistent/telesthete-hub"}
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=60)


def free_base():
    """A port base whose three ports are currently free."""
    for _ in range(50):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        base = s.getsockname()[1]
        s.close()
        if base > 65000:
            continue
        ok = True
        for port, kind in ((base, socket.SOCK_DGRAM), (base + 1, socket.SOCK_STREAM),
                           (base + 2, socket.SOCK_STREAM)):
            t = socket.socket(socket.AF_INET, kind)
            try:
                t.bind(("0.0.0.0", port))
            except OSError:
                ok = False
            finally:
                t.close()
        if ok:
            return base
    pytest.skip("no free port range")


@pytest.mark.parametrize("base", [7474, 7003, 8763, 8765])
def test_refuses_live_default_ports(tmp_path, base):
    r = run("start", "--data", tmp_path / "d", "--port-base", base)
    assert r.returncode != 0
    assert "default port" in r.stderr
    assert not (tmp_path / "d").exists()


def test_refuses_data_dir_that_looks_live(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    (data / "quickstart.env").write_text("ROOK_BAND_PSK=x\n")
    r = run("start", "--data", data, "--port-base", free_base())
    assert r.returncode != 0
    assert "looks like a live install" in r.stderr
    assert sorted(p.name for p in data.iterdir()) == ["quickstart.env"]


def test_refuses_non_empty_unmarked_data_dir(tmp_path):
    data = tmp_path / "other"
    data.mkdir()
    (data / "notes.txt").write_text("x")
    r = run("start", "--data", data, "--port-base", free_base())
    assert r.returncode != 0
    assert "not a test hub" in r.stderr


@pytest.mark.parametrize("sub", ["", ".rook-band-worker", ".config/rook"])
def test_refuses_home_and_live_state_dirs(tmp_path, sub):
    home = tmp_path / "home"
    home.mkdir()
    r = run("start", "--data", home / sub if sub else home, "--port-base", free_base(), home=home)
    assert r.returncode != 0
    assert "live Rook state" in r.stderr


def test_refuses_bound_ports(tmp_path):
    base = free_base()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", base + 2))
    s.listen(1)
    try:
        r = run("start", "--data", tmp_path / "d", "--port-base", base)
    finally:
        s.close()
    assert r.returncode != 0
    assert "in use" in r.stderr
    assert f"{base + 2}/tcp" in r.stderr


def test_missing_relay_is_reported(tmp_path):
    r = run("start", "--data", tmp_path / "d", "--port-base", free_base())
    assert r.returncode != 0
    assert "TELESTHETE_HUB" in r.stderr


def test_status_stop_reset_without_marker(tmp_path):
    data = tmp_path / "d"
    assert run("status", "--data", data).returncode == 1
    assert run("stop", "--data", data).returncode != 0
    assert run("reset", "--data", data).returncode == 0  # nothing there
    data.mkdir()
    (data / "keep").write_text("x")
    r = run("reset", "--data", data)
    assert r.returncode != 0 and "no .rook-test-hub marker" in r.stderr
    assert (data / "keep").exists()


def test_reset_removes_marked_dir_and_ignores_foreign_pids(tmp_path):
    data = tmp_path / "d"
    (data / "run").mkdir(parents=True)
    (data / ".rook-test-hub").touch()
    # A PID file pointing at a process that is not ours must never be signalled.
    (data / "run" / "relay.pid").write_text(str(os.getpid()))
    r = run("reset", "--data", data)
    assert r.returncode == 0, r.stderr
    assert not data.exists()


def test_rejects_unknown_command():
    r = run("launch")
    assert r.returncode == 2
