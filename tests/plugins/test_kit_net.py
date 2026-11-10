"""rook.kit.net, what the device plugins on the hub share, against real
local servers reached through the stand-in for the Rust worker's net.open,
net.udp and stream caps (port_helpers.FakeNet)."""

import asyncio
import base64
import hashlib
import socket
import struct
import threading

import pytest

from port_helpers import FakeNet
from rook.kit.ctx import KitError
from rook.kit.net import Stream, WebSocket, http, values


class Ctx:
    """Just what rook.kit.net uses of a hub script's ctx."""

    def __init__(self):
        self.net = FakeNet()

    async def call_on(self, worker, cap, **args):
        assert worker == "w1"
        try:
            return await self.net.call(cap, args)
        except RuntimeError as e:
            raise KitError(str(e)) from None


def serve(handler):
    """A one-thread TCP server; handler(conn) per connection."""
    srv = socket.create_server(("127.0.0.1", 0))

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda: (handler(conn), conn.close()), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_values_lets_settings_win():
    assert values({"name": "tv", "host": "h", "port": 1, "settings": {"port": 2, "key": None}}) == \
        {"name": "tv", "host": "h", "port": 2}
    assert values(None) == {}


def test_http_content_length_chunked_and_to_close():
    def handler(conn):
        req = conn.recv(65536).decode()
        path = req.split()[1]
        if path == "/chunked":
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        elif path == "/close":
            conn.sendall(b"HTTP/1.0 404 Not Found\r\n\r\ngone")
        else:
            body = req.split("\r\n\r\n", 1)[1].encode()
            conn.sendall(b"HTTP/1.1 201 Created\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s"
                         % (len(body), body))
    srv, port = serve(handler)
    try:
        ctx = Ctx()
        status, headers, body = run(http(ctx, "w1", "POST", "127.0.0.1", port, "/echo", body="hi there"))
        assert (status, headers["content-type"], body) == (201, "text/plain", b"hi there")
        assert run(http(ctx, "w1", "GET", "127.0.0.1", port, "/chunked"))[2] == b"abcde"
        assert run(http(ctx, "w1", "GET", "127.0.0.1", port, "/close"))[::2] == (404, b"gone")
    finally:
        srv.close()


def test_http_cuts_off_a_stream_that_never_ends():
    # Review K2 (kvm.api_get with a follow query read forever): a time and a size limit.
    def handler(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n" + b"log line\n" * 200)
        try:
            for _ in range(400):
                conn.sendall(b"log line\n" * 100)
                threading.Event().wait(0.01)
        except OSError:
            pass
    srv, port = serve(handler)
    try:
        with pytest.raises(KitError, match="didn't finish in 1s"):
            run(http(Ctx(), "w1", "GET", "127.0.0.1", port, "/api/log", timeout=1))
        with pytest.raises(KitError, match="larger than 0 MiB"):
            run(http(Ctx(), "w1", "GET", "127.0.0.1", port, "/api/log", max_bytes=1000))
    finally:
        srv.close()


def test_websocket_handshake_frames_and_ping():
    def handler(conn):
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        key = [h.split(":", 1)[1].strip() for h in head.decode().split("\r\n") if h.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        b0, b1 = conn.recv(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", conn.recv(2))[0]
        mask = conn.recv(4)
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        text = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        assert b1 & 0x80                                # client frames are masked
        conn.sendall(b"\x89\x00")                       # a ping first: answered, not returned
        reply = b"echo:" + text
        conn.sendall(bytes([0x81, 126]) + struct.pack(">H", len(reply)) + reply)
        conn.recv(64)                                   # the pong and the close
    srv, port = serve(handler)
    try:
        async def go():
            async with await WebSocket.connect(Ctx(), "w1", "127.0.0.1", port, "/") as ws:
                await ws.send("x" * 200)
                return await ws.recv()
        assert run(go()) == "echo:" + "x" * 200
    finally:
        srv.close()


def test_udp_datagrams_carry_their_sender():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    port = peer.getsockname()[1]

    def echo():
        data, addr = peer.recvfrom(100)
        peer.sendto(b"pong:" + data, addr)
    threading.Thread(target=echo, daemon=True).start()
    try:
        async def go():
            async with await Stream.udp(Ctx(), "w1") as s:
                await s.send(b"ping", to=f"127.0.0.1:{port}")
                return await s.listen(1.0)
        [(data, by)] = run(go())
        assert data == b"pong:ping" and by == f"127.0.0.1:{port}"
    finally:
        peer.close()


def test_a_closed_connection_and_a_silent_one_are_errors():
    srv, port = serve(lambda conn: None)              # accepts, then hangs up
    try:
        async def go(wait):
            async with await Stream.tcp(Ctx(), "w1", "127.0.0.1", port) as s:
                return await s.read_until(b"\n", timeout=wait)
        with pytest.raises(KitError, match="closed the connection"):
            run(go(5))
    finally:
        srv.close()
