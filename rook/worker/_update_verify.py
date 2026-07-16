"""Verify signed OTA update manifests.

A manifest is a JSON object describing the current published bundle::

    {"schema": 1, "build": 42, "version": "42.abc1234", "commit": "abc1234",
     "built_at": "2026-07-16T...", "filename": "band-worker.pyz",
     "sha256": "<hex>", "size": 179575, "sig": "<base64 ed25519 sig>"}

The signature is ed25519 over the *canonical* JSON of every field except
``sig`` (sorted keys, no whitespace). The build host holds the private key; the
worker verifies with the public key baked into :mod:`_update_pubkey`. Verifying
the manifest signature — and then checking the downloaded bundle's sha256
against the manifest — means an attacker who reaches the band (or the download
origin) still can't push code without the signing key. Fail closed everywhere.
"""

from __future__ import annotations

import base64
import hashlib
import json


def canonical_payload(manifest: dict) -> bytes:
    """The exact bytes that get signed/verified: manifest minus ``sig``,
    sorted keys, no spaces. Both signer and verifier must produce this."""
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest(manifest: dict, pubkey_b64: str | None = None) -> bool:
    """True iff the manifest carries a valid ed25519 signature for the given
    (or baked-in) public key. Returns False on any error — never raises."""
    if pubkey_b64 is None:
        from ._update_pubkey import PUBKEY_B64 as pubkey_b64
    sig = manifest.get("sig")
    if not sig or not pubkey_b64:
        return False
    try:
        from nacl.signing import VerifyKey
        vk = VerifyKey(base64.b64decode(pubkey_b64))
        vk.verify(canonical_payload(manifest), base64.b64decode(sig))
        return True
    except Exception:
        return False


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
