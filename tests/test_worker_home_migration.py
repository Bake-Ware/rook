"""ROOK_WORKER_HOME without losing state kept in ~/.rook-band-worker.

Before this release only the session mirror followed ROOK_WORKER_HOME; a
worker that had it set kept everything else in ~/.rook-band-worker. Moving
all state there must not cost such a worker its identity, enrollment or
pushed config, and above all must not let a deauthed node rejoin."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rook.core import paths

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy = home / ".rook-band-worker"
    state = tmp_path / "state"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ROOK_WORKER_HOME", str(state))
    monkeypatch.delenv(paths.FRESH_ENV, raising=False)
    return legacy, state


def _seed(legacy: Path) -> None:
    (legacy / "worker_id").write_text("abc123\n", encoding="utf-8")
    (legacy / "enrollment.json").write_text('{"bands": []}', encoding="utf-8")
    os.chmod(legacy / "enrollment.json", 0o600)
    (legacy / "banned").write_text('{"at": 1, "reason": "lost"}', encoding="utf-8")
    (legacy / "hold").write_text("", encoding="utf-8")
    (legacy / "update_state.json").write_text("{}", encoding="utf-8")
    (legacy / "config.json").write_text('{"env": {"X": "1"}}', encoding="utf-8")
    os.chmod(legacy / "config.json", 0o600)
    (legacy / "plugins.json").write_text('{"disabled": ["wake"]}', encoding="utf-8")
    (legacy / "custom_caps.json").write_text("{}", encoding="utf-8")
    (legacy / "anchors.json").write_text("[]", encoding="utf-8")
    (legacy / "audit.jsonl").write_text('{"cap": "x"}\n', encoding="utf-8")
    (legacy / "chat").mkdir()
    (legacy / "chat" / "room.jsonl").write_text("hi\n", encoding="utf-8")
    (legacy / "band-worker.pyz").write_bytes(b"PK")  # the bundle: never state


def test_copies_every_state_file_once_and_keeps_modes(homes):
    legacy, state = homes
    _seed(legacy)
    copied = paths.migrate_legacy_state()
    assert set(copied) == {"worker_id", "enrollment.json", "banned", "hold", "update_state.json",
                           "config.json", "plugins.json", "custom_caps.json", "anchors.json",
                           "audit.jsonl", "chat"}
    for name in copied:
        assert (state / name).exists(), name
    assert (state / "worker_id").read_text(encoding="utf-8") == "abc123\n"
    assert (state / "chat" / "room.jsonl").read_text(encoding="utf-8") == "hi\n"
    if os.name == "posix":
        assert stat.S_IMODE((state / "enrollment.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((state / "config.json").stat().st_mode) == 0o600
    assert not (state / "band-worker.pyz").exists()
    assert (legacy / "worker_id").exists()  # left in place for a rollback
    marker = json.loads((state / paths.MIGRATED_MARKER).read_text(encoding="utf-8"))
    assert sorted(marker["copied"]) == sorted(copied)

    # Once: a file deleted on purpose afterwards (say, the ban cleared) is not
    # brought back by the next start.
    (state / "hold").unlink()
    assert paths.migrate_legacy_state() == []
    assert not (state / "hold").exists()


def test_never_overwrites_state_already_in_rook_worker_home(homes):
    legacy, state = homes
    _seed(legacy)
    state.mkdir()
    (state / "worker_id").write_text("mine\n", encoding="utf-8")
    copied = paths.migrate_legacy_state()
    assert "worker_id" not in copied
    assert (state / "worker_id").read_text(encoding="utf-8") == "mine\n"


def test_noop_when_unset_or_same_dir_or_fresh(homes, monkeypatch):
    legacy, state = homes
    _seed(legacy)
    monkeypatch.setenv(paths.FRESH_ENV, "1")
    assert paths.migrate_legacy_state() == [] and not state.exists()
    monkeypatch.delenv(paths.FRESH_ENV)
    monkeypatch.setenv("ROOK_WORKER_HOME", str(legacy))
    assert paths.migrate_legacy_state() == [] and paths.legacy_state_path("banned") is None
    monkeypatch.delenv("ROOK_WORKER_HOME")
    assert paths.migrate_legacy_state() == []


def _probe(home: Path, state: Path, code: str, **extra: str) -> dict:
    """Module-level paths resolve at import: a fresh interpreter, as a
    service would start one."""
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "ROOK_WORKER_HOME": str(state), "PYTHONPATH": str(ROOT), **extra}
    for var in ("ROOK_ENROLLMENT_FILE", "ROOK_WORK_DB"):
        env.pop(var, None)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, cwd=str(state.parent))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_READ_STATE = r"""
import json
from rook.core.paths import migrate_legacy_state
migrate_legacy_state()
from rook.worker import core, enroll, wconfig, admin
from rook.worker.plugins import selfupdate
print(json.dumps({
    "worker_id": core.stable_worker_id(),
    "enrollment": enroll.load(),
    "config": wconfig.load(),
    "disabled": sorted(admin.load_disabled()),
    "banned": selfupdate.is_banned(), "ban": selfupdate.ban_info(),
    "held": selfupdate._HOLD.exists(),
}))
"""


def test_worker_reads_its_old_state_after_the_move(tmp_path):
    home = tmp_path / "home"
    legacy, state = home / ".rook-band-worker", tmp_path / "state"
    legacy.mkdir(parents=True)
    _seed(legacy)
    got = _probe(home, state, _READ_STATE)
    assert got["worker_id"] == "abc123"
    assert got["enrollment"] == {"bands": []}
    assert got["config"].get("env") == {"X": "1"}
    assert got["disabled"] == ["wake"]
    assert got["banned"] and got["ban"]["reason"] == "lost" and got["held"]


def test_a_deauthed_node_stays_banned_even_without_the_copy(tmp_path):
    """ROOK_WORKER_HOME_FRESH skips the copy, and the marker may predate the
    ban; the legacy flag is still honoured, so a deauthed node never rejoins."""
    home = tmp_path / "home"
    legacy, state = home / ".rook-band-worker", tmp_path / "state"
    legacy.mkdir(parents=True)
    (legacy / "banned").write_text('{"at": 5, "reason": "stolen"}', encoding="utf-8")
    got = _probe(home, state, _READ_STATE, ROOK_WORKER_HOME_FRESH="1")
    assert not (state / "banned").exists()
    assert got["banned"] and got["ban"]["reason"] == "stolen"
    assert got["worker_id"] != "abc123"  # fresh identity, as asked
