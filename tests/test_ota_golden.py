"""Golden vectors for OTA manifest signing.

``fixtures/ota_manifest_golden.json`` pins, for a fixed manifest and a fixed
(public, test-only) key, the exact canonical bytes and signature. Deployed
workers verify with their own copy of ``canonical_payload``; if the signer
or verifier drifts by one byte (key order, separators, escaping), every
update is refused (or worse, a different body verifies). Both signers must
reproduce the fixture byte for byte, and the verifier must accept it.
The vector was also checked against master's (9f0d6df) verifier.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

nacl_signing = pytest.importorskip("nacl.signing")

from rook.hubdeploy import manifest as hub_manifest  # noqa: E402
from rook.remote import update_keys  # noqa: E402
from rook.worker import _update_verify  # noqa: E402

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "ota_manifest_golden.json")
                    .read_text(encoding="utf-8"))


def _key():
    seed = hashlib.sha256(GOLDEN["seed_label"].encode("utf-8")).digest()
    sk = nacl_signing.SigningKey(seed)
    assert base64.b64encode(bytes(sk.verify_key)).decode() == GOLDEN["pubkey"]
    return sk


def test_worker_canonical_bytes_are_pinned():
    got = _update_verify.canonical_payload(GOLDEN["worker_manifest"])
    assert got == GOLDEN["worker_canonical"].encode("ascii")
    # sig is never part of what is signed
    signed = {**GOLDEN["worker_manifest"], "sig": GOLDEN["worker_sig"]}
    assert _update_verify.canonical_payload(signed) == got


def test_worker_verifier_accepts_the_golden_signature_and_nothing_else():
    good = {**GOLDEN["worker_manifest"], "sig": GOLDEN["worker_sig"]}
    assert _update_verify.verify_manifest(good, GOLDEN["pubkey"])
    assert not _update_verify.verify_manifest({**good, "build": 402}, GOLDEN["pubkey"])
    assert not _update_verify.verify_manifest({**good, "sig": GOLDEN["hub_sig"]}, GOLDEN["pubkey"])


def test_build_host_signer_reproduces_the_golden_signature(monkeypatch):
    sk = _key()
    monkeypatch.setattr(update_keys, "ensure_key", lambda: sk)
    signed = update_keys.sign_manifest(dict(GOLDEN["worker_manifest"]))
    assert signed["sig"] == GOLDEN["worker_sig"]          # ed25519 is deterministic
    assert update_keys._canonical_payload(signed) == GOLDEN["worker_canonical"].encode("ascii")


def test_hub_release_signer_and_verifier_match_the_golden_vector():
    sk = _key()
    hub = GOLDEN["hub_release"]
    assert hub_manifest.signed_bytes(hub) == base64.b64decode(GOLDEN["hub_signed_bytes_b64"])
    signed = hub_manifest.sign(dict(hub), sk)
    assert signed["sig"] == GOLDEN["hub_sig"]
    assert hub_manifest.verify(signed, GOLDEN["pubkey"]) == signed
    # Domain separation: a hub release never verifies as a worker bundle.
    assert not _update_verify.verify_manifest(signed, GOLDEN["pubkey"])
