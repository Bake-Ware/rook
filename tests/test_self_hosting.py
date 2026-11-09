"""Regressions for a fresh self-hosted hub: env-only band config, an MCP server
without a public URL on a non-default port, ROOK_DATA_DIR, and an operator's
own OTA key."""
import base64

import httpx
import pytest
from nacl.signing import SigningKey

from rook.remote import setup_store
from rook.worker._update_verify import canonical_payload, verify_manifest

STATIC = "static-token-0123456789abcdef"


class FakeBand:
    workers = {"w1": {"worker_id": "w1", "name": "demo", "band": "x",
                      "caps": ["shell.exec"], "last_seen": 0}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        return {"id": "c", "from": target, "ok": True, "result": {}}


def test_dashboard_refuses_public_bind_without_password(monkeypatch):
    from rook.remote import bootstrap
    monkeypatch.delenv("ROOK_WEB_PASS", raising=False)
    monkeypatch.setattr("sys.argv", ["rook-dashboard", "--bind", "0.0.0.0"])
    with pytest.raises(SystemExit):
        bootstrap._cli_main()


def test_operator_update_key_from_environment(monkeypatch):
    sk = SigningKey.generate()
    manifest = {"schema": 1, "build": 1, "sha256": "00"}
    manifest["sig"] = base64.b64encode(sk.sign(canonical_payload(manifest)).signature).decode()
    monkeypatch.delenv("ROOK_UPDATE_PUBKEY", raising=False)
    assert not verify_manifest(manifest)          # not signed by the baked-in key
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", base64.b64encode(bytes(sk.verify_key)).decode())
    assert verify_manifest(manifest)
