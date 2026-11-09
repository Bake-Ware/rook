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

from rook.remote import enrollment, setup_store
from rook.remote.enrollment import EnrollmentStore, JoinDenied, JoinLimited
from rook.remote.bootstrap import CombinedServer, InstallerAccessLogger


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


def test_application_access_log_does_not_contain_query_code():
    logger = Mock()
    request = SimpleNamespace(remote="127.0.0.1", method="GET", path="/worker",
                              path_qs="/worker?band=secret")
    InstallerAccessLogger(logger, "").log(request, SimpleNamespace(status=200), 0.1)
    assert "secret" not in str(logger.mock_calls)


