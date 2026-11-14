"""The hub's Telesthete carrier (docs/link-design.md sections 3.1, 3.3, 4, 5 and 9; build plan L4),
over loopback WebSocket. A Telesthete `Band(psk, reconnect=True)` over `WebSocketTransport` plays the
worker after a real `/link` exchange, and the protocol library encodes what it sends. A small proxy
between it and the hub's `/band` records the worker's frames and can cut the connection."""

import asyncio
import base64
import json
import os
import time

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from telesthete import Band
from telesthete.transport.websocket import WebSocketTransport

from rook_hub import Hub, link
from rook_protocol import MAX_MESSAGE, decode, encode
from test_hub import SHELL, heartbeat
from test_link_exchange import Box, post

ECHO = {"name": "demo.echo", "description": "Echo.", "schema": {"type": "object"}, "returns": "result"}
CAPS = (ECHO, SHELL)


async def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


class Proxy:
    """Between the worker's WebSocket client and the hub's /band: passes binary messages both ways,
    records what the worker sends while `recording`, and can cut every connection at once."""

    def __init__(self, upstream):
        self.upstream, self.recorded, self.recording, self._open = upstream, [], False, []

    async def start(self):
        app = web.Application()
        app.router.add_get("/band", self._route)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][:2]
        self.url = f"ws://{host}:{port}/band"
        return self

    async def _route(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async with aiohttp.ClientSession() as client, client.ws_connect(self.upstream) as up:
            self._open.append((ws, up))

            async def down():
                async for message in up:
                    if message.type == WSMsgType.BINARY:
                        await ws.send_bytes(message.data)
                await ws.close()
            to_worker = asyncio.create_task(down())
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    if self.recording:
                        self.recorded.append(message.data)
                    await up.send_bytes(message.data)
            to_worker.cancel()
            await asyncio.gather(to_worker, return_exceptions=True)
        return ws

    async def cut(self):
        """Close every connection, as a network drop would."""
        opened, self._open = self._open, []
        for ws, up in opened:
            await up.close()
            await ws.close()

    async def stop(self):
        await self.cut()
        await self._runner.cleanup()


class Played:
    """The worker's side of one session: a Telesthete Band under its psk over WebSocket, which
    opens Channel 0 (link design 3.1 steps 2 to 4)."""

    def __init__(self, psk, url):
        self.transport = WebSocketTransport(url)
        self.band = Band(psk, hostname="box", transport=self.transport, reconnect=True)
        self.peer = self.transport.peer_address

    async def start(self):
        await self.band.start()                     # connects, and sends HELLO on every connection
        await until(lambda: self.band.get_peers())  # HELLO_ACK: the hub holds the key
        self.channel = self.band.channel(0, self.peer)
        await self.channel.open()
        await until(lambda: self.channel.state == "ESTABLISHED")
        return self

    def send(self, message):
        self.channel.send_message(encode(message))

    async def recv(self, timeout=10):
        return decode(await self.channel.recv_message(timeout))

    async def stop(self):
        await self.band.stop()


class Setup:
    """A served hub, an approved worker identity (`box`) and the proxy in front of /band."""

    async def __aenter__(self):
        self.hub = Hub(self.data)
        host, port = await self.hub.serve("127.0.0.1", 0)
        self.base = f"http://{host}:{port}"
        self.alice = self.hub.auth.create_user("Alice", "a password")["guid"]
        self.client = aiohttp.ClientSession()
        self.proxy = await Proxy(f"ws://{host}:{port}/band").start()
        self.played = []
        return self

    def __init__(self, data, box=None):
        self.data, self.box = data, box or Box()

    async def approve(self):
        status, reply = await post(self.client, f"{self.base}/knock", self.box.knock())
        assert status == 200 and reply["status"] == "pending"
        self.worker = self.hub.approve(self.box.key, user=self.alice)
        self.box.hub_key = self.hub.public_key
        return self.worker

    async def session(self):
        """One /link exchange; (psk, link_accept)."""
        secret, opening = self.box.link_open()
        status, reply = await post(self.client, f"{self.base}/link", opening)
        assert status == 200 and reply["type"] == "link_accept", reply
        return self.box.psk(secret, opening, reply), reply

    async def connect(self, psk, url=None):
        played = await Played(psk, url or self.proxy.url).start()
        self.played.append(played)
        return played

    async def online(self, played):
        """The first heartbeat (H1): the worker is online on this session's link."""
        played.send(heartbeat(self.box.key, caps=CAPS))
        assert (await played.recv())["type"] == "heartbeat_reply"
        await until(lambda: self.hub.get_worker(self.worker["moniker"])["online"])

    async def __aexit__(self, *exc):
        for played in self.played:
            await played.stop()
        await self.proxy.stop()
        await self.client.close()
        await self.hub.stop_serving()
        self.hub.close()


async def round_trip(setup, played, text):
    """A call from the hub, answered by the played worker."""
    calling = asyncio.create_task(setup.hub.call(setup.worker["moniker"], "demo.echo", {"text": text}, "echo",
                                                 user=setup.alice))
    call = await played.recv()
    assert call["type"] == "call" and call["args"] == {"text": text}
    played.send({"type": "result", "id": call["id"], "result": {"echo": text}})
    assert (await calling) == {"type": "result", "id": call["id"], "result": {"echo": text}}


def udp_sockets():
    """The inodes of this process's UDP sockets (Linux)."""
    mine = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            mine.add(target[8:-1])
    udp = set()
    for table in ("/proc/net/udp", "/proc/net/udp6"):
        with open(table) as f:
            udp |= {line.split()[9] for line in list(f)[1:]}
    return mine & udp


def test_the_link_over_websocket(tmp_path):
    asyncio.run(over_websocket(tmp_path))


async def over_websocket(tmp_path):
    before = udp_sockets()
    async with Setup(tmp_path / "hub") as setup:
        hub = setup.hub
        assert udp_sockets() == before and hub.carrier.endpoint.band_ids() == []    # no UDP socket (D3)
        worker = await setup.approve()
        psk, accept = await setup.session()
        assert "udp_port" not in accept                                         # D3
        played = await setup.connect(psk)

        # Channel 0 opened: the session is the worker's live one, served as a link (3.1 step 4).
        await until(lambda: hub.sessions.live(setup.box.key) == psk)
        assert hub.sessions.pending(setup.box.key) == []
        # A heartbeat makes the worker online (H1, 3.1 step 5).
        await setup.online(played)
        live = hub._links[worker["guid"]]
        assert isinstance(live, link.TelestheteLink) and live.key == setup.box.key

        # A call and its result round trip.
        await round_trip(setup, played, "hello")

        # Stream data both ways.
        opening = asyncio.create_task(hub.call(worker["moniker"], "shell.open", {}, "a terminal", user=setup.alice))
        call = await played.recv()
        played.send({"type": "stream_open", "id": call["id"], "stream": "t1", "title": "terminal", "description": ""})
        stream = (await opening)["stream"]
        played.send({"type": "stream_data", "stream": "t1", "data": base64.b64encode(b"$ ").decode()})
        await until(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 1)
        await hub.streams.write(stream, "ls\n", user=setup.alice)
        typed = await played.recv()
        assert typed["type"] == "stream_data" and base64.b64decode(typed["data"]) == b"ls\n"
        read = await hub.streams.read(stream)
        assert [e.get("out") or e.get("in") for e in read["entries"]] == ["$ ", "ls\n"]

        # The worker's WebSocket connection is cut: its client reconnects under the same key, the hub
        # follows the new path, and the session and Channel 0 carry on (3.3; Telesthete 5.4).
        await setup.proxy.cut()
        await round_trip(setup, played, "after the cut")
        assert hub._links[worker["guid"]] is live and not live.closed and hub.sessions.live(setup.box.key) == psk

        # A second session used replaces the first (D10): its key leaves the Endpoint, its link ends,
        # and its frames are dropped before decryption.
        second_psk, _ = await setup.session()
        second = await setup.connect(second_psk)
        await until(lambda: live.closed)
        assert hub.sessions.live(setup.box.key) == second_psk
        assert hub.carrier.endpoint.band_ids() == [second.band.band_id]
        await setup.online(second)
        assert hub._links[worker["guid"]] is not live
        dropped, beats = hub.carrier.endpoint.dropped_unknown, hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
        played.send(heartbeat(setup.box.key, caps=CAPS))           # under the old session
        await until(lambda: hub.carrier.endpoint.dropped_unknown > dropped)
        await asyncio.sleep(0.3)
        assert hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] == beats
        await round_trip(setup, second, "on the second session")

        # Revoke (attach revokes the old key, W7; 3.3): the key leaves the Endpoint at once, the link
        # closes, and the session's frames are dropped before decryption.
        now_live = hub._links[worker["guid"]]
        rekeyed = Box()
        status, _ = await post(setup.client, f"{setup.base}/knock", rekeyed.knock())
        assert status == 200
        hub.attach(rekeyed.key, worker["moniker"], user=setup.alice)
        assert now_live.closed and hub.carrier.endpoint.band_ids() == []
        assert hub.sessions.live(setup.box.key) is None
        dropped = hub.carrier.endpoint.dropped_unknown
        second.send(heartbeat(setup.box.key, caps=CAPS))
        await until(lambda: hub.carrier.endpoint.dropped_unknown > dropped)
        await until(lambda: worker["guid"] not in hub._links)
        assert udp_sockets() == before


