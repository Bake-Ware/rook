"""A worker's own requests to the hub name themselves (User-Agent): a hub behind Cloudflare answers
Python's default one (Python-urllib/3.x) with 403, error 1010. Found on 22 Nov 2026 joining a worker
to a hub behind a Cloudflare tunnel: the knock never reached the hub. The engine's hook before
compaction calls the hub's door the same way, so it names itself too.

The same join then showed the link itself never opened to a hub reached by https: the websockets
library refuses ssl=None, Telesthete's default, with a wss:// address. The last test here."""
import http.server
import importlib.util
import json
import pathlib
import subprocess
import sys
import threading
import types

import pytest

import rook_link
import rook_worker

ENGINE = pathlib.Path(__file__).parent.parent / "hub/rook_hub/defaults/agents-claude-code/worker.py"


class Seen(Exception):
    """Raised by the stand-in for urlopen once it has the request."""


def test_knock_and_link_requests_name_the_worker(monkeypatch):
    seen = []

    def urlopen(request, timeout=None):
        seen.append(request)
        raise Seen

    monkeypatch.setattr(rook_worker.urllib.request, "urlopen", urlopen)
    worker = types.SimpleNamespace(hub_url="http://hub.example")
    with pytest.raises(Seen):
        rook_worker.Worker._post(worker, b"{}")
    with pytest.raises(Seen):
        rook_worker.Worker._get(worker, "/link")
    assert [r.full_url for r in seen] == ["http://hub.example/knock", "http://hub.example/link"]
    for request in seen:
        assert request.get_header("User-agent") == f"rook-worker/{rook_worker.VERSION}"


def test_the_hook_before_compaction_names_itself(tmp_path):
    spec = importlib.util.spec_from_file_location("engine_worker_for_user_agent", ENGINE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    agents = []

    class Door(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            agents.append(self.headers.get("User-Agent"))
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                "isError": False, "content": [{"type": "text", "text": "{}"}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Door)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        door = tmp_path / "door.json"
        door.write_text(json.dumps({"mcpServers": {"rook": {
            "type": "http", "url": f"http://127.0.0.1:{server.server_port}/mcp", "headers": {"Authorization": "Bearer x"}}}}))
        (tmp_path / "precompact.json").write_text(json.dumps(["agent.dream", {}]))
        (tmp_path / "precompact.py").write_text(engine.HOOK)
        done = subprocess.run([sys.executable, str(tmp_path / "precompact.py"), str(door), str(tmp_path / "precompact.json")],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    finally:
        server.shutdown()
    assert agents == ["rook-agents-claude-code"]


def test_a_link_to_a_hub_reached_by_https_has_tls_and_one_by_http_has_none():
    for band_url, tls in (("wss://hub.example/band", True), ("ws://127.0.0.1:9/band", None)):
        link = rook_link.TelestheteLink(rook_worker.Session("00" * 32, "a psk for the test only", band_url, None, None))
        assert link.transport.url == band_url
        assert link.transport.ssl is tls
