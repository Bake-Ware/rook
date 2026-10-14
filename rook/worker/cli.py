"""``python -m rook.worker`` — boot a rook worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from ..core.paths import worker_state_dir, worker_state_path
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
    # Keep legacy zipapp/service arguments working. Only the desktop launcher
    # selects the dashboard; a service never falls into an interactive prompt.
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        del sys.argv[1]
        from .desktop import main as desktop_main
        desktop_main()
        return
    ap = argparse.ArgumentParser(
        prog="rook-worker",
        epilog="State (identity, enrollment, pushed config, audit log, plugin data) "
               "lives in ROOK_WORKER_HOME, default ~/.rook-band-worker; the installed "
               "bundle stays in ~/.rook-band-worker. With ROOK_WORKER_HOME set, state "
               "missing there is copied once from ~/.rook-band-worker (older releases "
               "kept it there); ROOK_WORKER_HOME_FRESH=1 starts a separate worker instead. "
               "ROOK_CAP_TIMEOUT = seconds a capability call may run "
               "before it is abandoned with a timeout error (default 300; 0 = no limit). "
               "Caps that take a timeout argument get that plus a margin; long-running "
               "caps (updates) opt out.")
    ap.add_argument("--hub", default=os.environ.get("ROOK_HUB", "127.0.0.1:7474"),
                    help="hub host:port (or env ROOK_HUB; default 127.0.0.1:7474)")
    ap.add_argument("--psk", default=os.environ.get("ROOK_BAND_PSK") or None,
                    help="band pre-shared key, must match peers (or env ROOK_BAND_PSK)")
    ap.add_argument("--enroll", metavar="HTTPS_SERVER", help="sign in through Google/local web login and fetch your configurations")
    ap.add_argument("--pair-code", default=None, help="use a temporary pairing code with --enroll instead of account login")
    ap.add_argument("--enrolled", action="store_true", help="use the active band saved by --enroll")
    ap.add_argument("--band", default=None,
                    help="with --enroll and an account login: the band to join when the "
                         "account has several (or env ROOK_BAND)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="never prompt (also env ROOK_YES=1); prompts appear only at a terminal")
    ap.add_argument("--enrolled-band", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="store_true",
                    help="print the bundle version and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="load all plugins offline, print version, exit 0 "
                         "(used as a pre-swap smoke test by OTA self-update)")
    ap.add_argument("--install-cli", action="store_true",
                    help="install the bundled terminal dashboard launcher and exit")
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
                    help="seconds of link silence before a transport keepalive "
                         "(skipped while other frames flow). Tuning env: "
                         "ROOK_BAND_UDP_PACE_RATE/_BURST (UDP bulk pacing), "
                         "ROOK_BAND_DISPATCH_CONCURRENCY/_BACKLOG (inbound bound)")
    ap.add_argument("--ws", action="store_true",
                    help="use WebSocket instead of UDP for hub connection (for cloudflare tunnel)")
    ap.add_argument("--ws-url", default=None,
                    help="full ws:// or wss:// URL of the band endpoint, e.g. the relay's "
                         "native WebSocket listener; implies --ws (or env ROOK_HUB_WS_URL, "
                         "which a pushed config may set). Default: the hub's /band bridge "
                         "derived from --hub")
    ap.add_argument("--update-url", default=os.environ.get("ROOK_UPDATE_URL", ""),
                    help="signed manifest URL for OTA self-update "
                         "(e.g. https://rook.example.com/band-worker.json). "
                         "Empty = auto-update disabled.")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()
    requested = (args.hub, args.psk)

    from ._build_info import VERSION

    if args.version:
        print(VERSION)
        return

    if args.selftest:
        # Import + construct the plugin registry offline (no transport, no band).
        # A broken/incompatible bundle fails here with a non-zero exit — which is
        # exactly what the OTA self-update checks before swapping a downloaded pyz.
        from ..core.host import PluginHost
        from . import desktop
        from rook.cli import band_tui
        # Every built-in plugin, without placement: the check is that each
        # one imports and constructs, not whether it would run on this host.
        host = PluginHost(check_placement=False)
        reg = host.registry
        plugins = host.load(PluginHost.discover("rook.worker.plugins", entry_points=False))
        print(f"selftest OK v{VERSION}: {len(plugins)} plugins, {len(reg.list())} caps")
        return

    if args.install_cli:
        from .desktop import install_launcher
        result = install_launcher()
        if not result["installed"]:
            ap.error(result["reason"])
        # The installers say this once at the end, in their own words.
        if os.environ.get("ROOK_INSTALLER") != "1":
            print(f"Installed {result['launcher']}; open a new terminal and run rook.")
        return

    # Also covers existing workers receiving their first CLI through OTA. This
    # runs only after offline validation modes, which must have no side effects.
    from .desktop import ensure_launcher
    ensure_launcher()

    # State kept in ~/.rook-band-worker before ROOK_WORKER_HOME moved all of
    # it (identity, enrollment, deauth flag, pushed config...): copy it over
    # once, before anything reads it. Offline modes above stay side-effect free.
    from ..core.paths import migrate_legacy_state
    migrate_legacy_state()

    if args.enrolled_band:
        # For the installers' last line ("Done: <worker> joined <band>").
        from .enroll import active_band_name
        print(active_band_name())
        return
    if args.yes:
        os.environ["ROOK_YES"] = "1"
    if args.enroll:
        from .enroll import enroll, plain_error
        try:
            enroll(args.enroll, args.pair_code, name=args.name, band=args.band)
        except (ValueError, OSError) as error:
            # One plain line, not argparse's usage dump: this is what a person
            # sees when a code expired or the hub is unreachable.
            print(f"Could not join the band: {plain_error(error)}", file=sys.stderr)
            sys.exit(2)
        return
    from .enroll import load as load_enrollment
    if load_enrollment().get('auto_start'):
        args.enrolled = True
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
        ap.error("--psk (or env ROOK_BAND_PSK) is required")

    # Deauth gate: a worker that received a signed worker.deauth parks itself
    # OFF the band (no transport, no announce) instead of rejoining. Runs after
    # --version/--selftest (so OTA bundle validation still works on a banned
    # node) but before any band contact. The flag survives reboots; clearing
    # <state dir>/banned and restarting rejoins.
    from .plugins.selfupdate import is_banned, ban_info
    if is_banned():
        logging.basicConfig(level=logging.WARNING,
                            format="%(asctime)s %(name)s %(levelname)s: %(message)s")
        info = ban_info()
        logging.getLogger("rook.worker").warning(
            "DEAUTHED from band (at=%s reason=%r) — staying dormant, NOT joining. "
            "Clear %s and restart to rejoin.",
            info.get("at"), info.get("reason", ""), worker_state_path("banned"))
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

    level = max(logging.WARNING - 10 * args.verbose, logging.DEBUG)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    # `rook logs` reads INFO and up from an in-memory ring over the local
    # control link (the Windows task has no console to log to); the console
    # keeps the level asked for.
    from .control import LogRing, enabled as control_enabled
    ring = LogRing() if control_enabled() else None
    if ring is not None:
        root = logging.getLogger()
        for h in root.handlers:
            h.setLevel(level)
        root.addHandler(ring)
        ring.setLevel(logging.INFO)
        root.setLevel(min(level, logging.INFO))

    # Saved enrollment and pushed config beat --hub/--psk; say so, because a
    # second worker started on an enrolled machine otherwise quietly joins the
    # enrolled band instead of the one on its command line.
    if (args.hub, args.psk) != requested:
        logging.getLogger("rook.worker").warning(
            "using hub %s%s from saved state in %s (enrollment or "
            "pushed config), not the --hub/--psk given", args.hub,
            " and a different band key" if args.psk != requested[1] else "",
            worker_state_dir())

    host, _, port = args.hub.partition(":")
    if not port:
        ap.error("--hub must be host:port")

    # Read after the pushed config's env is applied, so the hub can move
    # workers to another WS endpoint with worker.config_apply.
    ws_url = (args.ws_url or os.environ.get("ROOK_HUB_WS_URL", "")).strip() or None
    if ws_url and not ws_url.startswith(("ws://", "wss://")):
        ap.error("--ws-url / ROOK_HUB_WS_URL must start with ws:// or wss://")
    if args.ws or ws_url:
        from .transports.telesthete_hub import TelestheteHubTransport
        transport = TelestheteHubTransport(
            psk=args.psk, hub_host=host, hub_port=int(port),
            keepalive_secs=args.keepalive, bind_port=args.bind_port, use_ws=True,
            ws_url=ws_url,
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
        control = None
        if ring is not None:
            # A failure here (no loopback, read-only state dir) must never
            # keep the worker off the band: the local CLI just reports it.
            from .control import ControlServer
            try:
                control = ControlServer(worker, ring)
                await control.start()
            except Exception as error:
                logging.getLogger("rook.worker").warning(
                    "local control link unavailable (%s); `rook status` will "
                    "read files only", error)
                control = None

        async def enrollment_watch():
            nonlocal outcome
            from .enroll import refresh
            while True:
                await asyncio.sleep(30)
                if not args.enrolled and not load_enrollment().get('auto_start'):
                    continue
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

        enrollment_task = asyncio.create_task(enrollment_watch())
        await stop.wait()
        if enrollment_task:
            enrollment_task.cancel()
            await asyncio.gather(enrollment_task, return_exceptions=True)
        if control is not None:
            await control.stop()
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
