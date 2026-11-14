"""The hub stops promptly whatever is connected to /band (F1; docs/link-design.md section 5.1): an idle
stranger, a linked worker, many of each, a client that never reads. A linked worker hears the hub
leave (its Channel resets on the hub's GOODBYE) without waiting for PEER_TIMEOUT. The real start
command exits on SIGTERM within 3 seconds with a /band connection held open, and starts again."""

import asyncio
import base64
import os
import signal
import socket
import time

import aiohttp

from rook_hub import Hub, link
from test_first_run import Started
from test_hub import heartbeat
from test_link_carrier import CAPS, Played, Setup
from test_link_exchange import post

BOUND = 1.5     # seconds stop_serving may take: the carrier's FLUSH (0.25) plus the Endpoint's GOODBYE pause


async def stranger(url):
    """An idle client on /band: connected, sending nothing."""
    session = aiohttp.ClientSession()
    return session, await session.ws_connect(url)


async def deaf(host, port, path="/band"):
    """A client that completed the WebSocket upgrade and never reads anything: a small receive buffer,
    so whatever the hub sends it soon stays in the hub's own buffers."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, (host, port))
    reader, writer = await asyncio.open_connection(sock=sock)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    await writer.drain()
    return writer


async def timed_stop(hub):
    started = time.monotonic()
    await hub.stop_serving()
    return time.monotonic() - started


async def linked(setup):
    """A worker linked over /band directly (no proxy), whose Channel resets are recorded."""
    await setup.approve()
    psk, _ = await setup.session()
    played = await setup.connect(psk, setup.base.replace("http", "ws") + "/band")
    await setup.online(played)
    played.resets = []
    played.channel.on_reset(lambda reason: played.resets.append((reason.value, time.monotonic())))
    return played


def test_stop_with_an_idle_stranger(tmp_path):
    async def go():
        async with Setup(tmp_path / "hub") as setup:
            session, ws = await stranger(setup.base.replace("http", "ws") + "/band")
            assert await timed_stop(setup.hub) < BOUND
            assert (await ws.receive(timeout=2)).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            await session.close()
    asyncio.run(go())


def test_stop_with_a_linked_worker_and_strangers(tmp_path):
    async def go():
        async with Setup(tmp_path / "hub") as setup:
            played = await linked(setup)
            url = setup.base.replace("http", "ws") + "/band"
            strangers = [await stranger(url) for _ in range(20)]
            assert len(setup.hub.carrier._requests) == 21
            started = time.monotonic()
            assert await timed_stop(setup.hub) < BOUND
            # The worker heard the hub leave: its GOODBYE reset Channel 0 at once, not after the 15 s
            # PEER_TIMEOUT.
            deadline = time.monotonic() + 2
            while not played.resets and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert [reason for reason, _ in played.resets] == ["peer_left"]
            assert played.resets[0][1] - started < BOUND
            assert setup.hub.carrier._requests == {} and setup.hub.carrier.endpoint.band_ids() == []
            for session, _ in strangers:
                await session.close()
    asyncio.run(go())


def test_stop_with_a_client_that_never_reads(tmp_path):
    async def go():
        async with Setup(tmp_path / "hub") as setup:
            host, port = setup.base.removeprefix("http://").split(":")
            writer = await deaf(host, int(port))
            deadline = time.monotonic() + 5
            while not setup.hub.carrier._requests and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            [path] = setup.hub.carrier._requests
            # Hand it more than its buffers hold; it reads none of it.
            for _ in range(40):
                for _ in range(link.QUEUE):
                    setup.hub.carrier._send(path, b"\x01" * 65536)
                await asyncio.sleep(0.01)
            transport = setup.hub.carrier._requests[path].transport
            assert not setup.hub.carrier._sent(path) and transport.get_write_buffer_size() > 0
            assert await timed_stop(setup.hub) < BOUND
            writer.close()
    asyncio.run(go())


def test_the_start_command_exits_on_sigterm_and_starts_again(tmp_path):
    async def go():
        data, key = tmp_path / "data", tmp_path / "vault.key"
        for signum in (signal.SIGTERM, signal.SIGINT):          # SIGINT is Ctrl-C
            hub = await Started(data, key).__aenter__()          # stopped here, not by __aexit__
            session, ws = await stranger(hub.base.replace("http", "ws") + "/band")
            started = time.monotonic()
            hub.process.send_signal(signum)
            code = await asyncio.wait_for(hub.process.wait(), 3)
            assert code == 0 and time.monotonic() - started < 3, signum
            await session.close()
        # A hub stopped this way starts again on the same data directory and serves.
        async with Started(data, key) as hub, aiohttp.ClientSession() as client:
            async with client.get(hub.base + "/api/setup") as response:
                assert response.status == 200
    asyncio.run(go())


def test_a_hub_stopped_with_a_linked_worker_serves_again(tmp_path):
    async def go():
        async with Setup(tmp_path / "hub") as setup:
            played = await linked(setup)
            assert await timed_stop(setup.hub) < BOUND
            await played.stop()
            setup.played.remove(played)
        # The same data directory: the worker makes a new session and links again.
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as client:
                secret, opening = setup.box.link_open()
                status, accept = await post(client, f"http://{host}:{port}/link", opening)
                assert status == 200, accept
                played = await Played(setup.box.psk(secret, opening, accept), f"ws://{host}:{port}/band").start()
                played.send(heartbeat(setup.box.key, caps=CAPS))
                assert (await played.recv())["type"] == "heartbeat_reply"
                assert hub.get_worker(setup.worker["moniker"])["online"]
                assert await timed_stop(hub) < BOUND
                await played.stop()
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())
