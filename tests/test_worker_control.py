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


def test_control_request_without_a_worker(home):
    with pytest.raises(control.NotRunning):
        control.request("status")
    (home / "control.json").write_text(json.dumps({"port": 1, "token": "t"}))
    with pytest.raises(control.NotRunning):
        control.request("status")


