"""Run a 0.5 hub, or mint a door token.

    python -m rook.core2.hub --config hub.yaml
    python -m rook.core2.hub --config hub.yaml token --person bake [--agent claude-code] [--label laptop]

A config (paths are relative to the file)::

    data: ./hub-data              # keys, journal, records, tokens, workers
    band: e17904e6ed4c664592f64f6f1d02c682
    library: ./plugins            # trusted plugin folders
    users:
      bake: { preset: owner }     # at least one owner; their first token lands in <data>/owner.token
    agents:
      claude-code: { grants: ["*"], max_risk: exec }
    doors:
      http: { bind: "127.0.0.1:8740" }
    transport:                    # rook.core2.transport options; leave out for no band
      bands: [ { psk_env: ROOK_PSK, relay: "relay.example:7474" } ]
    log: { level: info, format: json }

SIGINT or SIGTERM stops it cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .identity import Caller
from .runtime import Hub, add_token, load_config, setup_logging


async def serve(cfg) -> None:
    hub = Hub(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows: Ctrl+C still raises KeyboardInterrupt
            pass
    await hub.start()
    try:
        await stop.wait()
    finally:
        await hub.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.hub", description="Rook 0.5 hub")
    ap.add_argument("--config", required=True, help="the hub's YAML config")
    sub = ap.add_subparsers(dest="cmd")
    tok = sub.add_parser("token", help="mint a door token (printed once; only its hash is kept)")
    tok.add_argument("--person", help="the person it calls for (a user in the config)")
    tok.add_argument("--agent", help="the agent that calls (an agent in the config)")
    tok.add_argument("--label", default="")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if args.cmd == "token":
        if args.person and args.person not in cfg.users:
            print(f"token: no user {args.person!r} in the config", file=sys.stderr)
            return 2
        if args.agent and args.agent not in cfg.agents:
            print(f"token: no agent {args.agent!r} in the config", file=sys.stderr)
            return 2
        caller = Caller(f"person:{args.person}" if args.person else None,
                        f"agent:{args.agent}" if args.agent else None)
        try:
            cfg.data.mkdir(parents=True, exist_ok=True, mode=0o700)
            print(add_token(cfg.data, caller, args.label))
        except ValueError as e:
            print(f"token: {e}", file=sys.stderr)
            return 2
        return 0
    setup_logging(cfg.log)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
