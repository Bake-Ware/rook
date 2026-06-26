"""``python -m rook.worker`` — boot a rook worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .core import Worker
from .transports.telesthete_hub import TelestheteHubTransport


def main() -> None:
    ap = argparse.ArgumentParser(prog="rook-worker")
    ap.add_argument("--hub", default="hub.example.com:443",
                    help="hub host:port (default: bakenet hub)")
    ap.add_argument("--psk", required=True,
                    help="band pre-shared key (must match peers)")
    ap.add_argument("--enable", default="",
                    help="comma-separated plugin module names to load; "
                         "default = all builtins")
    ap.add_argument("--name", default=None,
                    help="human-readable worker name (default: hostname)")
    ap.add_argument("--announce-interval", type=float, default=30.0,
                    help="seconds between announce broadcasts")
    ap.add_argument("--bind-port", type=int, default=0,
                    help="local UDP bind port (0 = ephemeral)")
    ap.add_argument("--keepalive", type=float, default=20.0,
                    help="seconds between transport keepalives")
    ap.add_argument("--ws", action="store_true",
                    help="use WebSocket instead of UDP for hub connection (for cloudflare tunnel)")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(level=max(level, logging.DEBUG),
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    host, _, port = args.hub.partition(":")
    if not port:
        ap.error("--hub must be host:port")

    if args.ws:
        from .transports.telesthete_hub import TelestheteHubTransport
        transport = TelestheteHubTransport(
            psk=args.psk, hub_host=host, hub_port=int(port),
            keepalive_secs=args.keepalive, bind_port=args.bind_port, use_ws=True,
        )
    else:
        from .transports.telesthete_hub import TelestheteHubTransport
        transport = TelestheteHubTransport(
            psk=args.psk, hub_host=host, hub_port=int(port),
            keepalive_secs=args.keepalive, bind_port=args.bind_port,
        )

    enabled = [s.strip() for s in args.enable.split(",") if s.strip()] or None
    worker = Worker(transport=transport, enabled=enabled, name=args.name,
                    announce_interval=args.announce_interval)

    async def runner() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # Windows
        task = asyncio.create_task(worker.run())
        await stop.wait()
        await worker.shutdown()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass
        return 0

    sys.exit(asyncio.run(runner()))


if __name__ == "__main__":
    main()
