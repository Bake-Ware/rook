"""``python -m rook.worker`` — boot a rook worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from .core import Worker


def _force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 so the worker never dies with a
    UnicodeEncodeError when it prints non-ASCII (emoji, box-drawing, the
    ☤ brand, worker names, chat text). On Windows the console defaults to
    the locale ANSI code page (cp1252), whose 'charmap' codec raises on any
    character it can't represent; POSIX terminals are usually UTF-8 already,
    so this is a no-op there. errors='replace' guarantees output never
    crashes even for characters the terminal font can't show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except Exception:
            pass


def main() -> None:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(prog="rook-worker")
    ap.add_argument("--hub", default="hub.example.com:443",
                    help="hub host:port (default: bakenet hub)")
    ap.add_argument("--psk", default=None,
                    help="band pre-shared key (must match peers)")
    ap.add_argument("--enroll", metavar="HTTPS_SERVER", help="sign in through Google/local web login and fetch your configurations")
    ap.add_argument("--pair-code", default=None, help="use a temporary pairing code with --enroll instead of account login")
    ap.add_argument("--enrolled", action="store_true", help="use the active band saved by --enroll")
    ap.add_argument("--version", action="store_true",
                    help="print the bundle version and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="load all plugins offline, print version, exit 0 "
                         "(used as a pre-swap smoke test by OTA self-update)")
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
    ap.add_argument("--update-url", default=os.environ.get("ROOK_UPDATE_URL", ""),
                    help="signed manifest URL for OTA self-update "
                         "(e.g. https://rook.example.com/band-worker.json). "
                         "Empty = auto-update disabled.")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    from ._build_info import VERSION

    if args.version:
        print(VERSION)
        return

    if args.selftest:
        # Import + construct the plugin registry offline (no transport, no band).
        # A broken/incompatible bundle fails here with a non-zero exit — which is
        # exactly what the OTA self-update checks before swapping a downloaded pyz.
        from .registry import CapabilityRegistry
        from .plugin import load_plugins
        reg = CapabilityRegistry()
        plugins = load_plugins("rook.worker.plugins", reg, None)
        print(f"selftest OK v{VERSION}: {len(plugins)} plugins, {len(reg.list())} caps")
        return

    if args.enroll:
        from .enroll import enroll
        try:
            enroll(args.enroll,args.pair_code)
        except (ValueError,OSError) as error:
            ap.error(str(error))
        return
    if args.enrolled:
        from .enroll import refresh
        try:
            saved=refresh()
        except (ValueError,OSError) as error:
            ap.error(str(error))
        band=next((b for b in saved.get('bands',[]) if b['id']==saved.get('active_band')),None)
        if not band:
            ap.error('No enrolled band; run --enroll HTTPS_SERVER first.')
        args.psk=band['psk'];args.hub=band['hub']
    if not args.psk:
        ap.error("--psk is required")

    # Deauth gate: a worker that received a signed worker.deauth parks itself
    # OFF the band (no transport, no announce) instead of rejoining. Runs after
    # --version/--selftest (so OTA bundle validation still works on a banned
    # node) but before any band contact. The flag survives reboots; clearing
    # ~/.rook-band-worker/banned and restarting rejoins.
    from .plugins.selfupdate import is_banned, ban_info
    if is_banned():
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s %(name)s %(levelname)s: %(message)s")
        info = ban_info()
        logging.getLogger("rook.worker").warning(
            "DEAUTHED from band (at=%s reason=%r) — staying dormant, NOT joining. "
            "Clear ~/.rook-band-worker/banned and restart to rejoin.",
            info.get("at"), info.get("reason", ""))
        # Park quietly so we don't busy-loop under a service manager's Restart
        # policy; a stop signal (systemctl stop) wakes us to exit.
        try:
            signal.pause()          # POSIX: block until a signal
        except (AttributeError, ValueError):
            while True:
                time.sleep(3600)    # Windows / no signal.pause
        return

    # Expose the manifest URL to the selfupdate plugin (reads ROOK_UPDATE_URL).
    if args.update_url:
        os.environ["ROOK_UPDATE_URL"] = args.update_url

    # Remote config overrides (design §1). Auto-revert a stranded pending config
    # first, then apply env-gates BEFORE plugins load (so a pushed ROOK_WAKE_CMD/
    # ROOK_MEMORY_VAULT actually turns the cap on) and merge simple settings over
    # the installer args (the pushed config is the override, so it wins).
    from . import wconfig
    reverted = wconfig.boot_reconcile()
    if reverted:
        logging.getLogger("rook.worker").warning(reverted)
    _cfg = wconfig.load()
    wconfig.apply_env(_cfg)
    if _cfg.get("name"):
        args.name = _cfg["name"]
    if _cfg.get("announce_interval"):
        try:
            args.announce_interval = float(_cfg["announce_interval"])
        except (TypeError, ValueError):
            pass
    if _cfg.get("hub") and not args.enrolled:
        args.hub = str(_cfg["hub"])
    if _cfg.get("psk"):
        if not args.enrolled:
            args.psk = str(_cfg["psk"])
    if _cfg.get("log_level"):
        _lvl = str(_cfg["log_level"]).upper()
        if _lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            args.verbose = max(args.verbose, {"ERROR": 0, "WARNING": 0,
                                              "INFO": 1, "DEBUG": 2}[_lvl])

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
        outcome = 0
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # Windows
        task = asyncio.create_task(worker.run())
        task.add_done_callback(lambda _: stop.set())

        async def enrollment_watch():
            nonlocal outcome
            from .enroll import refresh
            while True:
                await asyncio.sleep(30)
                try:
                    saved = await asyncio.to_thread(refresh)
                    band = next(b for b in saved['bands'] if b['id'] == saved['active_band'])
                    if band['psk'] != args.psk or band['hub'] != args.hub:
                        logging.getLogger('rook.worker').info('Enrolled band configuration changed; reconnecting.')
                        outcome = 75
                        stop.set()
                        return
                except Exception as error:
                    logging.getLogger('rook.worker').error('Enrollment authorization unavailable (%s); leaving band.', type(error).__name__)
                    outcome = 78
                    stop.set()
                    return

        enrollment_task = asyncio.create_task(enrollment_watch()) if args.enrolled else None
        await stop.wait()
        if enrollment_task:
            enrollment_task.cancel()
            await asyncio.gather(enrollment_task, return_exceptions=True)
        await worker.shutdown()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass
        return outcome

    outcome = asyncio.run(runner())
    if outcome == 75:
        # Preserve -m versus zipapp invocation and every original installer arg.
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    sys.exit(outcome)


if __name__ == "__main__":
    main()
