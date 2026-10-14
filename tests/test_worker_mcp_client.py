"""rook.worker.mcp_client: the stdlib MCP client `rook ask` uses with an API
token, against a small stand-in for the hub's streamable-HTTP endpoint."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rook.worker import mcp_client


class Endpoint(BaseHTTPRequestHandler):
    seen: list = []
    sse = True

    def log_message(self, *a):
        pass

    def do_DELETE(self):
        self.seen.append(("DELETE", self.headers.get("Mcp-Session-Id")))
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append((body.get("method"), self.headers.get("Mcp-Session-Id")))
        if self.headers.get("Authorization") != "Bearer good":
            self.send_response(401)
            self.end_headers()
            return
        if body.get("method") == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if body["method"] == "initialize":
            result = {"protocolVersion": mcp_client.PROTOCOL, "capabilities": {}}
        else:
            args = body["params"]["arguments"]
            text = json.dumps({"ok": True, "from": args["worker"],
                               "result": {"answer": f"you asked {args['args']['question']}"}})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        msg = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s-1")
        if self.sse:
            payload = f"event: message\ndata: {msg}\n\n".encode()
            self.send_header("Content-Type", "text/event-stream")
        else:
            payload = msg.encode()
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Endpoint.seen = []
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


@pytest.mark.parametrize("sse", [True, False])
def test_rook_call_through_mcp(endpoint, sse):
    Endpoint.sse = sse
    reply = mcp_client.rook_call(endpoint, "good", "home.ask", {"question": "lunch?"})
    assert reply == {"ok": True, "from": "rook", "result": {"answer": "you asked lunch?"}}
    # One short session: initialize, initialized, the call, then closed.
    assert Endpoint.seen == [("initialize", None), ("notifications/initialized", "s-1"),
                             ("tools/call", "s-1"), ("DELETE", "s-1")]


def test_a_refused_token_says_what_to_do(endpoint):
    with pytest.raises(mcp_client.McpError, match="Tokens page"):
        mcp_client.rook_call(endpoint, "bad", "home.ask", {"question": "x"})


def test_event_stream_parsing():
    body = b'event: message\ndata: {"id": 1,\ndata:  "result": {}}\n\n: ping\n\ndata: [x\n\n'
    assert mcp_client._messages(body, "text/event-stream") == [{"id": 1, "result": {}}]


class Elsewhere(BaseHTTPRequestHandler):
    """Another host a redirect points at: records whatever reaches it."""
    seen: list = []

    def log_message(self, *a):
        pass

    def _any(self):
        self.seen.append((self.command, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _any


class Redirecting(BaseHTTPRequestHandler):
    target = ""
    code = 302

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(self.code)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_a_redirect_is_never_followed_with_the_token(code):
    """A 301/302 on a POST would become a GET that still carries the bearer
    token, to whatever host the Location names."""
    other, hub = _serve(Elsewhere), _serve(Redirecting)
    try:
        Elsewhere.seen = []
        Redirecting.code = code
        Redirecting.target = f"http://localhost:{other.server_address[1]}/mcp"
        with pytest.raises(mcp_client.McpError, match="redirected to http://localhost:") as e:
            mcp_client.rook_call(f"http://127.0.0.1:{hub.server_address[1]}/mcp", "good",
                                 "home.ask", {"question": "x"})
        assert "--mcp-url" in str(e.value)
        assert Elsewhere.seen == []
    finally:
        other.shutdown()
        hub.shutdown()


class Missing(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_no_mcp_at_the_dashboard_address_names_the_split_hub_port():
    server = _serve(Missing)
    try:
        with pytest.raises(mcp_client.McpError, match="8765") as e:
            mcp_client.rook_call(f"http://127.0.0.1:{server.server_address[1]}/mcp", "good",
                                 "home.status", {})
        assert "--mcp-url" in str(e.value) and "good" not in str(e.value)
    finally:
        server.shutdown()
