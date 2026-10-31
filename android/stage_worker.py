#!/usr/bin/env python3
"""Stage rook.worker + telesthete.protocol (and Rook 0.5) into the Chaquopy python source set.

Chaquopy bundles whatever lives under ``app/src/main/python`` into the APK and
puts it on ``sys.path``. The worker code and the telesthete protocol package
live outside this module (and telesthete is a sibling repo), so we copy them in
before each build — same sources the band-worker.pyz is built from, one source
of truth.

Run from anywhere:
    python3 android/stage_worker.py

Rook 0.5 (castling_entry.py) needs rook.core2 (less the hub), rook.kit,
rook.streams, the manifest schema, and the phone plugins: the plugin folders
whose manifest lists ``android`` in ``platforms``, zipped into
``castling_plugins.zip`` (the 0.5 loader reads folders from disk; the app
unpacks them at start).

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
# telesthete is a sibling checkout, matching build_band_worker.py's layout;
# without one, the pip-installed package (as the zipapp builder does).
TELESTHETE_ROOT = REPO_ROOT.parent / "telesthete" / "telesthete"
if not TELESTHETE_ROOT.is_dir():
    import importlib.util as _ilu
    _spec = _ilu.find_spec("telesthete")
    if _spec and _spec.origin:
        TELESTHETE_ROOT = Path(_spec.origin).parent
PY_DST = HERE / "app" / "src" / "main" / "python"


def _copy_pkg(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


#: rook.core2 parts the phone never runs.
CORE2_SKIP = ("hub", "migrate")


def phone_plugins(root: Path = REPO_ROOT / "rook" / "plugins") -> list[Path]:
    """Plugin folders whose manifest lists android in ``platforms``."""
    import yaml
    found = []
    for manifest in sorted(root.glob("*/plugin.yaml")):
        platforms = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("platforms") or []
        if "android" in platforms:
            found.append(manifest.parent)
    return found


def plugins_zip(folders: list[Path]) -> bytes:
    """The folders as one zip, ``<name>/...``; fixed timestamps, so the same
    plugins give the same bytes (the app unpacks only on a change)."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in folders:
            for f in sorted(folder.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                    info = zipfile.ZipInfo(f"{folder.name}/{f.relative_to(folder).as_posix()}", (1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, f.read_bytes())
    return buf.getvalue()


def stage_castling(rook_dst: Path) -> list[Path]:
    """Rook 0.5 beside the 0.4 worker in the same ``rook`` package."""
    src = REPO_ROOT / "rook"
    if (rook_dst / "core2").exists():
        shutil.rmtree(rook_dst / "core2")
    shutil.copytree(src / "core2", rook_dst / "core2",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", *CORE2_SKIP))
    for pkg in ("kit", "streams"):
        _copy_pkg(src / pkg, rook_dst / pkg)
    # rook/kit/manifest/validate.py looks for the schema here in an installed copy.
    # A package, so Chaquopy extracts it next to its __init__ on import.
    schema = rook_dst / "_conformance"
    schema.mkdir(exist_ok=True)
    (schema / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "conformance" / "manifest" / "manifest.schema.json", schema)
    folders = phone_plugins()
    (PY_DST / "castling_plugins.zip").write_bytes(plugins_zip(folders))
    return folders


def main() -> int:
    if not WORKER_SRC.is_dir():
        print(f"ERROR: worker source not found: {WORKER_SRC}", file=sys.stderr)
        return 1
    if not TELESTHETE_ROOT.is_dir():
        print(f"ERROR: telesthete source not found: {TELESTHETE_ROOT}\n"
              "Expected a sibling checkout at ../telesthete/telesthete or a pip-installed "
              "telesthete (same as build_band_worker.py).", file=sys.stderr)
        return 1

    sys.path.insert(0, str(REPO_ROOT))
    from rook.remote.build_band_worker import check_telesthete
    try:
        # Modules (e.g. route.py) and, for a git checkout, the pinned commit.
        check_telesthete(TELESTHETE_ROOT)
    except (FileNotFoundError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    # rook.worker  ->  python/rook/worker  (with a minimal rook/__init__.py)
    rook_dst = PY_DST / "rook"
    rook_dst.mkdir(parents=True, exist_ok=True)
    (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    _copy_pkg(WORKER_SRC, rook_dst / "worker")
    # rook.core (plugin host, registry, facts) is shared with the hub and
    # imported by rook.worker; stdlib-only.
    _copy_pkg(REPO_ROOT / "rook" / "core", rook_dst / "core")
    folders = stage_castling(rook_dst)
    # Native workers report the same source build as the zipapp.
    import datetime
    sys.path.insert(0,str(REPO_ROOT))
    from rook.remote.build_band_worker import compute_build,_stamp_build_info,_stamp_pubkey
    build,commit,version=compute_build()
    _stamp_build_info(rook_dst/'worker',build,commit,version,
                      datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'))
    _stamp_pubkey(rook_dst/'worker')


    # telesthete.protocol  ->  python/telesthete/protocol
    tel_dst = PY_DST / "telesthete"
    tel_dst.mkdir(parents=True, exist_ok=True)
    (tel_dst / "__init__.py").write_text("", encoding="utf-8")
    _copy_pkg(TELESTHETE_ROOT / "protocol", tel_dst / "protocol")

    print(f"Staged worker -> {rook_dst / 'worker'}")
    print(f"Staged telesthete.protocol -> {tel_dst / 'protocol'}")
    print(f"Staged Rook 0.5 + {len(folders)} phone plugins -> {PY_DST / 'castling_plugins.zip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
