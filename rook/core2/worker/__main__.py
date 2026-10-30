"""Run a 0.5 worker.

    python -m rook.core2.worker --config worker.yaml

A config (paths are relative to the file)::

    data: ./worker-data           # worker_id, replay cache, memberships, installed plugins
    anchor: /etc/rook/anchor.json # the home hub {hub, root, band}: written at enrollment, read-only here
    name: kaiju                   # default: the hostname
    worker_id: 3f0a...            # from enrollment; default: made once in <data>
    memberships: ./memberships.json   # other hubs' signed memberships + ceilings; default <data>/memberships.json
    bands:                        # one connection per band (home, co-owner and guest hubs)
      - { psk_env: ROOK_PSK, relay: "relay.example:7474" }
      - { psk_env: ROOK_GUEST_PSK, ws_url: "wss://friend.example/band" }
    plugins: [./plugins]          # plugin folders, or folders of them, loaded at start
    settings: { tv: { host: 192.168.1.40 } }   # worker-local plugin settings
    log: { level: info, format: json }

SIGINT or SIGTERM stops it cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .runtime import Worker, load_config, setup_logging


async def serve(worker: Worker) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows: Ctrl+C still raises KeyboardInterrupt
            pass
    await worker.start()
    try:
        await stop.wait()
    finally:
        await worker.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.worker", description="Rook 0.5 worker")
    ap.add_argument("--config", required=True, help="the worker's YAML config")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.log)
    try:
        asyncio.run(serve(Worker(cfg)))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as e:     # a bad anchor, an unwritable data folder
        print(f"worker: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
