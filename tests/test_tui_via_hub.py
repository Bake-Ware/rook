"""The `rook band` TUI reaches workers only through the hub (permissions 3.6).

End to end in one process: the TUI's own HTTP client (``BandHTTP``) talks to
the real dashboard app, whose band client evaluates policy for the login and
signs a ticket; a fake transport carries the frame to a real ``Worker``.
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from nacl.signing import SigningKey

from rook.cli import band_tui
from rook.core import authz
from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer

PASSWORD = "test-password-1"
SETUP = {"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
         "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"}


class _WorkerTransport:
    """Worker side of an in-memory band: replies go straight to the client."""

    def __init__(self, band_id: bytes, deliver):
        self.band_id = band_id
        self._deliver = deliver

    async def send(self, data):
        self._deliver(json.loads(data))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A dashboard whose band client signs tickets, wired to one worker."""
    from rook.band_mcp.client import BandClient
    from rook.hub.authz import Authorizer
    from rook.hub.keys import HubSigner
    from rook.hub.policy import PolicyStore
    from rook.worker import admin, audit, core

    root = SigningKey.generate()
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", authz.pub_b64(root))
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    setup_store.save(SETUP)

    client = BandClient("test-band")
    client.authz = Authorizer(PolicyStore(str(tmp_path / "policy.json")),
                              HubSigner(root, op_dir=tmp_path / "keys"))
    worker = core.Worker(_WorkerTransport(bytes.fromhex(client.band_hex), client._handle_reply),
                         enabled=["info", "shell"], name="worker-a")

    async def to_worker(data):
        asyncio.get_running_loop().create_task(worker._on_message(data, ()))
    client.transport.send = to_worker
    client.workers[worker.worker_id] = {"worker_id": worker.worker_id, "name": "worker-a",
                                        "caps": worker.registry.list(), "last_seen": time.time()}

    server = CombinedServer(web_user="owner", web_pass=PASSWORD)
    server._band = client
    yield server, worker, audit
    if server._chat:
        server._chat.close()


async def _tui(server):
    """The TUI's real client against the running dashboard."""
    ts = TestServer(server._app)
    await ts.start_server()
    return ts, band_tui.BandHTTP(str(ts.make_url("")), "owner", PASSWORD)


def _direct(cap, worker, args):
    """What a PSK-only peer could send: a frame with no ticket."""
    return json.dumps({"id": "direct-1", "cap": cap, "target": worker.worker_id,
                       "args": args}).encode()


@pytest.mark.asyncio
async def test_audit_mode_tui_exec_goes_through_hub_with_ticket(hub):
    server, worker, audit = hub
    worker.guard.mode = "audit"
    ts, band = await _tui(server)
    try:
        who = await asyncio.to_thread(band.whoami)
        assert who["principal"] == "human:dashboard" and who["tickets"] is True
        assert band.notice() is None
        reply = await asyncio.to_thread(band.call, "shell.exec", worker.worker_id,
                                        {"cmd": "echo via-hub"}, 10)
    finally:
        await ts.close()
    assert reply["ok"], reply
    assert "via-hub" in json.dumps(reply["result"])
    row = [r for r in audit.tail(10) if r["cap"] == "shell.exec"][-1]
    assert row["decision"] == "allow"
    assert row["ticket"]["verified"] and row["ticket"]["p"] == "human:dashboard"
    # Audit mode logs, never refuses, a direct unticketed call.
    await worker._on_message(_direct("shell.exec", worker, {"cmd": "echo direct"}), ())
    row = [r for r in audit.tail(10) if r["cap"] == "shell.exec"][-1]
    assert row["ok"] and not row["ticket"]["verified"]


@pytest.mark.asyncio
async def test_enforce_exec_accepts_tui_via_hub_refuses_direct(hub):
    server, worker, audit = hub
    worker.guard.mode = "enforce-exec"
    replies = []
    sent = worker.transport.send
    worker.transport.send = lambda data: (replies.append(json.loads(data)), sent(data))[1]
    ts, band = await _tui(server)
    try:
        await asyncio.to_thread(band.whoami)
        reply = await asyncio.to_thread(band.call, "shell.exec", worker.worker_id,
                                        {"cmd": "echo enforced"}, 10)
        plugins = await asyncio.to_thread(band.call, "worker.plugin.list", worker.worker_id,
                                          None, 10)
    finally:
        await ts.close()
    assert reply["ok"] and "enforced" in json.dumps(reply["result"]), reply
    assert plugins["ok"], plugins
    # The same call straight onto the band, without the hub, is refused.
    await worker._on_message(_direct("shell.exec", worker, {"cmd": "echo direct"}), ())
    refused = replies[-1]
    assert refused["id"] == "direct-1" and not refused["ok"]
    assert "requires a hub ticket" in refused["error"]
    row = audit.tail(1)[-1]
    assert row["decision"] == "deny" and not row["ticket"]["verified"]


