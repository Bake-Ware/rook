"""Start a hub: `python -m rook_hub --data DIR --vault-key FILE [--host H] [--port P]`.

It opens the hub's data in DIR, or makes it there on the first start, unlocks the
vault with the key in FILE (X4), made on the first start and kept outside DIR,
and serves the site and the MCP door over HTTP until it is stopped (Ctrl-C or SIGTERM).
README.md has the commands from a fresh clone.
"""

import argparse
import asyncio
import signal
import sys

from . import Hub
from .errors import HubError

_VAULT = {
    "open": "open",
    "locked": "locked: the key file is missing and this vault was keyed before, so no new key was made. "
              "Put the key file back and start again.",
    "wrong_key": "locked: the key file does not hold the key this vault was keyed with. "
                 "Put the right key file in its place and start again.",
}


async def run(hub, host, port):
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    address = await hub.serve(host, port)
    with hub._lock:
        needed = not hub.auth.has_users()
    print(f"Rook hub at http://{address[0]}:{address[1]}/", flush=True)
    if needed:
        print("  This hub has no user yet: open the address above to set it up.", flush=True)
    print(f"  data:      {hub.state_dir.resolve()}", flush=True)
    print(f"  vault key: {hub.vault_key_file}", flush=True)
    print(f"  vault:     {_VAULT[hub.vault.state]}", flush=True)
    await stop.wait()
    await hub.stop_serving()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m rook_hub", description="Start a Rook hub.")
    parser.add_argument("--data", required=True, help="the hub's data directory; made on the first start")
    parser.add_argument("--vault-key", required=True,
                        help="the vault key file, outside the data directory; made on the first start")
    parser.add_argument("--host", default="127.0.0.1", help="the address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8700, help="the port to listen on (default 8700)")
    args = parser.parse_args(argv)
    try:
        hub = Hub(args.data, vault_key_file=args.vault_key)
    except HubError as e:
        sys.exit(f"rook hub: {e.message['message']} {e.message['next']}")
    try:
        asyncio.run(run(hub, args.host, args.port))
    except HubError as e:          # another hub serves this data directory
        sys.exit(f"rook hub: {e.message['message']} {e.message['next']}")
    except OSError as e:
        sys.exit(f"rook hub: cannot listen on {args.host} port {args.port}: {e.strerror or e}")
    finally:
        hub.close()


if __name__ == "__main__":
    main()
