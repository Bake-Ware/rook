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


@pytest.mark.asyncio
async def test_setup_suggests_a_readable_key_without_saving_it(server):
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/setup")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        key = re.search(r'name="band_psk" value="([^"]+)"', await response.text())[1]
        assert_phrase(key)
        assert not setup_store.setup_path().exists()


@pytest.mark.asyncio
async def test_existing_setup_keys_are_not_replaced(server):
    legacy = "band-AbCd_0123456789-legacy"
    setup_store.save({"band_psk": legacy, "hub_public": "hub.example.com:443"})
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/setup", headers=AUTH)
        assert response.status == 200
        assert f'name="band_psk" value="{legacy}"' in await response.text()
        assert setup_store.load()["band_psk"] == legacy


@pytest.mark.asyncio
async def test_psk_suggestion_requires_login_and_does_not_modify_bands(server):
    setup_store.save({"band_psk": server.band_psk, "hub_public": "hub.example.com:443"})
    before = setup_store.setup_path().read_bytes()
    async with TestClient(TestServer(server._app)) as client:
        response = await client.post("/api/bands/psk")
        assert response.status == 401
        response = await client.post("/api/bands/psk", headers=AUTH)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert_phrase((await response.json())["psk"])
        assert setup_store.setup_path().read_bytes() == before
        assert server.band_psk == "legacy-Existing_KEY"


@pytest.mark.asyncio
async def test_setup_round_trip_and_scoped_enrollment_keep_exact_key(server):
    key = setup_store.gen_psk()
    extra = {"name": "existing", "psk": "band-Old_CASE_sensitive"}
    setup_store.save_bands([extra])
    async with TestClient(TestServer(server._app)) as client:
        response = await client.post("/setup", data={
            "band_name": "test-band", "band_psk": key,
            "hub_public": "hub.example.com:443", "pyz_domain": "rook.example.com",
        }, allow_redirects=False)
        assert response.status == 302
        assert setup_store.load()["band_psk"] == server.band_psk == key
        assert extra in setup_store.load_bands()
        band = next(b for b in server._enrollment.bands(secrets_visible=True) if b["psk"] == key)
        code = server._enrollment.issue(band["id"])["code"]
        for target in ("unix", "windows"):
            response = await client.get("/worker", params={"os": target, "band": code})
            assert response.status == 200
            script = await response.text()
            assert key not in script
            assert f'--pair-code {code}' in script
        response = await client.post('/enroll', json={'code': code})
        assert response.status == 200
        assert (await response.json())['psk'] == key


@pytest.mark.asyncio
async def test_suggested_key_can_create_a_band_and_survives_reload(server):
    setup_store.save({"band_psk": server.band_psk, "hub_public": "hub.example.com:443"})
    server._band = AsyncMock()
    async with TestClient(TestServer(server._app), headers=AUTH) as client:
        response = await client.post("/api/bands/psk")
        key = (await response.json())["psk"]
        response = await client.post("/api/bands", json={"name": "phone", "psk": key})
        assert response.status == 200
        assert (await response.json())["id"] == derive_band_id(key).hex()[:8]
        assert any(b["name"] == "phone" and b["psk"] == key
                   for b in server._enrollment.bands(secrets_visible=True))
        assert key in server._band.sync_bands.await_args.args[0]
        response = await client.get("/api/bands")
        assert key not in await response.text()
