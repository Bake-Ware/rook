"""The beta APK channel (``/apk.json?channel=beta``): served only when
``ROOK_BETA_APK_SHA256`` approves it and it is newer than stable; every other
request gets exactly today's stable answer (installed phones poll /apk.json
with no query and must never see a beta)."""

import hashlib
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer


def _artifact(base, body: bytes, code: int, name: str, url: str) -> dict:
    base.mkdir(parents=True, exist_ok=True)
    (base / "rook-worker.apk").write_bytes(body)
    manifest = {"package": "systems.bake.rook", "version_code": code, "version_name": name,
                "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), "url": url}
    (base / "rook-worker-apk.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_UPDATES_DIR", str(tmp_path / "updates"))
    monkeypatch.delenv("ROOK_GOOGLE_CLIENT_FILE", raising=False)
    monkeypatch.delenv("ROOK_BETA_APK_SHA256", raising=False)
    setup_store.save({"band_name": "home", "band_psk": "old-key",
                      "hub_public": "hub.example.com:443", "pyz_domain": "rook.example.com"})
    updates = tmp_path / "updates"
    stable = _artifact(updates / "apk", b"stable apk" * 50, 30, "0.4.19",
                       "https://rook.example.com/apk")
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", stable["sha256"])
    server = CombinedServer(band_psk="old-key", web_user="operator", web_pass="operator-password",
                            domain="rook.example.com")
    return server, updates, stable


def _beta(updates, code=31, body=b"beta apk" * 50):
    return _artifact(updates / "apk-beta", body, code, "0.4.20-beta",
                     "https://rook.example.com/apk?channel=beta")


async def _get(client, path):
    r = await client.get(path, allow_redirects=False)
    return r.status, await r.read(), r.headers.get("Content-Type"), r.headers.get("Cache-Control")


STABLE_PATHS = ("/apk.json", "/apk", "/apk.json?channel=stable", "/apk?channel=stable",
                "/apk.json?channel=nightly", "/apk.json?channel=BETA", "/apk.json?channel=",
                "/apk.json?chan=beta", "/apk?channel=beta2")


@pytest.mark.asyncio
async def test_stable_requests_are_unchanged_when_a_beta_exists(hub, monkeypatch):
    server, updates, stable = hub
    async with TestClient(TestServer(server._app)) as client:
        before = {p: await _get(client, p) for p in STABLE_PATHS}
        assert before["/apk.json"][0] == 200 and json.loads(before["/apk.json"][1]) == stable
        assert before["/apk"][1] == b"stable apk" * 50
        beta = _beta(updates)
        monkeypatch.setenv("ROOK_BETA_APK_SHA256", beta["sha256"])
        status, body, _, _ = await _get(client, "/apk.json?channel=beta")
        assert status == 200 and json.loads(body) == beta
        after = {p: await _get(client, p) for p in STABLE_PATHS}
        assert after == before


@pytest.mark.asyncio
async def test_beta_served_when_approved_and_newer(hub, monkeypatch):
    server, updates, stable = hub
    beta = _beta(updates)
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", beta["sha256"])
    async with TestClient(TestServer(server._app)) as client:
        r = await client.get("/apk.json?channel=beta")
        assert r.status == 200 and await r.json() == beta
        assert r.headers["Cache-Control"] == "no-store"
        r = await client.get("/apk?channel=beta")
        assert r.status == 200 and await r.read() == b"beta apk" * 50
        assert r.headers["Content-Type"] == "application/vnd.android.package-archive"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unapproved", "other_hash", "tampered", "older", "same",
                                  "wrong_package", "size", "missing"])
async def test_beta_not_served_unless_approved_verified_and_newer(hub, monkeypatch, case):
    server, updates, stable = hub
    beta = _beta(updates, code={"older": 29, "same": 30}.get(case, 31))
    if case != "unapproved":
        monkeypatch.setenv("ROOK_BETA_APK_SHA256",
                           "0" * 64 if case == "other_hash" else beta["sha256"])
    if case == "tampered":
        (updates / "apk-beta" / "rook-worker.apk").write_bytes(b"BETA APK" * 50)
    if case == "missing":
        (updates / "apk-beta" / "rook-worker.apk").unlink()
    if case in ("wrong_package", "size"):
        bad = {**beta, **({"package": "com.evil"} if case == "wrong_package" else {"size": 1})}
        (updates / "apk-beta" / "rook-worker-apk.json").write_text(json.dumps(bad))
    async with TestClient(TestServer(server._app)) as client:
        r = await client.get("/apk.json?channel=beta")
        assert r.status == 200 and await r.json() == stable
        r = await client.get("/apk?channel=beta")
        assert r.status == 200 and await r.read() == b"stable apk" * 50


@pytest.mark.asyncio
async def test_beta_phone_rejoins_stable_once_stable_passes_it(hub, monkeypatch):
    server, updates, stable = hub
    beta = _beta(updates)
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", beta["sha256"])
    async with TestClient(TestServer(server._app)) as client:
        assert (await (await client.get("/apk.json?channel=beta")).json()) == beta
        newer = _artifact(updates / "apk", b"stable apk 2" * 50, 32, "0.4.21",
                          "https://rook.example.com/apk")
        monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", newer["sha256"])
        assert (await (await client.get("/apk.json?channel=beta")).json()) == newer


@pytest.mark.asyncio
async def test_beta_only_approval_never_opens_the_stable_apk(hub, monkeypatch, tmp_path):
    """With only the beta approved, /apk?channel=beta is public for the beta
    alone: an operator's stable copy that no gate approves (rook/remote, maybe
    a legacy build with a band key inside) stays behind login. A stable APK
    published from a release (updates/apk) is a verified generic build and is
    public on its own (rook hub release / the installer publish one)."""
    from rook.remote import bootstrap
    server, updates, stable = hub
    monkeypatch.delenv("ROOK_PUBLIC_APK_SHA256")
    legacy = tmp_path / "release" / "rook" / "remote"
    _artifact(legacy, b"stable apk" * 50, 30, "0.4.19", "https://rook.example.com/apk")
    monkeypatch.setattr(bootstrap, "__file__", str(legacy / "bootstrap.py"))
    published = updates / "apk"
    for f in published.iterdir():
        f.rename(tmp_path / f.name)
    beta = _beta(updates)
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", beta["sha256"])
    async with TestClient(TestServer(server._app)) as client:
        r = await client.get("/apk?channel=beta")
        assert r.status == 200 and await r.read() == b"beta apk" * 50
        for path in ("/apk", "/apk.json", "/apk.json?channel=stable"):
            r = await client.get(path, allow_redirects=False)
            assert r.status != 200 and b"stable apk" not in await r.read()
        for f in ("rook-worker.apk", "rook-worker-apk.json"):
            (tmp_path / f).rename(published / f)
        r = await client.get("/apk.json", allow_redirects=False)
        assert r.status == 200 and (await r.json())["sha256"] == stable["sha256"]
        _beta(updates, code=29)
        r = await client.get("/apk.json?channel=beta")
        assert r.status == 200 and (await r.json())["sha256"] == stable["sha256"]
