"""Signed hub release manifests.

A hub release is a tarball of one git commit (``git archive``) plus a manifest::

    {"schema": 1, "typ": "rook-hub-release", "build": 412,
     "version": "412.salty.otter", "commit": "<full sha>",
     "built_at": "2026-09-30T12:00:00+00:00",
     "filename": "rook-hub-412.salty.otter.tar.gz",
     "sha256": "<hex>", "size": 1234567, "url": "https://...",   # url optional
     "sig": "<base64 ed25519>"}

It is signed with the same ed25519 update key that signs worker bundles
(``rook/remote/update_keys.py``), but under its own domain prefix
(``rook-hub-release-v1``, see docs/design/permissions.md 4.2): the signed bytes
are ``PREFIX + canonical(manifest minus sig)``. A worker manifest can therefore
never pass as a hub release, and a hub release never verifies as a worker
bundle, grant or deauth order.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

TYP = "rook-hub-release"
PREFIX = b"rook-hub-release-v1\n"
SCHEMA = 1
VERSION_RE = re.compile(r"^[0-9]+\.[a-z]+\.[a-z]+$")
REQUIRED = ("schema", "typ", "build", "version", "commit", "filename", "sha256", "size")


class ManifestError(Exception):
    pass


def canonical(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def signed_bytes(manifest: dict) -> bytes:
    return PREFIX + canonical(manifest)


def sign(manifest: dict, signing_key) -> dict:
    sig = signing_key.sign(signed_bytes(manifest)).signature
    return {**manifest, "sig": base64.b64encode(sig).decode("ascii")}


def trusted_pubkey(explicit: str | None = None) -> str:
    """The public key releases must verify against: explicit value, then
    ``$ROOK_UPDATE_PUBKEY``, then the public half of this hub's own update
    signing key. Empty string when none is available (verification then fails)."""
    for cand in (explicit, os.environ.get("ROOK_UPDATE_PUBKEY")):
        if cand and cand.strip():
            return cand.strip()
    try:
        from rook.remote.update_keys import load_signing_key, public_key_b64
        sk = load_signing_key()
        return public_key_b64(sk) if sk is not None else ""
    except Exception:
        return ""


def check_fields(manifest: dict) -> None:
    missing = [k for k in REQUIRED if k not in manifest]
    if missing:
        raise ManifestError(f"manifest is missing {', '.join(missing)}")
    if manifest["typ"] != TYP:
        raise ManifestError(f"not a hub release manifest (typ={manifest['typ']!r})")
    if manifest["schema"] != SCHEMA:
        raise ManifestError(f"unsupported manifest schema {manifest['schema']!r}")
    if not isinstance(manifest["build"], int) or manifest["build"] < 1:
        raise ManifestError("build must be a positive integer")
    v = manifest["version"]
    if not isinstance(v, str) or not VERSION_RE.match(v) or not v.startswith(f"{manifest['build']}."):
        raise ManifestError(f"version {v!r} is not '<build>.<adjective>.<noun>'")
    fn = manifest["filename"]
    if not isinstance(fn, str) or "/" in fn or "\\" in fn or fn.startswith("."):
        raise ManifestError(f"bad filename {fn!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])):
        raise ManifestError("sha256 must be 64 lowercase hex characters")


def verify(manifest: dict, pubkey_b64: str | None = None) -> dict:
    """Return the manifest if its fields are sane and its signature verifies,
    else raise ManifestError. Fails closed: no key, no signature, no pass."""
    check_fields(manifest)
    key = trusted_pubkey(pubkey_b64)
    if not key:
        raise ManifestError("no trusted public key: pass --pubkey, set ROOK_UPDATE_PUBKEY, "
                            "or run on the hub that holds the update signing key")
    sig = manifest.get("sig")
    if not sig:
        raise ManifestError("manifest is unsigned")
    try:
        from nacl.signing import VerifyKey
        VerifyKey(base64.b64decode(key)).verify(signed_bytes(manifest), base64.b64decode(sig))
    except Exception:
        raise ManifestError("bad signature (not signed by the trusted update key)") from None
    return manifest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- fetching -----------------------------------------------------------------

def _is_url(s: str) -> bool:
    return urllib.parse.urlparse(s).scheme in ("http", "https")


def _read(src: str, timeout: float = 60) -> bytes:
    if _is_url(src):
        with urllib.request.urlopen(src, timeout=timeout) as r:  # noqa: S310 (http/https only)
            return r.read()
    return Path(src).expanduser().read_bytes()


def load_manifest(src: str) -> dict:
    try:
        data = json.loads(_read(src))
    except (OSError, ValueError) as e:
        raise ManifestError(f"cannot read manifest {src}: {e}") from None
    if not isinstance(data, dict):
        raise ManifestError("manifest is not a JSON object")
    return data


def artifact_source(manifest: dict, manifest_src: str) -> str:
    """Where to get the tarball: the manifest's signed ``url`` if present, else
    ``filename`` next to the manifest (same directory or same URL base)."""
    if manifest.get("url"):
        return str(manifest["url"])
    if _is_url(manifest_src):
        return urllib.parse.urljoin(manifest_src, manifest["filename"])
    return str(Path(manifest_src).expanduser().resolve().parent / manifest["filename"])


def fetch_artifact(manifest: dict, manifest_src: str, dest_dir: Path) -> Path:
    """Download/copy the artifact into dest_dir and check size + sha256."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / manifest["filename"]
    if dest.exists() and sha256_file(dest) == manifest["sha256"]:
        return dest
    src = artifact_source(manifest, manifest_src)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(_read(src, timeout=300))
    except OSError as e:
        raise ManifestError(f"cannot fetch artifact {src}: {e}") from None
    size = tmp.stat().st_size
    if size != manifest["size"]:
        tmp.unlink()
        raise ManifestError(f"artifact size {size} != manifest size {manifest['size']}")
    got = sha256_file(tmp)
    if got != manifest["sha256"]:
        tmp.unlink()
        raise ManifestError(f"artifact sha256 {got} does not match the signed manifest")
    os.replace(tmp, dest)
    return dest


# -- building -----------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


def build_release(repo: Path, out_dir: Path, ref: str = "HEAD",
                  url_base: str = "", signing_key=None) -> tuple[Path, Path, dict]:
    """``git archive`` one commit into ``out_dir`` and write its signed manifest.

    Only committed content goes in: a dirty working tree cannot leak into a
    release. Returns (tarball, manifest_path, manifest)."""
    from rook.remote.build_band_worker import build_name
    if signing_key is None:
        from rook.remote.update_keys import key_path, load_signing_key
        signing_key = load_signing_key()
        if signing_key is None:
            raise ManifestError(f"no update signing key at {key_path()}; refusing to "
                                "build an unsigned hub release")
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    build = int(_git(repo, "rev-list", "--count", commit))
    version = f"{build}.{build_name(commit[:7])}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball = out_dir / f"rook-hub-{version}.tar.gz"
    subprocess.run(["git", "-C", str(repo), "archive", "--format=tar.gz",
                    f"--prefix={version}/", "-o", str(tarball), commit], check=True)
    manifest = {
        "schema": SCHEMA, "typ": TYP, "build": build, "version": version,
        "commit": commit,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "filename": tarball.name, "sha256": sha256_file(tarball),
        "size": tarball.stat().st_size,
    }
    if url_base:
        manifest["url"] = f"{url_base.rstrip('/')}/{tarball.name}"
    manifest = sign(manifest, signing_key)
    mpath = out_dir / f"rook-hub-{version}.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out_dir / "rook-hub-latest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                                  encoding="utf-8")
    return tarball, mpath, manifest
