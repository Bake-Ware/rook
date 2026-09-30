"""Dashboard Permissions page and policy API (permissions.md 3.10)."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.hub.policy import DEFAULT_POLICY
from test_setup_auth import AUTH, SETUP, env  # noqa: F401  (fixture)


@pytest.mark.asyncio
async def test_policy_page_api_save_and_explain(env, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    server = env(web_user="owner", web_pass="test-password-1")
    from rook.remote import setup_store
    setup_store.save({k: SETUP[k] for k in ("band_name", "band_psk", "hub_public")})
    async with TestClient(TestServer(server._app)) as client:
        assert (await client.get("/api/policy")).status == 401           # behind the login
        page = await client.get("/permissions", headers=AUTH)
        assert page.status == 200 and "audit" in await page.text()
        got = await (await client.get("/api/policy", headers=AUTH)).json()
        assert got["mode"] == "audit" and got["rev"] == 0 and got["tickets"] is False
        # The shared dashboard password is an owner: it may save.
        saved = await client.post("/api/policy", headers=AUTH,
                                  json={"policy": {**DEFAULT_POLICY, "mode": "enforce"}, "note": "go"})
        assert saved.status == 200 and (await saved.json())["rev"] == 1
        disk = json.loads((tmp_path / "data" / "policy.json").read_text())
        assert disk["mode"] == "enforce" and disk["rev"] == 1
        cross = await client.post("/api/policy", headers={**AUTH, "Origin": "https://evil.example"},
                                  json={"policy": DEFAULT_POLICY})
        assert cross.status == 403
        bad = await client.post("/api/policy", headers=AUTH, json={"policy": {"mode": "loud"}})
        assert bad.status == 400
        why = await (await client.post("/api/policy/explain", headers=AUTH, json={
            "principal": "integration:telegram", "cap": "shell.exec", "worker": "box"})).json()
        assert why["decision"] == "deny" and why["tier"] == "exec"
