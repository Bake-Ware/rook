#!/usr/bin/env python3
"""Build band-worker.pyz — self-contained zipapp bundling rook.worker + telesthete.protocol.

Run from anywhere:
    python3 rook/remote/build_band_worker.py

Output: rook/remote/band-worker.pyz
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
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


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(REPO_ROOT), text=True,
        stderr=subprocess.DEVNULL).strip()


def compute_build() -> tuple[int, str, str]:
    """Return (build, commit, version). ``build`` is git's commit count — a
    monotonic integer that the OTA self-update compares. Falls back to
    (0, "", "0.dev") outside a git checkout, which disables auto-update for
    that bundle (0 is never greater than a published build)."""
    try:
        build = int(_git("rev-list", "--count", "HEAD"))
        commit = _git("rev-parse", "--short", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
        version = f"{build}.{commit}" + ("+dirty" if dirty else "")
        if dirty:
            print("WARNING: building from a dirty tree — version tagged +dirty")
        return build, commit, version
    except Exception as e:
        print(f"WARNING: could not derive version from git ({e}); "
              "stamping BUILD=0 (auto-update disabled for this bundle)")
        return 0, "", "0.dev"


def _stamp_build_info(worker_dst: Path, build: int, commit: str,
                      version: str, built_at: str) -> None:
    """Overwrite the bundled _build_info.py with the real build stamp."""
    (worker_dst / "_build_info.py").write_text(
        '"""Generated at bundle time by build_band_worker.py — do not edit."""\n'
        "from __future__ import annotations\n\n"
        f"BUILD = {build}\n"
        f"VERSION = {version!r}\n"
        f"COMMIT = {commit!r}\n"
        f"BUILT_AT = {built_at!r}\n\n"
        "def as_dict() -> dict:\n"
        '    return {"version": VERSION, "build": BUILD, "commit": COMMIT, "built_at": BUILT_AT}\n',
        encoding="utf-8",
    )


def _write_manifest(build: int, commit: str, version: str, built_at: str) -> Path:
    """Compute the pyz hash and write a signed manifest next to it."""
    import hashlib
    h = hashlib.sha256()
    with open(OUTPUT, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    manifest = {
        "schema": 1,
        "build": build,
        "version": version,
        "commit": commit,
        "built_at": built_at,
        "filename": OUTPUT.name,
        "sha256": h.hexdigest(),
        "size": OUTPUT.stat().st_size,
    }
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from rook.remote.update_keys import sign_manifest
    manifest = sign_manifest(manifest)
    import json
    manifest_path = OUTPUT.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    signed = "signed" if manifest.get("sig") else "UNSIGNED"
    print(f"Manifest: {manifest_path}  ({signed})")
    return manifest_path


def build() -> Path:
    if not WORKER_SRC.is_dir():
        raise FileNotFoundError(f"Worker source not found: {WORKER_SRC}")
    if not TELESTHETE_ROOT.is_dir():
        raise FileNotFoundError(
            f"Telesthete source not found: {TELESTHETE_ROOT}\n"
            "Expected at /root/repos/telesthete/telesthete"
        )

    build_num, commit, version = compute_build()
    built_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)

        (t / "__main__.py").write_text(_MAIN, encoding="utf-8")

        # rook.worker package
        rook_dst = t / "rook"
        rook_dst.mkdir()
        (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        _copy_pkg(WORKER_SRC, rook_dst / "worker")
        _stamp_build_info(rook_dst / "worker", build_num, commit, version, built_at)

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
    print(f"Built: {OUTPUT}  v{version}  ({size:,} bytes)")
    _write_manifest(build_num, commit, version, built_at)
    return OUTPUT


if __name__ == "__main__":
    build()
