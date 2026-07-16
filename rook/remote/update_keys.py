#!/usr/bin/env python3
"""Manage the ed25519 signing key for OTA update manifests (build-host side).

    python rook/remote/update_keys.py generate   # create a keypair (one time)
    python rook/remote/update_keys.py pubkey      # print pubkey of existing key

The PRIVATE key lives OUTSIDE the repo — default ``~/.config/rook/update-signing-key``
(override with ``$ROOK_UPDATE_KEY``), mode 0600. It never gets committed. The
PUBLIC key is printed for you to paste into ``rook/worker/_update_pubkey.py``
(committed), which is what every worker uses to verify a manifest before it
swaps its bundle.

``sign_manifest`` is imported by build_band_worker.py to sign each build.
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
    return Path.home() / ".config" / "rook" / "update-signing-key"


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
    sk = load_signing_key()
    if sk is None:
        print(f"WARNING: no signing key at {key_path()} — manifest will be "
              "UNSIGNED (workers will refuse to auto-update). Run "
              "`python rook/remote/update_keys.py generate` on the build host.",
              file=sys.stderr)
        return {**manifest, "sig": ""}
    sig = sk.sign(_canonical_payload(manifest)).signature
    return {**manifest, "sig": base64.b64encode(sig).decode("ascii")}


def generate() -> None:
    from nacl.signing import SigningKey
    p = key_path()
    if p.exists():
        print(f"Key already exists at {p} — refusing to overwrite.\n"
              "Delete it manually to rotate (and re-paste the new pubkey).",
              file=sys.stderr)
        sys.exit(1)
    sk = SigningKey.generate()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(base64.b64encode(bytes(sk)).decode("ascii") + "\n")
    p.chmod(0o600)
    pub = base64.b64encode(bytes(sk.verify_key)).decode("ascii")
    print(f"Private key written to {p} (0600 — keep it here, never commit).\n")
    print("Paste this into rook/worker/_update_pubkey.py and commit:\n")
    print(f'    PUBKEY_B64 = "{pub}"\n')


def pubkey() -> None:
    sk = load_signing_key()
    if sk is None:
        print(f"No key at {key_path()}", file=sys.stderr)
        sys.exit(1)
    print(base64.b64encode(bytes(sk.verify_key)).decode("ascii"))


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
