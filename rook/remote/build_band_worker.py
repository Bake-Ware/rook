#!/usr/bin/env python3
"""Build band-worker.pyz — self-contained zipapp bundling rook.worker + telesthete.protocol.

Run from anywhere:
    python3 rook/remote/build_band_worker.py

Output: rook/remote/band-worker.pyz
"""

from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent        # rook/remote/
REPO_ROOT = HERE.parent.parent               # /root/repos/rook
WORKER_SRC = REPO_ROOT / "rook" / "worker"
TELESTHETE_ROOT = REPO_ROOT.parent / "telesthete" / "telesthete"
OUTPUT = HERE / "band-worker.pyz"

_MAIN = """\
from rook.worker.cli import main
main()
"""


def _copy_pkg(src: Path, dst: Path) -> None:
    """Copy a Python package tree, skipping __pycache__ and compiled bytecode."""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def build() -> Path:
    if not WORKER_SRC.is_dir():
        raise FileNotFoundError(f"Worker source not found: {WORKER_SRC}")
    if not TELESTHETE_ROOT.is_dir():
        raise FileNotFoundError(
            f"Telesthete source not found: {TELESTHETE_ROOT}\n"
            "Expected at /root/repos/telesthete/telesthete"
        )

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)

        (t / "__main__.py").write_text(_MAIN, encoding="utf-8")

        # rook.worker package
        rook_dst = t / "rook"
        rook_dst.mkdir()
        (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        _copy_pkg(WORKER_SRC, rook_dst / "worker")

        # telesthete.protocol package (only protocol/ is imported by rook.worker)
        tel_dst = t / "telesthete"
        tel_dst.mkdir()
        (tel_dst / "__init__.py").write_text("", encoding="utf-8")
        _copy_pkg(TELESTHETE_ROOT / "protocol", tel_dst / "protocol")

        zipapp.create_archive(
            str(t),
            target=str(OUTPUT),
            interpreter="/usr/bin/env python3",
        )

    size = OUTPUT.stat().st_size
    print(f"Built: {OUTPUT}  ({size:,} bytes)")
    return OUTPUT


if __name__ == "__main__":
    build()
