"""A minimal Telesthete relay: it forwards each datagram to the other peers
of its band (the first 16 bytes), like telesthete-hub does, without routing.
It prints its port on stdout and runs until killed.

    python -m rook.core2.relay [port [host]]
"""
import asyncio
import sys


class Relay(asyncio.DatagramProtocol):
    def __init__(self):
        self.peers: dict[bytes, set] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 16:
            return
        peers = self.peers.setdefault(data[:16], set())
        peers.add(addr)
        for peer in peers:
            if peer != addr:
                self.transport.sendto(data, peer)


async def main(port: int, host: str = "127.0.0.1") -> None:
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        Relay, local_addr=(host, port))
    print(transport.get_extra_info("socket").getsockname()[1], flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0, *sys.argv[2:3]))
    except KeyboardInterrupt:
        pass
