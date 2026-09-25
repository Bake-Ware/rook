"""Regressions for a fresh self-hosted hub: env-only band config, an MCP server
without a public URL on a non-default port, ROOK_DATA_DIR, and an operator's
own OTA key."""
import base64

import httpx
import pytest
from nacl.signing import SigningKey

from rook.band_mcp.server import build_server
from rook.remote import setup_store
from rook.worker._update_verify import canonical_payload, verify_manifest

STATIC = "static-token-0123456789abcdef"


class FakeBand:
    workers = {"w1": {"worker_id": "w1", "name": "demo", "band": "x",
                      "caps": ["shell.exec"], "last_seen": 0}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        return {"id": "c", "from": target, "ok": True, "result": {}}


def test_data_dir_holds_hub_state(tmp_path, monkeypatch):
    monkeypatch.delenv("ROOK_SETUP_PATH", raising=False)
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path))
    assert setup_store.setup_path() == tmp_path / "setup.json"
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "elsewhere.json"))
    assert setup_store.setup_path() == tmp_path / "elsewhere.json"


def test_env_configured_dashboard_skips_the_setup_wizard(tmp_path, monkeypatch):
    from rook.remote import bootstrap
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROOK_SETUP_PATH", raising=False)
    monkeypatch.setattr(bootstrap, "CombinedServer", lambda **kw: None)
    monkeypatch.setattr(bootstrap.asyncio, "run", lambda coro: coro.close())

    def start(*argv):
        monkeypatch.setattr("sys.argv", ["rook-dashboard", "--bind", "127.0.0.1", *argv])
        bootstrap._cli_main()

    start("--psk", "alpha-bravo-charlie-delta-echo", "--hub-public", "hub.example.com:443")
    assert setup_store.is_configured()
    assert setup_store.load()["band_psk"] == "alpha-bravo-charlie-delta-echo"
    # A later start with another key never replaces the saved configuration.
    start("--psk", "other-key-words-here-now")
    assert setup_store.load()["band_psk"] == "alpha-bravo-charlie-delta-echo"


def test_dashboard_refuses_public_bind_without_password(monkeypatch):
    from rook.remote import bootstrap
    monkeypatch.delenv("ROOK_WEB_PASS", raising=False)
    monkeypatch.setattr("sys.argv", ["rook-dashboard", "--bind", "0.0.0.0"])
    with pytest.raises(SystemExit):
        bootstrap._cli_main()


@pytest.mark.asyncio
async def test_mcp_without_public_url_serves_loopback_on_any_port(tmp_path):
    mcp, _ = build_server(FakeBand(), persist_path=str(tmp_path / "tokens.json"),
                          static_token=STATIC, journal_path=str(tmp_path / "journal.db"))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18765",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}}
        assert (await http.post("/mcp", json=init)).status_code == 401
        r = await http.post("/mcp", json=init, headers={"Authorization": "Bearer " + STATIC})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")


def test_operator_update_key_from_environment(monkeypatch):
    sk = SigningKey.generate()
    manifest = {"schema": 1, "build": 1, "sha256": "00"}
    manifest["sig"] = base64.b64encode(sk.sign(canonical_payload(manifest)).signature).decode()
    monkeypatch.delenv("ROOK_UPDATE_PUBKEY", raising=False)
    assert not verify_manifest(manifest)          # not signed by the baked-in key
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", base64.b64encode(bytes(sk.verify_key)).decode())
    assert verify_manifest(manifest)
