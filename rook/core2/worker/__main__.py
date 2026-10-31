"""Enroll a 0.5 worker with its home hub, or run it.

    python -m rook.core2.worker --config worker.yaml enroll https://rook.example [--name kaiju]
    python -m rook.core2.worker --config worker.yaml enroll 'rook://enroll?url=…&code=…&hub=…'
    python -m rook.core2.worker --config worker.yaml enroll --discover
    python -m rook.core2.worker --config worker.yaml
    python -m rook.core2.worker --config worker.yaml check   # the smoke check of a core update

Running, it first picks its core (rook.core2.worker.boot): the installed
package, or the release a hub installed under ``<data>/core/``, and execs into
it when that isn't the code already running.

``enroll`` (spec 12; rook.core2.enroll.device) signs the machine in as a
person: with the QR's one-time code, else by showing a short code the person
approves on their phone or the dashboard. It writes the anchor, the home
band and the config's ``anchor``/``name``/``worker_id``; the rest of an
existing config is kept.

A config (paths are relative to the file)::

    data: ./worker-data           # worker_id, replay cache, memberships, bands, installed plugins
    anchor: /etc/rook/anchor.json # the home hub {hub, root, band}: written at enrollment, read-only here
    name: kaiju                   # default: the hostname
    worker_id: 3f0a...            # from enrollment; default: made once in <data>
    memberships: ./memberships.json   # other hubs' signed memberships + ceilings; default <data>/memberships.json
    bands:                        # more bands by hand; enrollment and core.trust.set keep theirs in <data>/bands.json
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
from pathlib import Path

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


def enroll(args) -> int:
    from .. import wire
    from ..enroll import device, mdns
    config = Path(args.config)
    try:
        if bool(args.hub) == args.discover:
            raise device.EnrollError("give the hub's address (or QR text), or --discover")
        url, code, pinned = device.parse_target(args.hub) if args.hub else (None, None, None)
        if args.discover:
            url, pinned = device.find_hub(mdns.discover)
            print(f"Found hub {pinned} at {url}")
        device.check_url(url, args.insecure)
        if not args.force and device.home_of(config):
            raise device.EnrollError(device.already(device.home_of(config)))
        name = args.name or device.default_name(config)
        key = device.signing_key(config, args.force)
        bundle = device.sign_in(url, name, args.code or code, pinned, key=wire.pub_text(key),
                                say=lambda s: print(s, flush=True))
        paths = device.install(config, bundle, url, force=args.force, key=key)
    except device.EnrollError as e:
        print(f"enroll: {e}", file=sys.stderr)
        return 1
    print(f"Enrolled {bundle['name']} ({bundle['worker_id']}) with hub {bundle['hub']['hub_id']}, "
          f"owned by {bundle.get('owner')}.\nStart it:  python -m rook.core2.worker --config {paths['config']}")
    return 0


def check(args) -> int:
    """Import this core, read the config and validate core's own manifests;
    print where the code came from. A core update runs it from the new
    release before switching (rook.core2.worker.update.smoke)."""
    import json
    from rook.kit.manifest import check as check_manifest
    from ..builtin import CORE, STREAM, WORKER
    from . import boot, builtins, host, install, runtime, update  # noqa: F401  (imports are the check)
    try:
        load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    for path in (CORE, WORKER, STREAM):
        data, issues = check_manifest(path)
        errors = [str(i) for i in issues if i.level == "error"]
        if data is None or errors:
            print(f"{path.name}: {errors}", file=sys.stderr)
            return 1
    folder = boot.running_folder()
    print(json.dumps({"ok": True, "folder": str(folder) if folder else None, "version": boot.running()}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.worker", description="Rook 0.5 worker")
    ap.add_argument("--config", required=True, help="the worker's YAML config")
    sub = ap.add_subparsers(dest="cmd")
    en = sub.add_parser("enroll", help="sign this machine in with its home hub (writes the anchor and config)")
    en.add_argument("hub", nargs="?", help="the hub's address, or the text of its enrollment QR")
    en.add_argument("--discover", action="store_true", help="find the hub on the local network")
    en.add_argument("--code", help="a one-time code from the hub (else a code to approve is shown)")
    en.add_argument("--name", help="this machine's name on the band (default: the config's, else the hostname)")
    en.add_argument("--insecure", action="store_true", help="allow a plain http hub address off this machine")
    en.add_argument("--force", action="store_true", help="enroll again: a new home hub, every other band dropped")
    sub.add_parser("check", help="import this core and check the config and core's manifests (update smoke check)")
    args = ap.parse_args(argv)
    if args.cmd == "enroll":
        return enroll(args)
    if args.cmd == "check":
        return check(args)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.log)
    from . import boot
    try:
        boot.select(cfg.data)             # may exec into another core and not return
    except Exception as e:                # a broken core folder or state: run what is here, never not at all
        print(f"worker: core release not switched: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        asyncio.run(serve(Worker(cfg, config_path=Path(args.config).resolve())))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as e:     # a bad anchor, an unwritable data folder
        print(f"worker: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