def test_a_channel_reset_ends_the_link(tmp_path):
    asyncio.run(reset_ends_the_link(tmp_path))


async def reset_ends_the_link(tmp_path):
    """Channel 0 resets (here: the worker's Band says GOODBYE, Telesthete 6.7 c): the link ends, its key
    leaves the Endpoint, and a call in flight fails with worker_disconnected (3.3)."""
    async with Setup(tmp_path / "hub") as setup:
        hub = setup.hub
        worker = await setup.approve()
        psk, _ = await setup.session()
        played = await setup.connect(psk)
        await setup.online(played)
        live = hub._links[worker["guid"]]
        calling = asyncio.create_task(hub.call(worker["moniker"], "demo.echo", {}, "never answered", user=setup.alice))
        assert (await played.recv())["type"] == "call"
        await played.stop()                                    # GOODBYE
        setup.played.remove(played)
        reply = await asyncio.wait_for(calling, 5)
        assert reply["type"] == "error" and reply["code"] == "worker_disconnected"
        assert live.closed and hub.sessions.live(setup.box.key) is None and hub.carrier.endpoint.band_ids() == []
        await until(lambda: worker["guid"] not in hub._links)
        with pytest.raises(EOFError):
            await live.recv()


def test_largest_message(tmp_path):
    asyncio.run(largest_message(tmp_path))


