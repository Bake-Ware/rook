"""Set up a 0.5 hub, run it, mint a door token, or show the recovery key.

    python -m rook.core2.hub --config hub.yaml init --owner bake --relay relay.example:7474 [--url https://rook.example]
    python -m rook.core2.hub --config hub.yaml
    python -m rook.core2.hub --config hub.yaml token --person bake [--agent claude-code] [--label laptop]
    python -m rook.core2.hub --config hub.yaml recovery-key
    python -m rook.core2.hub --config hub.yaml secret list | set NAME [--description TEXT] | delete NAME
    python -m rook.core2.hub --config hub.yaml recover-vault
    python -m rook.core2.hub --config hub.yaml backup [run|list|verify [NAME]] [--target dir:/mnt/usb]
    python -m rook.core2.hub --config new/hub.yaml restore (FILE | --target T [--name NAME])

``secret set`` reads the value from stdin (never the command line, which
other users of the machine can see). ``recover-vault`` opens a ``vault.db``
copied from a hub that died, with the owner's recovery key (typed, or on
stdin), and seals it under this hub's root key.

``init`` is the owner hub's first run: it writes the config (with a new band
key), makes the hub's keys, and prints the owner's sign-in token and the
recovery key, once. ``backup`` makes a sealed backup (rook.core2.hub.backup)
while the hub runs or not; ``restore`` rebuilds a hub on a fresh machine from
one, asking for the recovery key, and writes ``--config`` with ``data/`` and
``library/`` beside it. Devices then enroll with ``python -m rook.core2.worker
--config worker.yaml enroll`` (spec 12).

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
      mcp: { bind: "127.0.0.1:8741" }   # agents: http://127.0.0.1:8741/mcp with an --agent token
    transport:                    # rook.core2.transport options; leave out for no band
      bands: [ { psk_env: ROOK_PSK, relay: "relay.example:7474" } ]
    enroll:                       # what enrolling devices are told (rook.core2.enroll.hub)
      url: https://rook.example   # where they reach this door (behind TLS)
      relay: rook.example:7474    # the band relay as devices reach it, if not the transport's
      discover: true              # answer local network discovery (mDNS _rook-hub._tcp)
      services: { voice: { url: "https://voice.example", key_env: VOICE_KEY, presets: [owner, adult] } }
    log: { level: info, format: json }

SIGINT or SIGTERM stops it cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import signal
import sys
from pathlib import Path

import yaml

from .. import wire
from . import backup as bk
from ..enroll.hub import recovery_key
from .identity import Caller
from .runtime import Hub, _key, _write_private, add_token, home_band, load_config, setup_logging
from .vault import Vault, VaultError


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


def init(path: Path, owner: str, relay: str | None, ws_url: str | None, url: str | None, bind: str) -> int:
    """The owner hub's first run: config, keys, owner token, recovery key."""
    if path.exists():
        print(f"init: {path} exists; this hub is set up already", file=sys.stderr)
        return 2
    if not (relay or ws_url):
        print("init: give the band's --relay (host:port) or --ws-url", file=sys.stderr)
        return 2
    band = {"psk": secrets.token_hex(24), **({"relay": relay} if relay else {}), **({"ws_url": ws_url} if ws_url else {})}
    raw = {"data": "./data", "library": "./library", "users": {owner: {"preset": "owner"}},
           "doors": {"http": {"bind": bind}}, "transport": {"bands": [band]}}
    if url:
        raw["enroll"] = {"url": url}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, "# Rook hub, written by init. It holds the band key: keep it private.\n"
                   + yaml.safe_dump(raw, sort_keys=False))
    try:
        cfg = load_config(path)
    except ValueError as e:
        path.unlink()
        print(f"init: {e}", file=sys.stderr)
        return 2
    (path.parent / "library").mkdir(exist_ok=True)
    (cfg.data / "keys").mkdir(parents=True, exist_ok=True, mode=0o700)
    root = _key(cfg.data / "keys" / "root.key")
    token = add_token(cfg.data, Caller(person=f"person:{owner}"), "first owner")
    print(f"Rook hub {wire.key_id(bytes(root.verify_key))} set up in {path.parent} (band {home_band(cfg)[:8]}).")
    print(f"\nSign-in token for {owner}, shown once (only its hash is kept):\n  {token}")
    print(f"\nRecovery key: save it however you like. You need it only if this hub dies;\n"
          f"while it lives, the recovery-key command shows it again.\n  {recovery_key(cfg.data)}")
    print(f"\nStart it:  python -m rook.core2.hub --config {path}")
    print(f"Add a machine:  python -m rook.core2.worker --config worker.yaml enroll {url or 'http://' + bind}")
    return 0


