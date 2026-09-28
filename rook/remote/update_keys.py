#!/usr/bin/env python3
"""Manage the ed25519 signing key for OTA update manifests (build-host side).

    python rook/remote/update_keys.py generate   # create a keypair (one time)
    python rook/remote/update_keys.py pubkey      # print pubkey of existing key

The hub creates the key on first start (``ensure_key``), so running these by
hand is optional. The PRIVATE key never leaves the hub: ``$ROOK_UPDATE_KEY``
if set, else ``~/.config/rook/update-signing-key`` when that file already
exists, else ``$ROOK_DATA_DIR/update-signing-key`` (the Docker image and the
quickstart), mode 0600. ``build_band_worker.py`` signs each build with it and
writes the PUBLIC half into the bundle, which is what workers verify updates
and deauth orders against.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def key_path() -> Path:
    env = os.environ.get("ROOK_UPDATE_KEY")
    if env:
        return Path(env).expanduser()
    legacy = Path.home() / ".config" / "rook" / "update-signing-key"
    data = os.environ.get("ROOK_DATA_DIR", "").strip()
    if data and not legacy.exists():
        return Path(data).expanduser() / "update-signing-key"
    return legacy


def _canonical_payload(manifest: dict) -> bytes:
    """MUST match rook.worker._update_verify.canonical_payload byte-for-byte."""
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_signing_key():
    """Return the nacl SigningKey, or None if no key file exists."""
    p = key_path()
    if not p.exists():
        return None
    from nacl.signing import SigningKey
    return SigningKey(base64.b64decode(p.read_text().strip()))


def sign_manifest(manifest: dict) -> dict:
    """Return the manifest with a base64 ed25519 ``sig`` added. If no signing
    key is present, sets ``sig`` to "" and warns — the worker will reject the
    unsigned manifest (fail closed), so builds don't break but won't auto-ship."""
    sk = ensure_key()
    if sk is None:
        print("WARNING: manifest is UNSIGNED; workers will refuse to auto-update.",
              file=sys.stderr)
        return {**manifest, "sig": ""}
    sig = sk.sign(_canonical_payload(manifest)).signature
    return {**manifest, "sig": base64.b64encode(sig).decode("ascii")}


def public_key_b64(sk) -> str:
    return base64.b64encode(bytes(sk.verify_key)).decode("ascii")


def _create(p: Path):
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(bytes(sk)).decode("ascii") + "\n")
    return sk


def ensure_key():
    """Return the signing key, creating it on first use. Returns None only if
    the key file cannot be written."""
    sk = load_signing_key()
    if sk is not None:
        return sk
    p = key_path()
    try:
        sk = _create(p)
    except FileExistsError:  # created concurrently (e.g. dashboard and a build)
        return load_signing_key()
    except OSError as e:
        print(f"WARNING: could not create update signing key at {p}: {e}", file=sys.stderr)
        return None
    print(f"Created update signing key {p}; workers built by this hub will "
          f"trust public key {public_key_b64(sk)}", file=sys.stderr)
    return sk


def generate() -> None:
    p = key_path()
    if p.exists():
        print(f"Key already exists at {p} — refusing to overwrite.\n"
              "Delete it manually to rotate; rebuilt bundles pick up the new key, "
              "but workers still running old bundles will reject its updates.",
              file=sys.stderr)
        sys.exit(1)
    sk = _create(p)
    print(f"Private key written to {p} (0600 — keep it here, never commit).")
    print(f"Public key (built into worker bundles): {public_key_b64(sk)}")


def pubkey() -> None:
    sk = load_signing_key()
    if sk is None:
        print(f"No key at {key_path()}", file=sys.stderr)
        sys.exit(1)
    print(public_key_b64(sk))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "generate":
        generate()
    elif cmd == "pubkey":
        pubkey()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
