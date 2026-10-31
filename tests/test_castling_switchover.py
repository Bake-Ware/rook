"""castling/switchover/dryrun.py: the migration's dry run from a hub snapshot
tar, with a private full report and a names-free summary."""
from __future__ import annotations

import importlib.util
import io
import os
import stat
import tarfile
import tempfile
from pathlib import Path

import pytest

from test_core2_migrate import SECRET, make_04

REPO = Path(__file__).resolve().parents[1]


def _dryrun():
    spec = importlib.util.spec_from_file_location("switchover_dryrun", REPO / "castling/switchover/dryrun.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _add(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def make_snapshot(root: Path) -> Path:
    """A hub snapshot laid out as hub-offload's: databases under data/db,
    the rest of the data folder in a nested tar."""
    copy = make_04(root)
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tf:
        for name in ("trust.json", "vault.key"):
            _add(tf, f"rook-band-mcp/{name}", (copy / name).read_bytes())
        _add(tf, "rook-band-mcp/plugins/home/sessions.json", b"{}")          # not needed: left out
    snap = root / "snap-20261027.tar"
    with tarfile.open(snap, "w") as tf:
        for p in sorted(copy.rglob("*.db")):
            _add(tf, f"snap-20261027/data/db/{p.relative_to(copy)}", p.read_bytes())
        _add(tf, "snap-20261027/data/var-lib-rook-band-mcp-nodb.tar", inner.getvalue())
        _add(tf, "snap-20261027/data/db/../../../escape.txt", b"no")           # never written outside
        _add(tf, "snap-20261027/meta/release.txt", b"1241.mopey.blimp\n")
    return snap


def test_snapshot_dry_run_writes_private_reports_and_a_names_free_summary(tmp_path, capsys):
    snap, out = make_snapshot(tmp_path), tmp_path / "out"
    before = set(Path(tempfile.gettempdir()).glob("rook-switchover-*"))
    assert _dryrun().main(["--snapshot", str(snap), "--out", str(out)]) == 0
    assert set(Path(tempfile.gettempdir()).glob("rook-switchover-*")) == before    # the unpacked copy is gone
    assert not (tmp_path / "escape.txt").exists()
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for name in ("report.md", "report.json", "summary.md"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600, name
    summary = (out / "summary.md").read_text()
    assert capsys.readouterr().out == summary
    for words in ("stores found: 8 of 9", "people: 3 (1 member-admin, 1 member-write, 1 owner)",
                  "vault: 2 secrets", "machines to re-enroll: 3", "1 API keys to mint again"):
        assert words in summary, words
    report = (out / "report.md").read_text()
    assert "github-pat" in report and "github-pat" not in summary          # names stay in the private report
    for text in (summary, report, (out / "report.json").read_text()):
        assert SECRET not in text and "kvm-pass-123" not in text


def test_a_folder_copy_works_too_and_a_non_snapshot_is_refused(tmp_path):
    out = tmp_path / "out"
    assert _dryrun().main(["--from", str(make_04(tmp_path)), "--out", str(out)]) == 0
    assert "stores found: 8 of 9" in (out / "summary.md").read_text()
    bad = tmp_path / "empty.tar"
    with tarfile.open(bad, "w") as tf:
        _add(tf, "x/readme.txt", b"hi")
    with pytest.raises(SystemExit, match="not a 0.4 hub snapshot"):
        _dryrun().main(["--snapshot", str(bad), "--out", str(out)])
