"""The running worker's local control link (rook.worker.control): what
`rook status`, `rook plugins` and `rook logs` talk to."""

import asyncio
import json
import os

import pytest

from rook.worker import control


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path))
    return tmp_path


# -- the control link ------------------------------------------------------------------

class FakeRegistry:
    def __init__(self):
        self.calls = []

    def list(self):
        return ["worker.plugin.list", "info.ping"]

    async def call(self, cap, **kw):
        self.calls.append((cap, kw))
        return {"ok": True, "plugins": [{"module": "info", "loaded": True}]}


class FakeWorker:
    name = "kitchen-pc"
    worker_id = "w" * 16
    band_rx_at = None
    band_rtt_at = None
    _band_peers = {}

    def __init__(self):
        self.registry = FakeRegistry()
        self.plugins = []
        self.requests = []

    def band_peers(self):
        return []

    async def request(self, cap, args=None, target=None, timeout=10.0):
        self.requests.append((cap, args, target, timeout))
        if cap == "home.ask":
            return {"ok": False, "error": "home.ask is not callable over the band on the hub "
                                          "(risk above 'read'); call it through the MCP bridge"}
        return {"ok": True, "result": {"rooms": []}}


def test_control_link_status_calls_and_token(home):
    async def go():
        worker = FakeWorker()
        ring = control.LogRing()
        server = control.ControlServer(worker, ring)
        await server.start()
        info = json.loads((home / "control.json").read_text())
        assert info["port"] == server.port and info["token"] == server.token
        if os.name == "posix":
            assert (home / "control.json").stat().st_mode & 0o777 == 0o600
        status = await asyncio.to_thread(control.request, "status")
        assert status["ok"] and status["status"]["name"] == "kitchen-pc"
        assert status["status"]["band"]["connected"] is False
        # The plugin switches run locally; anything else of our own is refused.
        listed = await asyncio.to_thread(control.request, "call", cap="worker.plugin.list",
                                         args={}, local=True)
        assert listed["ok"] and worker.registry.calls == [("worker.plugin.list", {})]
        refused = await asyncio.to_thread(control.request, "call", cap="shell.exec",
                                          args={"argv": ["id"]}, local=True)
        assert refused["ok"] is False and "not available" in refused["error"]
        # Everything else goes out on the band as this worker.
        band = await asyncio.to_thread(control.request, "call", cap="chat.read",
                                       args={"action": "rooms"}, timeout=3)
        assert band["ok"] and worker.requests[0][:2] == ("chat.read", {"action": "rooms"})
        import logging
        logging.getLogger("rook.test").addHandler(ring)
        logging.getLogger("rook.test").warning("hello from the ring")
        logs = await asyncio.to_thread(control.request, "logs")
        assert any("hello from the ring" in r["text"] for r in logs["lines"])
        # A wrong token gets nothing.
        good = server.token
        server.token = "something-else"
        bad = await asyncio.to_thread(control.request, "status")
        assert bad == {"ok": False, "error": "bad token"}
        server.token = good
        await server.stop()
        assert not (home / "control.json").exists()
    asyncio.run(go())


def _raw(port, payload: bytes) -> bytes:
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return buf


def test_a_non_ascii_token_is_a_bad_token_not_a_hang_up(home):
    async def go():
        server = control.ControlServer(FakeWorker())
        await server.start()
        try:
            reply = await asyncio.to_thread(
                _raw, server.port, (json.dumps({"token": "töken", "op": "status"},
                                               ensure_ascii=False) + "\n").encode("utf-8"))
            assert json.loads(reply) == {"ok": False, "error": "bad token"}
        finally:
            await server.stop()
    asyncio.run(go())


def test_idle_connections_cannot_lock_the_link(home, monkeypatch):
    """Connections that never send a token don't count against the client
    limit, and are dropped after AUTH_SECS."""
    import socket
    monkeypatch.setattr(control, "AUTH_SECS", 0.5)

    async def go():
        server = control.ControlServer(FakeWorker())
        await server.start()
        idle = [socket.create_connection(("127.0.0.1", server.port))
                for _ in range(control.MAX_CLIENTS * 2)]
        try:
            await asyncio.sleep(0.1)
            status = await asyncio.to_thread(control.request, "status")
            assert status["ok"] and status["status"]["name"] == "kitchen-pc"
            # The idle ones were given up on: each got "bad request" and EOF.
            await asyncio.sleep(0.8)
            for s in idle:
                s.settimeout(2)
                data = s.recv(4096)
                assert json.loads(data) == {"ok": False, "error": "bad request"}
                assert s.recv(4096) == b""
        finally:
            for s in idle:
                s.close()
            await server.stop()
    asyncio.run(go())


def test_busy_link_answers_busy(home, monkeypatch):
    monkeypatch.setattr(control, "MAX_CLIENTS", 0)

    async def go():
        server = control.ControlServer(FakeWorker())
        await server.start()
        try:
            reply = await asyncio.to_thread(control.request, "status")
            assert reply["ok"] is False and "busy" in reply["error"]
        finally:
            await server.stop()
    asyncio.run(go())


def test_windows_files_get_an_owner_only_acl(home, monkeypatch):
    import subprocess
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setenv("USERNAME", "jane")
    monkeypatch.setenv("USERDOMAIN", "PC1")
    monkeypatch.setattr(control, "_is_windows", lambda: False)
    control.restrict_to_owner(home / "control.json")
    assert ran == []
    monkeypatch.setattr(control, "_is_windows", lambda: True)

    async def go():
        server = control.ControlServer(FakeWorker())
        await server.start()
        await server.stop()
    asyncio.run(go())
    assert ran == [["icacls", str(home / "control.json"), "/inheritance:r", "/grant:r",
                    "PC1\\jane:F"]]


def test_control_request_without_a_worker(home):
    with pytest.raises(control.NotRunning):
        control.request("status")
    (home / "control.json").write_text(json.dumps({"port": 1, "token": "t"}))
    with pytest.raises(control.NotRunning):
        control.request("status")


