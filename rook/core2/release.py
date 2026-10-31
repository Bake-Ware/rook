"""The 0.5 core release format (N27; docs/castling-updates.md): one signed
tarball of the ``rook`` package that workers run instead of their installed
copy.

    tar = build(source, version, signer=op_key, hub=hub_id, dest=path)  # the hub
    sig, why = verify(path, keys)                                       # a worker
    extract(path, sig, folder)

A release is ``rook-core-<version>.tar.gz`` holding ``release.sig`` and the
files of the ``rook/`` package (no caches, no symlinks). ``release.sig`` is a
``rook-release`` signed object (spec 13): ``hub``, ``version``, ``core`` (the
protocol version the code speaks), ``files`` (posix path -> hex SHA-256 of
every file but ``release.sig``), ``requires`` (the PEP 508 requirements from
the source's pyproject, which a worker installs before it switches) and
``iat``. Unlike ``plugin.sig`` it names no worker: one release, signed once,
goes to every worker; a worker accepts it only under an operational key of a
hub that may approve code there (role ``home`` or ``co-owner``), exactly as
for plugins.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import subprocess
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Mapping

from . import CORE_VERSION, wire

SIG_TYP = "rook-release"
SIG_FILE = "release.sig"
PACKAGE = "rook"
SIG_MAX = 4 << 20                 # the signed file list, not a payload
MAX_BYTES = 48 << 20              # compressed; Drop serves at most 64 MiB per band at once
MAX_UNPACKED = 256 << 20

SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?")
_SHA = re.compile(r"[0-9a-f]{64}")
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class ReleaseError(Exception):
    """Not a release this machine may run; the message says why."""


def safe_rel(name: str) -> bool:
    """A plain relative path inside ``rook/``: no root, drive, backslash, ``.`` or ``..``."""
    return (isinstance(name, str) and name.startswith(PACKAGE + "/") and "\\" not in name and ":" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- the hub: build and sign ---------------------------------------------------------

def _pyproject(source: Path) -> dict:
    path = Path(source) / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReleaseError(f"pyproject.toml: {e}") from None


def layout(source: Path) -> list[tuple[str, str]]:
    """(source path, path in the release) pairs: ``rook`` itself plus the
    wheel's ``force-include`` entries that land inside it (the manifest
    schema, the manual, the skill...), so a release holds what an installed
    wheel does."""
    extra = (((_pyproject(source).get("tool") or {}).get("hatch") or {}).get("build") or {})
    extra = ((extra.get("targets") or {}).get("wheel") or {}).get("force-include") or {}
    pairs = [(PACKAGE, PACKAGE)]
    for src, dst in sorted(extra.items()):
        if isinstance(src, str) and isinstance(dst, str) and dst.startswith(PACKAGE + "/") and \
                all(part not in ("", ".", "..") for part in [*src.split("/"), *dst.split("/")]):
            pairs.append((src.strip("/"), dst.strip("/")))
    return pairs


def _listed(source: Path, path: str) -> list[str]:
    """Files under ``source/path`` (or that one file), relative to ``source``:
    the tracked and not-ignored ones in a git checkout (no gitignored model
    files or local data), else all but caches and compiled files."""
    if (source / ".git").exists():
        r = subprocess.run(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard", "--", path], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise ReleaseError(f"git ls-files failed in {source}: {r.stderr.decode(errors='replace')[:200]}")
        rels = [n for n in r.stdout.decode("utf-8").split("\0") if n]
        return [n for n in rels if (source / n).exists() or (source / n).is_symlink()]   # deleted, not committed
    top = source / path
    if top.is_file() or top.is_symlink():
        return [path]
    if not top.is_dir():
        return []
    rels = []
    for p in sorted(top.rglob("*")):
        parts = p.relative_to(source).parts
        if any(part in _SKIP_DIRS for part in parts) or p.suffix in (".pyc", ".pyo"):
            continue
        if p.is_symlink() or p.is_file():
            rels.append("/".join(parts))
    return rels


def source_files(source: Path) -> dict[str, Path]:
    """Release path -> source file, for every file that goes into a release."""
    source = Path(source).resolve()
    if not (source / PACKAGE / "__init__.py").is_file():
        raise ReleaseError(f"{source} has no {PACKAGE}/ package")
    out: dict[str, Path] = {}
    for src, dst in layout(source):
        for rel in _listed(source, src):
            target = dst + rel[len(src):]
            p = source / rel
            if p.is_symlink() or not p.is_file():
                raise ReleaseError(f"{rel} is a symlink or not a file: a release holds plain files only")
            if not safe_rel(target) or target.endswith("/" + SIG_FILE) or target in out:
                raise ReleaseError(f"{rel} can't go into a release as {target}")
            out[target] = p
    return dict(sorted(out.items()))


def requirements(source: Path) -> list[str]:
    """``[project].dependencies`` of ``source/pyproject.toml`` (none without one)."""
    deps = (_pyproject(source).get("project") or {}).get("dependencies") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) and d and not d.startswith("-") for d in deps):
        raise ReleaseError("pyproject.toml: dependencies must be requirement strings")
    return list(deps)


def base_version(source: Path) -> str:
    v = (_pyproject(source).get("project") or {}).get("version")
    return v if isinstance(v, str) and SEMVER.fullmatch(v) and "+" not in v else "0.5.0"


def digest(files: Mapping[str, str]) -> str:
    """One hash over a release's file list: the same code gives the same digest."""
    return hashlib.sha256(wire.canonical(dict(files))).hexdigest()


