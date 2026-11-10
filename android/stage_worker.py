#!/usr/bin/env python3
"""Stage Rook 0.5 + telesthete.protocol into the Chaquopy python source set.

Chaquopy bundles whatever lives under ``app/src/main/python`` into the APK and
puts it on ``sys.path``. castling_entry.py needs rook.core2 (less the hub),
rook.kit, rook.streams, the manifest schema, telesthete.protocol (a sibling
repo), and the phone plugins: the plugin folders whose manifest lists
``android`` in ``platforms``, zipped into ``castling_plugins.zip`` (the 0.5
loader reads folders from disk; the app unpacks them at start).

Run from anywhere:
    python3 android/stage_worker.py

The staged dirs are git-ignored (see android/.gitignore) so they never get
committed twice.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent              # android/
REPO_ROOT = HERE.parent                             # repo root (…/rook)
# telesthete is a sibling checkout; without one, the pip-installed package.
TELESTHETE_ROOT = REPO_ROOT.parent / "telesthete" / "telesthete"
if not TELESTHETE_ROOT.is_dir():
    import importlib.util as _ilu
    _spec = _ilu.find_spec("telesthete")
    if _spec and _spec.origin:
        TELESTHETE_ROOT = Path(_spec.origin).parent
PY_DST = HERE / "app" / "src" / "main" / "python"

# telesthete.protocol modules the worker imports. route.py (addressed hub
# routing, telesthete 0.3.0) is the newest: a checkout without it is older than
# pyproject.toml pins, so refuse to build.
REQUIRED_TELESTHETE = ("crypto.py", "fragment.py", "framing.py", "sequence.py",
                       "drop.py", "route.py")


# Set to 1 to build from a telesthete git checkout at another revision than
# the pin (e.g. while changing both repos at once): a warning, not an error.
ANY_REV_ENV = "ROOK_TELESTHETE_ANY_REV"


def pinned_telesthete_rev(pyproject: Path = REPO_ROOT / "pyproject.toml") -> Optional[str]:
    """The telesthete commit pyproject.toml pins (``telesthete @ git+...@<sha>``)."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'"telesthete\s*@\s*git\+[^"@]+@([0-9a-fA-F]{7,40})"', text)
    return m.group(1).lower() if m else None


def _checkout_rev(root: Path) -> Optional[str]:
    """HEAD of the git checkout whose top level holds the ``telesthete``
    package dir ``root``, or None when ``root`` isn't one (a pip install)."""
    checkout = root.resolve().parent
    try:
        top = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL).strip()
        if Path(top).resolve() != checkout:
            return None   # e.g. site-packages inside some unrelated repo
        return subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip().lower()
    except (OSError, subprocess.CalledProcessError):
        return None


def check_telesthete(root: Path, *, verify_rev: bool = True,
                     pin: Optional[str] = None) -> None:
    """Raise if ``root`` (a ``telesthete`` package dir) can't go in a bundle:
    it lacks a protocol module the worker needs, or (``verify_rev``) it is a
    git checkout at another commit than pyproject.toml pins. A pip-installed
    telesthete (from the pin, or any non-checkout install) is checked for
    modules only: it is the transport this hub itself runs, so it needs no
    ``ROOK_TELESTHETE_ANY_REV``."""
    missing = [m for m in REQUIRED_TELESTHETE if not (root / "protocol" / m).is_file()]
    if missing:
        raise FileNotFoundError(
            f"telesthete at {root} is too old (missing protocol/{', protocol/'.join(missing)}); "
            "update it to the commit pinned in pyproject.toml")
    if not verify_rev:
        return
    pin = pin or pinned_telesthete_rev()
    head = _checkout_rev(root)
    if pin is None or head is None or head.startswith(pin) or pin.startswith(head):
        return
    msg = (f"telesthete checkout {root.parent} is at {head[:12]}, but pyproject.toml pins "
           f"{pin[:12]}: the bundle would ship a different transport than the hub "
           f"installs. Check out the pinned commit (git -C {root.parent} checkout {pin}), "
           f"or set {ANY_REV_ENV}=1 to build anyway.")
    if os.environ.get(ANY_REV_ENV, "").strip() == "1":
        print(f"WARNING: {msg}", file=sys.stderr)
        return
    raise RuntimeError(msg)


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
    """Rook 0.5 into the staged ``rook`` package."""
    src = REPO_ROOT / "rook"
    if (rook_dst / "core2").exists():
        shutil.rmtree(rook_dst / "core2")
    junk = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(src / "core2", rook_dst / "core2",
                    ignore=lambda d, names: [*junk(d, names),
                                             *(n for n in names if Path(d) == src / "core2" and n in CORE2_SKIP)])
    for pkg in ("kit", "streams"):
        _copy_pkg(src / pkg, rook_dst / pkg)
    # rook/kit/manifest/validate.py looks for the schema here in an installed copy.
    # Chaquopy puts package data files on disk where __file__ says (seen on the
    # emulator for this and rook/core2/builtin/core.yaml).
    schema = rook_dst / "_conformance"
    schema.mkdir(exist_ok=True)
    (schema / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "conformance" / "manifest" / "manifest.schema.json", schema)
    folders = phone_plugins()
    (PY_DST / "castling_plugins.zip").write_bytes(plugins_zip(folders))
    return folders


def main() -> int:
    if not TELESTHETE_ROOT.is_dir():
        print(f"ERROR: telesthete source not found: {TELESTHETE_ROOT}\n"
              "Expected a sibling checkout at ../telesthete/telesthete or a pip-installed "
              "telesthete.", file=sys.stderr)
        return 1
    try:
        # Modules (e.g. route.py) and, for a git checkout, the pinned commit.
        check_telesthete(TELESTHETE_ROOT)
    except (FileNotFoundError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    rook_dst = PY_DST / "rook"
    if rook_dst.exists():
        shutil.rmtree(rook_dst)             # only what is staged below ships
    rook_dst.mkdir(parents=True)
    (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    folders = stage_castling(rook_dst)

    # telesthete.protocol  ->  python/telesthete/protocol
    tel_dst = PY_DST / "telesthete"
    tel_dst.mkdir(parents=True, exist_ok=True)
    (tel_dst / "__init__.py").write_text("", encoding="utf-8")
    _copy_pkg(TELESTHETE_ROOT / "protocol", tel_dst / "protocol")

    print(f"Staged Rook 0.5 -> {rook_dst}")
    print(f"Staged telesthete.protocol -> {tel_dst / 'protocol'}")
    print(f"Staged {len(folders)} phone plugins -> {PY_DST / 'castling_plugins.zip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