@pytest.mark.asyncio
async def test_hub_policy_denial_comes_back_with_a_hint(hub, tmp_path):
    from rook.hub.policy import DEFAULT_POLICY
    server, worker, _ = hub
    doc = json.loads(json.dumps(DEFAULT_POLICY))
    doc["mode"] = "enforce"
    doc["rules"].append({"id": "no-shell-from-dashboard", "who": "human:dashboard",
                         "deny": ["shell.exec"]})
    (tmp_path / "policy.json").write_text(json.dumps(doc))
    from rook.hub.policy import PolicyStore
    server._band.authz.store = PolicyStore(str(tmp_path / "policy.json"))
    ts, band = await _tui(server)
    try:
        await asyncio.to_thread(band.whoami)
        reply = await asyncio.to_thread(band.call, "shell.exec", worker.worker_id,
                                        {"cmd": "echo x"}, 5)
    finally:
        await ts.close()
    assert "denied" in reply, reply
    assert reply["denied"]["principal"] == "human:dashboard" and "hint" in reply


def test_old_hub_without_whoami_gets_a_clear_message(monkeypatch):
    import urllib.error
    band = band_tui.BandHTTP("http://hub.invalid", "owner", PASSWORD)

    def old_hub(path, *a, **k):
        if path == "/api/band/whoami":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        return {"ok": False, "error": "denied by worker: exec requires a hub ticket "
                                      "(mode enforce-exec; no ticket)"}
    monkeypatch.setattr(band, "_req", old_hub)
    assert band.whoami() == {"legacy": True}
    assert "predates permissions" in band.notice()
    reply = band.call("shell.exec", "w1", {"cmd": "id"})
    assert "update the hub" in reply["hint"]
    # The header shows the warning instead of "connected".
    assert "predates permissions" in band_tui.UI(band, "test").status


def test_hub_without_signing_key_is_explained(monkeypatch):
    band = band_tui.BandHTTP("http://hub.invalid", "owner", PASSWORD)
    band.hub = {"principal": "human:dashboard", "tickets": False}
    assert "no call tickets" in band.notice()
    reply = band.explain({"ok": False, "error": "denied by worker: admin requires a hub "
                                                "ticket (mode enforce-admin; no ticket)"})
    assert "no signing key" in reply["hint"]
    ok = {"ok": True, "result": 1}
    assert band.explain(dict(ok)) == ok


def test_tui_has_no_direct_band_path():
    """The TUI must stay an HTTP client of the hub: no band client, PSK or
    telesthete transport that could reach workers without a ticket."""
    src = Path(band_tui.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." if node.level else (node.module or "").split(".")[0])
    assert not imported & {".", "rook", "telesthete", "websockets", "aiohttp", "nacl",
                           "asyncio", "ssl"}, imported
    assert "psk" not in src.lower()
    assert "create_connection" not in src and ".connect(" not in src
    paths = {p for p in ("/api/band/call", "/api/band/ban", "/api/band/unban",
                         "/api/band/workers", "/api/band/overview", "/api/band/whoami")
             if p in src}
    assert len(paths) == 6


@pytest.mark.asyncio
async def test_whoami_needs_the_login_and_reports_no_tickets_without_a_signer(hub):
    server, _, _ = hub
    server._band.authz = None
    ts, band = await _tui(server)
    try:
        anonymous = band_tui.BandHTTP(band.url, "owner", "wrong-password")
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as err:
            await asyncio.to_thread(anonymous.whoami)
        assert err.value.code == 401
        who = await asyncio.to_thread(band.whoami)
    finally:
        await ts.close()
    assert who["principal"] == "human:dashboard" and who["tickets"] is False
    assert "no call tickets" in band.notice()
