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
