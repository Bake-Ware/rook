#!/usr/bin/env python3
"""Download a prebuilt telesthete-hub (the band relay) and verify its checksum.

    python3 scripts/fetch-relay.py --dest .venv/bin                  # the pinned relay, this arch
    python3 scripts/fetch-relay.py --docker-arch arm64 --dest /out/bin --pins /rook_release.py

Fetches telesthete-hub-<arch>-linux.tar.gz from the telesthete GitHub Releases
and extracts the static binary into --dest. The version and the expected
sha256 come from the pin in rook/_release.py (RELAY, RELAY_SHA256; --pins, by
default the one in this checkout), so a download is checked against a hash
committed to Rook, not one published next to the file. For a version or arch
the pin doesn't cover, it falls back to the release's own .sha256 and says so.
Stdlib only, so it runs in a bare python image.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

BASE = "https://github.com/Bake-Ware/telesthete/releases"
# Docker's TARGETARCH[/TARGETVARIANT] -> the asset's arch (uname -m spelling).
DOCKER_ARCH = {"amd64": "x86_64", "arm64": "aarch64", "arm/v7": "armv7", "armv7": "armv7"}
ARCHES = ("x86_64", "aarch64", "armv7")
# The checkout's pins when run from scripts/; the Dockerfile copies this script
# to / (no parent directory) and passes --pins explicitly.
_HERE = Path(__file__).resolve()
DEFAULT_PINS = (_HERE.parents[1] / "rook" / "_release.py") if len(_HERE.parents) > 1 else None


def read_pins(path: Path | None) -> tuple[str, dict]:
    """(RELAY, RELAY_SHA256) from a rook/_release.py, without importing it."""
    if path is None:
        return "", {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return "", {}
    vals = {t.id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) for t in node.targets
            if isinstance(t, ast.Name) and t.id in ("RELAY", "RELAY_SHA256")}
    return str(vals.get("RELAY", "")), dict(vals.get("RELAY_SHA256", {}))


def asset_url(base: str, version: str, name: str) -> str:
    base = base.rstrip("/")
    if version == "latest":
        return f"{base}/latest/download/{name}"
    return f"{base}/download/{version}/{name}"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "rook-fetch-relay"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def expected_sha256(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise ValueError(f"no checksum for {name}")


def extract_binary(tarball: bytes, dest: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        member = tf.getmember("telesthete-hub")
        if not member.isfile():
            raise ValueError("telesthete-hub in the archive is not a regular file")
        data = tf.extractfile(member).read()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "telesthete-hub"
    tmp = out.with_name(out.name + ".partial")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download and verify a prebuilt telesthete-hub.")
    ap.add_argument("--version", default=os.environ.get("TELESTHETE_RELAY_VERSION", ""),
                    help="telesthete release tag (default: the pinned RELAY) or 'latest' "
                         "[env TELESTHETE_RELAY_VERSION]")
    ap.add_argument("--pins", type=Path, default=DEFAULT_PINS,
                    help="rook/_release.py holding RELAY and RELAY_SHA256")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--arch", choices=ARCHES, help="asset arch (default: this machine's)")
    g.add_argument("--docker-arch", help="Docker TARGETARCH[/TARGETVARIANT], e.g. amd64, arm64, arm/v7")
    ap.add_argument("--dest", type=Path, required=True, help="directory to put telesthete-hub in")
    ap.add_argument("--base-url", default=os.environ.get("TELESTHETE_RELAY_BASE_URL", BASE),
                    help="releases base URL (a mirror) [env TELESTHETE_RELAY_BASE_URL]")
    a = ap.parse_args(argv)

    if a.docker_arch:
        arch = DOCKER_ARCH.get(a.docker_arch.strip("/"))
        if not arch:
            ap.error(f"no prebuilt relay for Docker arch {a.docker_arch!r} "
                     f"(have {', '.join(DOCKER_ARCH)}); build with RELAY_SOURCE=source")
    else:
        arch = a.arch or {"amd64": "x86_64", "arm64": "aarch64", "armv7l": "armv7"}.get(
            os.uname().machine, os.uname().machine)
        if arch not in ARCHES:
            ap.error(f"no prebuilt relay for {arch!r} (have {', '.join(ARCHES)})")

    pinned_tag, pinned = read_pins(a.pins)
    version = a.version or pinned_tag
    if not version:
        ap.error(f"no relay pin in {a.pins}: pass --version vX.Y.Z (or latest)")
    name = f"telesthete-hub-{arch}-linux.tar.gz"
    url = asset_url(a.base_url, version, name)
    want = pinned.get(arch, "") if version == pinned_tag else ""
    try:
        tarball = fetch(url)
        if not want:
            print(f"fetch-relay: WARNING: no pinned sha256 for telesthete {version} {arch}; "
                  "checking against the release's own .sha256", file=sys.stderr)
            sums = fetch(url + ".sha256").decode("utf-8", "replace")
            want = expected_sha256(sums, name)
    except (OSError, ValueError) as e:
        print(f"fetch-relay: could not download {url}: {e}", file=sys.stderr)
        return 1
    got = hashlib.sha256(tarball).hexdigest()
    if got != want:
        print(f"fetch-relay: checksum mismatch for {name}: expected {want}, got {got}", file=sys.stderr)
        return 1
    try:
        out = extract_binary(tarball, a.dest)
    except (KeyError, ValueError, tarfile.TarError, OSError) as e:
        print(f"fetch-relay: bad archive {name}: {e}", file=sys.stderr)
        return 1
    print(f"telesthete-hub {version} ({arch}) -> {out}  sha256 {got[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
