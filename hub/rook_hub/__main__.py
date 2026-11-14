"""Start a hub: `python -m rook_hub --data DIR --vault-key FILE [--host H] [--port P]`.

It opens the hub's data in DIR, or makes it there on the first start, unlocks the
vault with the key in FILE (X4), made on the first start and kept outside DIR,
and serves the site and the MCP door over HTTP until it is stopped (Ctrl-C or SIGTERM).

Back the data up, and restore it, here or on another machine (F3; backup.py):

    python -m rook_hub backup  --data DIR --vault-key FILE --to BACKUP
    python -m rook_hub restore --from BACKUP --data DIR --vault-key FILE

README.md has the commands from a fresh clone.
"""

import argparse
import asyncio
import signal
import sys

from . import Hub, backup
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
    if hub.tightened:
        print(f"  tightened: {hub.tightened} files and folders in the data directory were open to other users; "
              "now only this hub's owner can read them", flush=True)
    print(f"  vault key: {hub.vault_key_file}", flush=True)
    print(f"  vault:     {_VAULT[hub.vault.state]}", flush=True)
    await stop.wait()
    await hub.stop_serving()


def backup_command(argv):
    parser = argparse.ArgumentParser(prog="python -m rook_hub backup",
                                     description="Write an encrypted backup of a hub's data, even while it serves.")
    parser.add_argument("--data", required=True, help="the hub's data directory")
    parser.add_argument("--vault-key", required=True, help="the hub's vault key file: the backup is encrypted with it")
    parser.add_argument("--to", required=True, help="the backup file to write; it must not exist yet")
    args = parser.parse_args(argv)
    try:
        size = backup.backup(args.data, args.vault_key, args.to)
    except HubError as e:
        sys.exit(f"rook hub backup: {e.message['message']} {e.message['next']}")
    print(f"Backup of {args.data} written to {args.to}: {size} bytes, encrypted with the key in {args.vault_key}.")
    print("Keep the vault key file apart from the backup: without it the backup cannot be opened.")


def restore_command(argv):
    parser = argparse.ArgumentParser(prog="python -m rook_hub restore",
                                     description="Write a hub's data directory from a backup.")
    parser.add_argument("--from", dest="source", required=True, help="the backup file")
    parser.add_argument("--data", required=True, help="the data directory to write; new, or empty")
    parser.add_argument("--vault-key", required=True, help="the vault key file of the hub the backup was taken from")
    args = parser.parse_args(argv)
    try:
        backup.restore(args.source, args.data, args.vault_key)
    except HubError as e:
        sys.exit(f"rook hub restore: {e.message['message']} {e.message['next']} Nothing was written.")
    print(f"Restored {args.source} into {args.data}. Start the hub on it with:")
    print(f"  python -m rook_hub --data {args.data} --vault-key {args.vault_key}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["backup"]:
        return backup_command(argv[1:])
    if argv[:1] == ["restore"]:
        return restore_command(argv[1:])
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
