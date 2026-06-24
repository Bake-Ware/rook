#!/usr/bin/env python3
"""Stage rook.worker + telesthete.protocol into the Chaquopy python source set.

Chaquopy bundles whatever lives under ``app/src/main/python`` into the APK and
puts it on ``sys.path``. The worker code and the telesthete protocol package
live outside this module (and telesthete is a sibling repo), so we copy them in
before each build — same sources the band-worker.pyz is built from, one source
of truth.

Run from anywhere:
    python3 android/stage_worker.py

Re-run whenever rook.worker or telesthete.protocol changes. The staged dirs are
git-ignored (see android/.gitignore) so they never get committed twice.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent              # android/
REPO_ROOT = HERE.parent                             # repo root (…/rook)
WORKER_SRC = REPO_ROOT / "rook" / "worker"
# telesthete is a sibling checkout, matching build_band_worker.py's layout.
TELESTHETE_ROOT = REPO_ROOT.parent / "telesthete" / "telesthete"
PY_DST = HERE / "app" / "src" / "main" / "python"


def _copy_pkg(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def main() -> int:
    if not WORKER_SRC.is_dir():
        print(f"ERROR: worker source not found: {WORKER_SRC}", file=sys.stderr)
        return 1
    if not TELESTHETE_ROOT.is_dir():
        print(f"ERROR: telesthete source not found: {TELESTHETE_ROOT}\n"
              "Expected a sibling checkout at ../telesthete/telesthete "
              "(same as build_band_worker.py).", file=sys.stderr)
        return 1

    # rook.worker  ->  python/rook/worker  (with a minimal rook/__init__.py)
    rook_dst = PY_DST / "rook"
    rook_dst.mkdir(parents=True, exist_ok=True)
    (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    _copy_pkg(WORKER_SRC, rook_dst / "worker")

    # telesthete.protocol  ->  python/telesthete/protocol
    tel_dst = PY_DST / "telesthete"
    tel_dst.mkdir(parents=True, exist_ok=True)
    (tel_dst / "__init__.py").write_text("", encoding="utf-8")
    _copy_pkg(TELESTHETE_ROOT / "protocol", tel_dst / "protocol")

    print(f"Staged worker -> {rook_dst / 'worker'}")
    print(f"Staged telesthete.protocol -> {tel_dst / 'protocol'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
