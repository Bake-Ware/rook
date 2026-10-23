"""``python -m rook.agents --config <host.json>``: run the session host."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .config import Config, ConfigError
from .host import Host


async def _run(cfg: Config, token: str) -> None:
    host = Host(cfg, token)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, host.request_stop)
    await host.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.agents",
                                 description="Rook's session host for persistent agents.")
    ap.add_argument("--config", required=True, help="the host's JSON config file")
    ap.add_argument("--log-level", default=os.environ.get("ROOK_AGENTS_LOG", "INFO"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        print(f"rook.agents: {e}", file=sys.stderr)
        return 2
    token = os.environ.get(cfg.token_env, "")
    if cfg.hub_url and not token:
        print(f"rook.agents: the link token is not set (environment variable {cfg.token_env})",
              file=sys.stderr)
        return 2
    asyncio.run(_run(cfg, token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
