"""Desktop launcher shipped inside the worker bundle, independent of its service."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shlex
import sys
import tempfile

MARKER = "# rook managed worker CLI"


def _write(path: Path, text: str, mode: int = 0o755) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            os.chmod(name, mode)
            out.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _posix_path(home: Path, dest: Path) -> None:
    # Both login and interactive shells need the path, including existing workers
    # upgraded from a service with a minimal PATH. Never source users' rc files.
    quoted = shlex.quote(str(dest))
    block = (f"\n{MARKER}\ncase \":$PATH:\" in\n"
             f"  *:{quoted}:*) ;;\n  *) export PATH={quoted}:\"$PATH\" ;;\nesac\n")
    for name in (".profile", ".bashrc", ".zshrc"):
        path = home / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if MARKER not in text:
                with path.open("a", encoding="utf-8") as out:
                    out.write(block)
        else:
            _write(path, block, 0o600)
    fish = home / ".config/fish/conf.d/rook-cli.fish"
    if not fish.exists() or MARKER in fish.read_text(encoding="utf-8"):
        _write(fish, f"{MARKER}\nif not contains -- {quoted} $PATH\n"
                    f"    set -gx PATH {quoted} $PATH\nend\n", 0o600)


def _windows_path(dest: Path) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        if str(dest).casefold() not in [p.rstrip("\\").casefold() for p in value.split(";")]:
            winreg.SetValueEx(key, "Path", 0, kind, str(dest) + ";" + value)
            # Notify desktop shells without blocking the worker indefinitely.
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(
                0xffff, 0x1a, 0, "Environment", 2, 1000, None)


def install_launcher() -> dict:
    """Install from the canonical bundle only; safe to repeat after each OTA boot.

    No worker credentials are copied into the dashboard's independent config.
    Native APK runtimes and source checkouts are deliberately excluded.
    """
    home = Path.home()
    bundle = home / ".rook-band-worker/band-worker.pyz"
    if os.environ.get("ANDROID_ARGUMENT") or (
        hasattr(sys, "getandroidapilevel") and not os.environ.get("TERMUX_VERSION")
    ):
        return {"installed": False, "reason": "native Android runtime"}
    if Path(sys.argv[0]).absolute() != bundle.absolute() or not bundle.is_file():
        return {"installed": False, "reason": "not an installed worker bundle"}
    windows = sys.platform == "win32"
    dest = home / ".local/bin"
    launcher = dest / ("rook.cmd" if windows else "rook")
    python = Path(sys.executable)  # Do not resolve symlinks out of the worker venv.
    if windows:
        python = python.with_name("python.exe")
        quote = lambda p: '"' + str(p).replace("%", "%%") + '"'
        text = f"@echo off\r\nREM {MARKER}\r\n{quote(python)} {quote(bundle)} --cli %*\r\n"
    else:
        text = f"#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(python))} {shlex.quote(str(bundle))} --cli \"$@\"\n"
    if launcher.exists() or launcher.is_symlink():
        # Preserve unrelated commands. Replace our older standalone TUI only
        # with a backup, so users can recover their old install if necessary.
        if launcher.is_symlink():
            return {"installed": False, "reason": f"existing symlink: {launcher}"}
        existing = launcher.read_text(encoding="utf-8")
        if MARKER not in existing:
            if "terminal control panel for the worker band" not in existing.lower():
                return {"installed": False, "reason": f"unmanaged command: {launcher}"}
            backup = launcher.with_name(launcher.name + ".pre-worker-cli")
            if not backup.exists():
                _write(backup, existing)
        if existing != text:
            _write(launcher, text)
    else:
        _write(launcher, text)
    if windows:
        _windows_path(dest)
    else:
        _posix_path(home, dest)
    return {"installed": True, "launcher": str(launcher)}


def ensure_launcher() -> None:
    # A read-only home or conflicting command must never prevent mesh startup.
    try:
        result = install_launcher()
        if not result["installed"] and result["reason"].startswith(("existing", "unmanaged")):
            logging.getLogger(__name__).warning("CLI not installed: %s", result["reason"])
    except Exception:
        logging.getLogger(__name__).exception("Could not install the rook terminal launcher")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help"):
        print("Rook — worker and terminal dashboard\n\n"
              "Usage: rook [band|tui] [dashboard options]\n"
              "       rook worker [worker options]\n\n"
              "Run rook with no arguments to open the dashboard.\n"
              "rook band --help     Dashboard connection options\n"
              "rook worker --help   Background worker options\n"
              "rook --version       Installed bundle version")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        from ._build_info import VERSION
        print(VERSION)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        del sys.argv[1]
        from .cli import main as worker_main
        worker_main()
        return
    from rook.cli.band_tui import main as band_main
    band_main()