async def largest_message(tmp_path):
    """16,000,000 bytes encoded pass over the link; one byte more is refused before sending (4.3)."""
    async with Setup(tmp_path / "hub") as setup:
        worker = await setup.approve()
        psk, _ = await setup.session()
        played = await setup.connect(psk)
        await setup.online(played)
        base = len(encode({"type": "call", "id": "0" * 32, "cap": "demo.echo", "args": {"pad": ""}}))
        for size, passes in ((MAX_MESSAGE, True), (MAX_MESSAGE + 1, False)):
            args = {"pad": "x" * (size - base)}
            calling = asyncio.create_task(setup.hub.call(worker["moniker"], "demo.echo", args, "a big call",
                                                         user=setup.alice))
            if passes:
                raw = await played.channel.recv_message(120)
                assert len(raw) == MAX_MESSAGE
                call = decode(raw)
                played.send({"type": "result", "id": call["id"], "result": {"size": len(raw)}})
                assert (await calling)["result"] == {"size": MAX_MESSAGE}
            else:
                reply = await calling
                assert reply["type"] == "error" and reply["code"] == "message_too_large", reply
        # And the TelestheteLink itself refuses one byte more.
        with pytest.raises(Exception) as refused:
            await setup.hub._links[worker["guid"]].send(b"x" * (MAX_MESSAGE + 1))
        assert refused.value.message["code"] == "message_too_large"


def test_band_route(tmp_path, monkeypatch):
    monkeypatch.setattr(link, "SILENT", 0.5)
    asyncio.run(band_route(tmp_path))


async def band_route(tmp_path):
    async with Setup(tmp_path / "hub") as setup:
        hub, url = setup.hub, setup.base.replace("http", "ws") + "/band"
        async with aiohttp.ClientSession() as client:
            # Text and binaries under 43 bytes are ignored; a ping is answered with a pong.
            async with client.ws_connect(url, autoping=False) as ws:
                dropped = hub.carrier.endpoint.dropped_unknown
                await ws.send_str("hello")
                await ws.send_bytes(b"\x00" * 42)
                await ws.ping(b"are you there")
                pong = await ws.receive(timeout=2)
                assert pong.type == WSMsgType.PONG and pong.data == b"are you there"
                assert not ws.closed and hub.carrier.endpoint.dropped_unknown == dropped + 1   # the short binary

                # A connection silent for SILENT seconds is closed (shortened here from 60 s).
                started = time.monotonic()
                closing = await ws.receive(timeout=5)
                assert closing.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                assert 0.4 < time.monotonic() - started < 3

            # A full outgoing queue drops, and other connections go on (the seam's send never blocks).
            await setup.approve()
            async with client.ws_connect(url) as stalled:
                await until(lambda: len(hub.carrier._queues) == 1)
                [path] = hub.carrier._queues
                psk, _ = await setup.session()
                played = await setup.connect(psk)
                await setup.online(played)
                for _ in range(link.QUEUE + 50):
                    hub.carrier._send(path, b"\x01" * 100)        # what the Endpoint calls: never blocks
                assert hub.carrier._queues[path].qsize() <= link.QUEUE
                await round_trip(setup, played, "beside a full queue")
                assert not stalled.closed