def _read(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.read().rstrip("\r\n")


def vault_cmd(cfg, args) -> int:
    """``secret ...`` and ``recover-vault``: the vault from the hub's machine.
    Nothing caches a value, so the hub may be running."""
    v = Vault(cfg.data)
    if args.cmd == "recover-vault" or args.action == "set":
        (cfg.data / "keys").mkdir(parents=True, exist_ok=True, mode=0o700)
        _key(cfg.data / "keys" / "root.key")      # as the hub's first start would
    try:
        if args.cmd == "recover-vault":
            v.recover(_read("Recovery key: "))
            print(f"vault recovered: {len(v.names())} secrets, now sealed under this hub's root key; "
                  "the recovery key you typed is this hub's recovery key")
            return 0
        if args.action == "list":
            for s in v.list():
                print(f"{s['name']}\t{s['description']}")
            return 0
        if not args.name:
            print(f"secret {args.action}: give a NAME", file=sys.stderr)
            return 2
        if args.action == "delete":
            if not v.delete(args.name):
                print(f"secret delete: {args.name} is not in the vault", file=sys.stderr)
                return 2
            return 0
        v.unlock().set(args.name, _read(f"Value for {args.name}: "), by="cli", description=args.description)
        return 0
    except VaultError as e:
        print(f"{args.cmd}: {e}", file=sys.stderr)
        return 2


def _settings(cfg, spec: str | None) -> tuple[str, int]:
    s = cfg.settings.get("backup") or {}
    return spec or str(s.get("target") or ""), int(s.get("keep") or bk.KEEP)


def backup_cmd(cfg, action: str, name: str | None, spec: str | None) -> int:
    """Without the running hub: a dir or rclone target (a worker target is the
    ``backup.run`` cap's, since it goes over the band)."""
    spec, keep = _settings(cfg, spec)
    if action == "run":
        out = bk.run(config=cfg.file, data=cfg.data, library=cfg.library, spec=spec, keep=keep)
    else:
        t, hub = bk.target(spec), bk.hub_id_of(cfg.data)
        if action == "list":
            out = {"target": t.label, "keep": keep, "backups": bk.backups(t, hub)}
        else:
            found = name or next(iter(b["name"] for b in bk.backups(t, hub)), None)
            if not found:
                raise bk.BackupError(f"no backups of this hub on {t.label}")
            out = {"name": found, "target": t.label, **bk.fetch_verify(t, found, cfg.data)}
    print(json.dumps(out, indent=1))
    return 0


def restore_cmd(config: Path, src: str | None, spec: str | None, name: str | None) -> int:
    if bool(src) == bool(spec):
        print("restore: give a backup FILE or --target", file=sys.stderr)
        return 2
    folder = config.absolute().parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fetched = None
    try:
        if spec:
            fetched = folder / f".fetch-{os.getpid()}{bk.SUFFIX}"
            name = bk.fetch(spec, name, fetched)
            print(f"fetched {name} from {spec}", file=sys.stderr)
        out = bk.restore(Path(src) if src else fetched, _read("Recovery key: "), config)
    finally:
        if fetched is not None:
            fetched.unlink(missing_ok=True)
    print(f"Restored hub {out['hub']} ({out['files']} files, {out['databases']} databases) into {folder}.")
    print("Its recovery key is the one you just used: the recovery-key command shows it again.")
    print(f"Start it:  python -m rook.core2.hub --config {config}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.hub", description="Rook 0.5 hub")
    ap.add_argument("--config", required=True, help="the hub's YAML config")
    sub = ap.add_subparsers(dest="cmd")
    tok = sub.add_parser("token", help="mint a door token (printed once; only its hash is kept)")
    tok.add_argument("--person", help="the person it calls for (a user in the config)")
    tok.add_argument("--agent", help="the agent that calls (an agent in the config)")
    tok.add_argument("--label", default="")
    ini = sub.add_parser("init", help="set up a new hub: config, keys, the owner's token and recovery key")
    ini.add_argument("--owner", required=True, help="the owner's user name (lowercase)")
    ini.add_argument("--relay", help="the band's Telesthete relay, host:port")
    ini.add_argument("--ws-url", help="or its WebSocket address")
    ini.add_argument("--url", help="where devices reach this hub's door (https://...), for enrollment")
    ini.add_argument("--bind", default="127.0.0.1:8740", help="the HTTP door's address (default %(default)s)")
    sub.add_parser("recovery-key", help="show the owner's recovery key")
    sec = sub.add_parser("secret", help="the hub's vault: list, set (value on stdin) or delete a secret")
    sec.add_argument("action", choices=["list", "set", "delete"])
    sec.add_argument("name", nargs="?")
    sec.add_argument("--description")
    sub.add_parser("recover-vault", help="open a vault.db from a hub that died, with the owner's recovery key")
    bak = sub.add_parser("backup", help="make, list or verify sealed backups (dir or rclone targets)")
    bak.add_argument("action", nargs="?", default="run", choices=("run", "list", "verify"))
    bak.add_argument("name", nargs="?", help="verify: which backup (default: the newest)")
    bak.add_argument("--target", help="instead of the backup plugin's target setting")
    res = sub.add_parser("restore", help="rebuild a hub on a fresh machine from a backup; writes --config")
    res.add_argument("file", nargs="?", help="a .rookbak file")
    res.add_argument("--target", help="or fetch from a dir or rclone target")
    res.add_argument("--name", help="with --target: which backup (default: the newest)")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        return init(Path(args.config), args.owner, args.relay, args.ws_url, args.url, args.bind)
    if args.cmd == "restore":
        try:
            return restore_cmd(Path(args.config), args.file, args.target, args.name)
        except (bk.BackupError, OSError) as e:
            print(f"restore: {e}", file=sys.stderr)
            return 1
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if args.cmd == "recovery-key":
        print(recovery_key(cfg.data))
        return 0
    if args.cmd in ("secret", "recover-vault"):
        return vault_cmd(cfg, args)
    if args.cmd == "backup":
        try:
            return backup_cmd(cfg, args.action, args.name, args.target)
        except (bk.BackupError, OSError) as e:
            print(f"backup: {e}", file=sys.stderr)
            return 1
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
