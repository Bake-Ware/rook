"""Real UDP fanout between WebSocket handlers, including sender exclusion."""
import asyncio

import pytest
from starlette.applications import Starlette
from starlette.websockets import WebSocketDisconnect

from rook.band_mcp.ws_band import WSBandBridge


class BlindHub(asyncio.DatagramProtocol):
    def __init__(self):
        self.peers = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        self.peers[address] = data[:1]
        for peer, band in self.peers.items():
            if peer != address and band == data[:1]:
                self.transport.sendto(data, peer)


class WebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.closed = False

    async def accept(self):
        self.accepted.set()

    async def receive_bytes(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return value

    async def send_bytes(self, data):
        await self.outgoing.put(data)

    async def close(self, code=1000):
        self.closed = True


@pytest.mark.asyncio
async def test_websocket_peers_can_talk_through_sender_excluding_hub():
    loop = asyncio.get_running_loop()
    hub_transport, hub = await loop.create_datagram_endpoint(BlindHub, local_addr=('127.0.0.1', 0))
    bridge = WSBandBridge(Starlette(), '127.0.0.1', hub_transport.get_extra_info('sockname')[1])
    bridge.start()
    peers = [WebSocket() for _ in range(3)]
    tasks = [asyncio.create_task(bridge._ws_handler(ws)) for ws in peers]
    try:
        for ws in peers:
            await ws.accepted.wait()
        for ws, message in zip(peers, [b'A:first', b'A:second', b'B:other-band']):
            await ws.incoming.put(message)
        for _ in range(100):
            if len(hub.peers) == 3:
                break
            await asyncio.sleep(.01)
        assert len(hub.peers) == 3, 'Every WebSocket needs a distinct UDP peer'
        await asyncio.sleep(.02)
        for ws in peers:
            while not ws.outgoing.empty():
                ws.outgoing.get_nowait()
        await peers[0].incoming.put(b'A:request')
        assert await asyncio.wait_for(peers[1].outgoing.get(), 1) == b'A:request'
        await peers[1].incoming.put(b'A:reply')
        assert await asyncio.wait_for(peers[0].outgoing.get(), 1) == b'A:reply'
        assert peers[2].outgoing.empty(), 'Hub must not forward another band'
        await peers[0].incoming.put(None)
        await asyncio.wait_for(tasks[0], 1)
        assert len(bridge._ws_peers) == 2
    finally:
        await bridge.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        hub_transport.close()
    assert all(ws.closed for ws in peers)
    assert not bridge._ws_peers