def test_pending_session_unused_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(link, "PENDING_WAIT", 0.5)
    asyncio.run(pending_removed(tmp_path))


async def pending_removed(tmp_path):
    """A pending session not used within PENDING_WAIT of its link_open leaves the Endpoint; one used
    in time stays (the reviewer's decision, told to Bake 13 Nov; 60 s, shortened here)."""
    async with Setup(tmp_path / "hub") as setup:
        hub = setup.hub
        await setup.approve()
        unused, _ = await setup.session()
        used, _ = await setup.session()
        assert len(hub.carrier.endpoint.band_ids()) == 2 and len(hub.sessions.pending(setup.box.key)) == 2
        played = await setup.connect(used)
        await setup.online(played)
        await asyncio.sleep(0.8)
        assert hub.sessions.pending(setup.box.key) == [] and hub.sessions.live(setup.box.key) == used
        assert hub.carrier.endpoint.band_ids() == [played.band.band_id]
        await round_trip(setup, played, "still here")

        # Unused on its own, with no session used: removed too.
        await setup.session()
        assert len(hub.sessions.pending(setup.box.key)) == 1
        await asyncio.sleep(0.8)
        assert hub.sessions.pending(setup.box.key) == [] and hub.carrier.endpoint.band_ids() == [played.band.band_id]


FIXED = "fixed-session-key-reused-across-a-restart-test-only"


def test_replay_after_hub_restart(tmp_path):
    """Replay test 2 (section 9): a recording of the worker's frames sent to a restarted hub reaches
    nothing; the Endpoint counts every frame as unknown."""
    accepted = asyncio.run(replay(tmp_path, fixed=False))
    assert accepted == 0


def test_replay_control_with_a_fixed_key(tmp_path):
    """Test 3, the control (section 9): with a fixed session key reused across the restart (test-only),
    the same replay is accepted, so the test above would notice if that ever changed."""
    accepted = asyncio.run(replay(tmp_path, fixed=True))
    assert accepted > 0


async def replay(tmp_path, fixed):
    """Record a session's worker frames (heartbeats, a result, stream_data), restart the hub on the
    same data directory and send the recording to its /band. Returns how many heartbeats it took."""
    box = Box()
    async with Setup(tmp_path / "hub", box) as setup:
        hub = setup.hub
        worker = await setup.approve()
        if fixed:
            hub.sessions.add(box.key, "fixed", FIXED)       # fresh keys disabled: test-only
            psk = FIXED
        else:
            psk, _ = await setup.session()
        setup.proxy.recording = True
        played = await setup.connect(psk)
        await setup.online(played)
        await round_trip(setup, played, "recorded")
        opening = asyncio.create_task(hub.call(worker["moniker"], "shell.open", {}, "a terminal", user=setup.alice))
        call = await played.recv()
        played.send({"type": "stream_open", "id": call["id"], "stream": "t1", "title": "terminal", "description": ""})
        await opening
        played.send({"type": "stream_data", "stream": "t1", "data": base64.b64encode(b"recorded output").decode()})
        played.send(heartbeat(box.key, caps=CAPS))
        await until(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 1)
        await until(lambda: hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] == 2)
        setup.proxy.recording = False
        recording = list(setup.proxy.recorded)
    assert len(recording) > 5

    # The hub restarts on the same data directory; the recording is sent to its /band.
    hub = Hub(tmp_path / "hub")
    served = []
    real = hub.serve_link

    async def serve_link(link_):
        served.append(link_)
        await real(link_)
    hub.serve_link = serve_link
    host, port = await hub.serve("127.0.0.1", 0)
    if fixed:
        hub.sessions.add(box.key, "fixed", FIXED)
    beats = hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
    data = hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0]
    try:
        async with aiohttp.ClientSession() as client, client.ws_connect(f"ws://{host}:{port}/band") as ws:
            for frame in recording:
                await ws.send_bytes(frame)
            await asyncio.sleep(1.5)
        taken = hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] - beats
        if not fixed:
            assert served == [] and taken == 0                      # nothing reached serve_link
            assert hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == data
            assert hub.carrier.endpoint.dropped_unknown == len(recording)
        else:
            assert served and hub.carrier.endpoint.dropped_unknown < len(recording)
        return taken
    finally:
        await hub.stop_serving()
        hub.close()
