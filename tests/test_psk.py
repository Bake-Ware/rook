"""Readable keys through setup, band creation, installers, and transport crypto."""

import base64
import hashlib
import re
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from nacl.exceptions import CryptoError

from rook.remote import psk, setup_store
from rook.remote.bootstrap import CombinedServer
from rook.worker.transports.telesthete_hub import TelestheteHubTransport
from telesthete.protocol.crypto import BandCrypto, derive_band_id

AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:test-password").decode()}


def assert_phrase(key):
    assert re.fullmatch(r"[a-z]+(?:-[a-z]+){4}", key)
    assert all(w in psk._words() for w in key.split("-"))
    assert len(key) <= 49


def test_wordlist_retains_full_unique_dictionary():
    words = psk._words()
    assert len(words) == len(set(words)) == 7776
    assert all(re.fullmatch(r"[a-z]{3,9}", w) for w in words)


def test_generation_uses_independent_secure_draws_with_replacement(monkeypatch):
    # A repeating random draw must remain valid; don't filter duplicates or
    # select from the small, predictable build-name vocabulary.
    draws = []

    def choose(words):
        draws.append(words)
        return "rook"

    monkeypatch.setattr(psk.secrets, "choice", choose)
    assert setup_store.gen_psk() == "rook-rook-rook-rook-rook"
    assert len(draws) == 5
    assert all(len(words) == 7776 for words in draws)


def test_generated_keys_have_the_requested_shape():
    for _ in range(20):
        assert_phrase(setup_store.gen_psk())


@pytest.mark.parametrize("key", [None, "band-AbCd_0123456789-legacy"])
def test_worker_and_controller_share_keys_without_protocol_changes(key):
    key = key or setup_store.gen_psk()
    worker = TelestheteHubTransport(psk=key, hub_host="127.0.0.1")
    controller = BandCrypto(key)
    assert controller.band_id == hashlib.sha256(key.encode()).digest()[:16]
    assert worker._crypto.band_id == controller.band_id
    request = b'{"id":"test","cap":"info.ping","args":{}}'
    ciphertext = controller.encrypt(17, request)
    assert worker._crypto.decrypt(17, ciphertext) == request
    with pytest.raises(CryptoError):
        BandCrypto(key + "-typo").decrypt(17, ciphertext)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    app = CombinedServer(web_user="owner", web_pass="test-password",
                         band_psk="legacy-Existing_KEY")
    yield app
    if app._chat:
        app._chat.close()