def plan(source: Path, version: str | None = None) -> tuple[str, dict[str, Path], dict[str, str], list[str]]:
    """(version, files, hashes, requires) for a release of ``source``. The
    version defaults to the pyproject's plus the code's digest
    (``0.5.0+3f0a9c1d2e3f``), so the same code is the same release."""
    files = source_files(source)
    hashes = {rel: sha256_file(p) for rel, p in files.items()}
    version = version or f"{base_version(source)}+{digest(hashes)[:12]}"
    if not SEMVER.fullmatch(version):
        raise ReleaseError(f"version {version!r} is not semver (like 0.5.1 or 0.5.1-rc.2)")
    return version, files, hashes, requirements(source)


def sign(signer, *, hub: str, version: str, files: Mapping[str, str], requires: list[str],
         now: int | None = None) -> dict:
    return wire.sign(signer, {"typ": SIG_TYP, "v": 2, "hub": hub, "version": version, "core": CORE_VERSION,
                              "files": dict(sorted(files.items())), "requires": list(requires),
                              "iat": int(time.time()) if now is None else now})


def build(source: Path, dest: Path, *, signer, hub: str, version: str | None = None,
          now: int | None = None) -> dict:
    """Write the signed release of ``source`` to ``dest`` (a .tar.gz); returns release.sig."""
    version, files, hashes, requires = plan(source, version)
    sig = sign(signer, hub=hub, version=version, files=hashes, requires=requires, now=now)
    dest = Path(dest)
    part = dest.with_name(f"{dest.name}.{secrets.token_hex(4)}.part")
    try:
        with tarfile.open(part, "w:gz", format=tarfile.PAX_FORMAT, compresslevel=6) as tar:
            raw = json.dumps(sig, sort_keys=True).encode("utf-8")
            _add(tar, SIG_FILE, raw)
            for rel, p in files.items():
                data = p.read_bytes()
                if hashlib.sha256(data).hexdigest() != hashes[rel]:
                    raise ReleaseError(f"{rel} changed while the release was built")
                _add(tar, rel, data)
        if part.stat().st_size > MAX_BYTES:
            raise ReleaseError(f"release is {part.stat().st_size} bytes, more than {MAX_BYTES}")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return sig


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, 0
    tar.addfile(info, io.BytesIO(data))


# -- a worker: verify and extract ---------------------------------------------------------

def check_sig(sig, keys: Mapping[str, str]) -> str | None:
    """Why ``sig`` is not a release.sig this machine may run, or None."""
    if not isinstance(sig, dict) or sig.get("typ") != SIG_TYP or sig.get("v") != 2:
        return "malformed"
    if not isinstance(sig.get("version"), str) or not SEMVER.fullmatch(sig["version"]):
        return "malformed: version"
    if not isinstance(sig.get("core"), str):
        return "malformed: core"
    files, requires = sig.get("files"), sig.get("requires")
    if not isinstance(files, dict) or f"{PACKAGE}/__init__.py" not in files or not all(
            safe_rel(k) and isinstance(v, str) and _SHA.fullmatch(v) for k, v in files.items()):
        return "malformed: files"
    if not isinstance(requires, list) or not all(isinstance(r, str) and r and not r.startswith("-")
                                                  for r in requires):
        return "malformed: requires"
    pub = keys.get(sig.get("kid")) if isinstance(sig.get("kid"), str) else None
    if not pub:
        return f"not signed by a hub that may approve code here (hub {sig.get('hub')})"
    if not wire.verify_sig(pub, sig):
        return "bad signature"
    if wire.negotiate(CORE_VERSION, sig["core"]) is None:
        return f"speaks core {sig['core']}, this worker {CORE_VERSION}"
    return None


def read_sig(path: Path) -> dict:
    """The release.sig of a release tarball, unverified."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            m = tar.next()
            if m is None or m.name != SIG_FILE or not m.isreg() or m.size > SIG_MAX:
                raise ReleaseError("not a release: release.sig must come first")
            return json.loads(tar.extractfile(m).read())
    except (OSError, tarfile.TarError, EOFError, ValueError) as e:
        raise ReleaseError(f"not a release: {e}") from None


def extract(path: Path, sig: dict, folder: Path) -> None:
    """Unpack a verified release into the new folder ``folder``: exactly the
    signed files with their signed hashes, then release.sig. Removes what it
    wrote on any failure."""
    folder = Path(folder)
    folder.mkdir(parents=True)
    want = dict(sig["files"])
    total = 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            seen: set[str] = set()
            for m in tar:
                if m.name == SIG_FILE and not seen:
                    seen.add(SIG_FILE)
                    continue
                if not m.isreg() or m.name not in want or m.name.casefold() in seen:
                    raise ReleaseError(f"tampered release: unexpected entry {m.name}")
                seen.add(m.name.casefold())
                total += m.size
                if total > MAX_UNPACKED:
                    raise ReleaseError("release unpacks to too much")
                dest = folder / m.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with tar.extractfile(m) as src, open(dest, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != want.pop(m.name):
                    raise ReleaseError(f"tampered release: {m.name} does not match its signed hash")
        if want:
            raise ReleaseError(f"tampered release: missing {sorted(want)[0]}"
                               + (f" and {len(want) - 1} more" if len(want) > 1 else ""))
        (folder / SIG_FILE).write_text(json.dumps(sig, sort_keys=True), encoding="utf-8")
    except BaseException:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
