"""Each hub generates its own update key and stamps it into the bundles it builds."""
import base64
import importlib.util
import os
import stat

import pytest

from rook.remote import update_keys
from rook.remote.build_band_worker import _stamp_pubkey
from rook.worker import _update_pubkey
from rook.worker._update_verify import verify_manifest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ROOK_UPDATE_KEY", raising=False)
    monkeypatch.delenv("ROOK_UPDATE_PUBKEY", raising=False)
    monkeypatch.delenv("ROOK_DATA_DIR", raising=False)
    return tmp_path / "home"


def test_source_tree_trusts_no_key(home):
    assert _update_pubkey.PUBKEY_B64 == ""
    signed = update_keys.sign_manifest({"schema": 1, "build": 2})
    assert not verify_manifest(signed)


def test_key_location(home, tmp_path, monkeypatch):
    legacy = home / ".config" / "rook" / "update-signing-key"
    assert update_keys.key_path() == legacy
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    assert update_keys.key_path() == tmp_path / "data" / "update-signing-key"
    # An existing key in the historical location keeps being used.
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x")
    assert update_keys.key_path() == legacy
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "explicit"))
    assert update_keys.key_path() == tmp_path / "explicit"


def test_ensure_key_creates_once_with_private_mode(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    first = update_keys.ensure_key()
    path = tmp_path / "data" / "update-signing-key"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert bytes(update_keys.ensure_key()) == bytes(first)


def test_bundle_trusts_exactly_its_hubs_key(home, tmp_path, monkeypatch):
    worker = tmp_path / "bundle"
    worker.mkdir()
    _stamp_pubkey(worker)
    spec = importlib.util.spec_from_file_location("stamped", worker / "_update_pubkey.py")
    stamped = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stamped)
    sk = update_keys.load_signing_key()
    assert stamped.PUBKEY_B64 == base64.b64encode(bytes(sk.verify_key)).decode()

    manifest = update_keys.sign_manifest({"schema": 1, "build": 3, "sha256": "00"})
    assert verify_manifest(manifest, stamped.PUBKEY_B64)
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "other-hub"))
    other = update_keys.sign_manifest({"schema": 1, "build": 3, "sha256": "00"})
    assert not verify_manifest(other, stamped.PUBKEY_B64)


def test_signers_and_verifier_agree(home, tmp_path, monkeypatch):
    """update_keys (worker bundles) and hubdeploy (hub releases) sign exactly
    the bytes the worker's verifier checks: one canonical form, one digest."""
    from rook.hubdeploy import manifest as hub_manifest
    from rook.worker import _update_verify
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    sk = update_keys.ensure_key()
    pub = update_keys.public_key_b64(sk)
    # Non-ASCII and unordered keys exercise the canonical form.
    m = {"schema": 1, "build": 7, "version": "7.salty.otter", "note": "café", "a": [2, 1]}
    signed = update_keys.sign_manifest(m)
    assert _update_verify.verify_manifest(signed, pub)
    assert not _update_verify.verify_manifest({**signed, "build": 8}, pub)
    assert hub_manifest.canonical is _update_verify.canonical_payload
    assert hub_manifest.sha256_file is _update_verify.sha256_file
    assert update_keys._canonical_payload(m) == _update_verify.canonical_payload(m)
    # A hub release verifies only with its domain prefix: never as a worker bundle.
    rel = {"schema": 1, "typ": hub_manifest.TYP, "build": 7, "version": "7.salty.otter",
           "commit": "c" * 40, "filename": "rook-hub-7.salty.otter.tar.gz",
           "sha256": "0" * 64, "size": 1}
    srel = hub_manifest.sign(rel, sk)
    assert hub_manifest.verify(srel, pub) == srel
    assert not _update_verify.verify_manifest(srel, pub)
    import nacl.signing
    nacl.signing.VerifyKey(base64.b64decode(pub)).verify(
        hub_manifest.PREFIX + _update_verify.canonical_payload(rel),
        base64.b64decode(srel["sig"]))
    f = tmp_path / "blob"
    f.write_bytes(b"x" * 3_000_000)
    import hashlib
    assert hub_manifest.sha256_file(f) == hashlib.sha256(f.read_bytes()).hexdigest()
