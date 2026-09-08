"""Pairing is scoped, temporary, shared across services, and revoked atomically."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from rook.remote import enrollment, setup_store
from rook.remote.enrollment import EnrollmentStore, JoinDenied, JoinLimited
from rook.remote.bootstrap import CombinedServer, InstallerAccessLogger
from rook.band_mcp.api_tokens_ui import build_api_token_routes
from rook.band_mcp.client import MultiBandClient


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": "hub.example.com:443", "pyz_domain": "rook.example.com"})
    s = EnrollmentStore()
    s.import_config()
    return s


def primary(store):
    return next(b for b in store.bands(secrets_visible=True) if b["is_primary"])


def test_code_shape_and_band_scope(store):
    first = primary(store)
    second = store.register("other", "other-band-permanent-key", "other.example.com:443")
    grant = store.issue(first["id"])
    assert re.fullmatch(r"[a-z0-9]{6}", grant["code"])
    assert len(set(enrollment._ALPHABET)) == 32
    assert store.redeem(grant["code"], "peer")["psk"] == first["psk"]
    assert store.issue(first["id"])["code"] == grant["code"]
    assert store.issue(second["id"])["code"] != grant["code"]
    assert all("psk" not in b for b in store.bands())


def test_expiry_rolls_only_the_code_and_preserves_permanent_key(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(enrollment, "time", SimpleNamespace(time=lambda: clock[0]))
    band = primary(store)
    grant = store.issue(band["id"])
    clock[0] = grant["expires"]
    with pytest.raises(JoinDenied):
        store.redeem(grant["code"], "peer")
    new = store.issue(band["id"], grant["session"])
    assert new["session"] == grant["session"]
    assert new["expires"] > grant["expires"]
    assert store.redeem(new["code"], "peer")["psk"] == band["psk"]


def test_revocation_stops_other_tabs_from_refreshing_code(store):
    grant = store.issue(primary(store)["id"])
    store.revoke_code(grant["band_id"])
    with pytest.raises(JoinDenied):
        store.redeem(grant["code"], "peer")
    with pytest.raises(ValueError, match="stopped"):
        store.issue(grant["band_id"], grant["session"])
    # An explicit new pairing session is allowed.
    new = store.issue(grant["band_id"])
    assert new["session"] != grant["session"]


def test_rotation_preserves_band_record_and_retires_stale_config(store):
    band = primary(store)
    grant = store.issue(band["id"])
    new = store.rotate(band["id"])
    assert new["id"] == band["id"]
    assert re.fullmatch(r"[a-z]+(?:-[a-z]+){4}", new["psk"])
    assert new["epoch"] == band["epoch"] + 1
    with pytest.raises(JoinDenied):
        store.redeem(grant["code"], "peer")
    restarted = EnrollmentStore()
    restarted.import_config([band["psk"]])  # stale setup.json and service env
    active = restarted.bands(active_only=True, secrets_visible=True)
    assert len(active) == 1
    assert active[0]["id"] == band["id"] and active[0]["psk"] == new["psk"]
    with pytest.raises(ValueError):
        restarted.register("old", band["psk"])
    with pytest.raises(ValueError):
        restarted.rotate(band["id"], band["psk"])


def test_revoked_band_cannot_join_until_key_is_replaced(store):
    band = primary(store)
    grant = store.issue(band["id"])
    store.revoke(band["id"])
    assert store.bands(active_only=True) == []
    with pytest.raises(ValueError):
        store.issue(band["id"])
    with pytest.raises(JoinDenied):
        store.redeem(grant["code"], "peer")
    store.import_config()
    assert store.bands(active_only=True) == []
    store.rotate(band["id"])
    assert len(store.bands(active_only=True)) == 1


def test_attempt_limits_are_shared_across_connections_and_commit_on_failure(store, monkeypatch):
    clock = [1200]
    monkeypatch.setattr(enrollment, "time", SimpleNamespace(time=lambda: clock[0]))
    grant = store.issue(primary(store)["id"])
    for _ in range(enrollment._PEER_ATTEMPTS):
        with pytest.raises(JoinDenied):
            EnrollmentStore().redeem("xxxxxx", "same-peer")
    with pytest.raises(JoinLimited):
        store.redeem(grant["code"], "same-peer")
    clock[0] += 60
    assert store.redeem(grant["code"], "same-peer")["active"]


def test_parallel_redeemers_cannot_exceed_global_budget(store, monkeypatch):
    monkeypatch.setattr(enrollment, "time", SimpleNamespace(time=lambda: 1200))
    grant = store.issue(primary(store)["id"])

    def redeem(index):
        try:
            EnrollmentStore().redeem(grant["code"], str(index))
            return True
        except JoinLimited:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(redeem, range(40)))
    assert sum(results) == enrollment._GLOBAL_ATTEMPTS


@pytest.fixture
def server(store):
    server = CombinedServer(web_user="owner", web_pass="password")
    yield server
    if server._chat:
        server._chat.close()


@pytest.mark.asyncio
async def test_installer_requires_code_even_for_logged_in_admin(server, store):
    async with TestClient(TestServer(server._app)) as client:
        auth = "Basic " + base64.b64encode(b"owner:password").decode()
        for headers in ({}, {"Authorization": auth}):
            response = await client.get("/worker", headers=headers)
            assert response.status == 403
            assert primary(store)["psk"] not in await response.text()


@pytest.mark.asyncio
async def test_installer_selects_only_authorized_band_and_preserves_windows_retry(server, store):
    other = store.register("phone", "phone-words-remain-very-secret", "phone.example.com:443")
    code = store.issue(other["id"])["code"]
    async with TestClient(TestServer(server._app)) as client:
        for target in ("unix", "windows"):
            response = await client.get("/worker", params={"band": code, "os": target})
            assert response.status == 200
            text = await response.text()
            assert other["psk"] not in text and primary(store)["psk"] not in text
            assert f'--pair-code {code}' in text and '--enrolled --ws' in text
            assert 'band-worker-enrollment.pyz' in text
            assert response.headers["Cache-Control"] == "no-store"
            if target == "windows":
                assert f'/worker?band={code}&os=windows' in text
                assert '/worker?os=windows' not in text
        response = await client.get('/worker?login=google')
        text = await response.text()
        assert response.status == 200 and '--enroll https://rook.example.com' in text
        assert '--pair-code' not in text and primary(store)['psk'] not in text
        response = await client.get("/install", params={"band": code})
        text = await response.text()
        assert code in text and other["psk"] not in text


@pytest.mark.asyncio
async def test_config_fetch_and_revocation_between_services(server, store):
    band = primary(store)
    code = store.issue(band["id"])["code"]
    async with TestClient(TestServer(server._app)) as client:
        response = await client.post("/enroll", json={"code": code})
        assert response.status == 200
        data = await response.json()
        assert data["psk"] == band["psk"] and data["band_id"] == band["id"]
        EnrollmentStore().rotate(band["id"])
        response = await client.get("/worker", params={"band": code})
        assert response.status == 403
        response = await client.post("/enroll", json={"code": code})
        assert response.status == 403


@pytest.mark.asyncio
async def test_legacy_apk_is_not_an_anonymous_credential_download(server):
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/apk")
        assert response.status == 401


def test_application_access_log_does_not_contain_query_code():
    logger = Mock()
    request = SimpleNamespace(remote="127.0.0.1", method="GET", path="/worker",
                              path_qs="/worker?band=secret")
    InstallerAccessLogger(logger, "").log(request, SimpleNamespace(status=200), 0.1)
    assert "secret" not in str(logger.mock_calls)


class AdminProvider:
    def admin_session_ok(self, sid):
        return sid == "admin-session"

    def list_api_tokens(self):
        return []


def test_tokens_page_pairing_controls_require_session_and_same_origin(store):
    app = Starlette(routes=build_api_token_routes(AdminProvider()))
    with ASGIClient(app) as client:
        uid = primary(store)["id"]
        request = {"band_id": uid}
        headers = {"X-Rook-Request": "tokens"}
        assert client.post("/tokens/pairing", json=request, headers=headers).status_code == 401
        client.cookies.set("rook_admin", "admin-session")
        assert client.post("/tokens/pairing", json=request).status_code == 403
        assert client.post("/tokens/pairing", json=request, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        page = client.get("/tokens")
        assert 'id="pair-code"' in page.text and 'id="pair-rotate"' in page.text
        assert page.headers["Cache-Control"] == "no-store"
        response = client.post("/tokens/pairing", json=request, headers=headers)
        assert response.status_code == 200
        grant = response.json()
        assert "https://rook.example.com/worker?band=" in grant["command"]
        assert primary(store)["psk"] not in response.text
        response = client.post("/tokens/bands/rotate", json=request, headers=headers)
        assert response.status_code == 200
        with pytest.raises(JoinDenied):
            store.redeem(grant["code"], "peer")


@pytest.mark.asyncio
async def test_live_controller_leaves_old_band_and_can_revoke_last_band(store):
    band = primary(store)
    client = MultiBandClient([band["psk"]], hub_host="127.0.0.1", hub_port=59999)
    await client.start()
    old = client._clients[0]
    try:
        rotated = store.rotate(band["id"])
        await client.sync_bands([rotated["psk"]])
        assert old not in client._clients
        assert len(client._clients) == 1
        await client.sync_bands([rotated["psk"]])
        assert len(client._clients) == 1
        await client.sync_bands([])
        assert client.workers == {}
        with pytest.raises(ConnectionError):
            await client.call("info.ping")
    finally:
        await client.stop()


def test_dongle_header_escapes_psk_and_uses_udp_endpoint():
    path = Path(__file__).resolve().parents[1] / "firmware/scripts/dongle-band-config.py"
    spec = importlib.util.spec_from_file_location("dongle_band_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.render_config({"psk": 'key"\\example', "hub": "hub.example.com:7474"})
    assert '#define HUB_PORT 7474' in text
    assert '#define BAND_PSK "key\\\"\\\\example"' in text
    with pytest.raises(ValueError):
        module.render_config({"psk": "valid", "hub": "host:99999"})
