"""The whole 0.4 copy, kept read-only beside the 0.5 hub's data.

castling-design (Bake, Oct 30): the one-off move exports 0.4's stores "as
plain files; an agent imports and reshapes them into 0.5; old journal kept
read-only as an archive". So every file of the copy goes to
``<data>/archive/0.4/<same path>``, byte for byte (files 0444, folders 0700),
with ``MANIFEST.json`` naming each one's size and sha256. What the move
imports into 0.5's own stores is imported as well; whatever it doesn't, an
agent can still read here. Nothing 0.4 had is dropped.

Only the 0.4 vault's key stays out: every secret is re-sealed under the 0.5
hub's key (castling-switchover-decisions D8), so the archive keeps
``vault.db`` as ciphertext with no key beside it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

KEYS = {"vault.key"}                     # re-sealed, never archived
DIR = Path("archive") / "0.4"
MANIFEST = "MANIFEST.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def skipped(rel: str) -> bool:
    return Path(rel).name in KEYS


def put(root: Path, files: list[Path], data: Path) -> dict:
    """Copy ``files`` (under ``root``) into ``<data>/archive/0.4``. A file
    already there with the same hash is left alone; one that differs is a
    different copy's and is kept, the new one is not written (reported)."""
    dest = data / DIR
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest, clashes, written = {}, [], 0
    for src in files:
        rel = src.relative_to(root).as_posix()
        if skipped(rel):
            continue
        digest = sha256(src)
        out = dest / rel
        if out.exists():
            if sha256(out) != digest:
                clashes.append(rel)
            manifest[rel] = {"size": out.stat().st_size, "sha256": sha256(out)}
            continue
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = out.with_name(out.name + ".tmp")
        tmp.unlink(missing_ok=True)
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o444)
        os.replace(tmp, out)
        written += 1
        manifest[rel] = {"size": out.stat().st_size, "sha256": digest}
    path = dest / MANIFEST
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o444)
    return {"path": str(dest), "files": len(manifest), "written": written, "clashes": clashes}
