"""A minimal Telesthete relay: it forwards each datagram to the other peers
of its band (the first 16 bytes), like telesthete-hub does, without routing.
Given a web socket port it also takes peers on ``ws://127.0.0.1:<ws_port>/band``
(one binary message per frame, the hub door's ``band:``), bridged with the UDP
ones, like telesthete-hub's ``HUB_WS_BIND``. It prints its port (then the web
socket's) on stdout and runs until killed.

    python -m rook.core2.relay [port [host [ws_port]]]
"""
import asyncio
import sys


class Relay(asyncio.DatagramProtocol):
    def __init__(self):
        self.peers: dict[bytes, set] = {}     # band -> UDP addresses and web sockets' queues

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.forward(data, addr)

    def forward(self, data, src):
        if len(data) < 16:
            return
        peers = self.peers.setdefault(data[:16], set())
        peers.add(src)
        for peer in peers:
            if peer == src:
                continue
            if isinstance(peer, tuple):
                self.transport.sendto(data, peer)
            else:
                peer.put_nowait(data)

    async def band(self, request):
        """One web socket peer: pinned to the band of its first frame."""
        from aiohttp import WSMsgType, web
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        out, band = asyncio.Queue(), None

        async def send():
            while True:
                await ws.send_bytes(await out.get())
        sender = asyncio.ensure_future(send())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY and len(msg.data) >= 16:
                    band = band or msg.data[:16]
                    if msg.data[:16] == band:
                        self.forward(msg.data, out)
        finally:
            sender.cancel()
            self.peers.get(band, set()).discard(out)
        return ws


async def main(port: int, host: str = "127.0.0.1", ws_port: int | None = None) -> None:
    transport, relay = await asyncio.get_running_loop().create_datagram_endpoint(
        Relay, local_addr=(host, port))
    print(transport.get_extra_info("socket").getsockname()[1], flush=True)
    if ws_port is not None:
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/band", relay.band)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", ws_port).start()
        print(runner.addresses[0][1], flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0,
                         *sys.argv[2:3], *[int(p) for p in sys.argv[3:4]]))
    except KeyboardInterrupt:
        pass
