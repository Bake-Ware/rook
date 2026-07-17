"""Combined HTTP + WebSocket server for remote workers on a single port."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .server import RemoteWorker

log = logging.getLogger(__name__)

WORKER_SCRIPT = (Path(__file__).parent / "worker.py").read_text(encoding="utf-8")

PS_BOOTSTRAP = '''
# R00K Band Worker Bootstrap (Windows)
$ErrorActionPreference = "Stop"

# Install Python if missing
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {{
    Write-Host "[r00k] Python not found. Installing..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {{
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements -h
    }} elseif (Get-Command choco -ErrorAction SilentlyContinue) {{
        choco install python3 -y
    }} else {{
        Write-Host "[r00k] Downloading Python installer..."
        $pyUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
        $pyInstaller = "$env:TEMP\\python_install.exe"
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller
        Start-Process -Wait -FilePath $pyInstaller -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1"
        Remove-Item $pyInstaller
    }}
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}}

Write-Host "[r00k] Python: $(python --version)"

# Use a dedicated venv — avoids PEP 668 and missing/broken system pip.
$venv = "$env:USERPROFILE\\.rook-band-worker\\venv"
if (-not (Test-Path "$venv\\Scripts\\python.exe")) {{
    Write-Host "[r00k] creating venv at $venv ..."
    python -m venv "$venv"
}}
$vpy = "$venv\\Scripts\\python.exe"
# pythonw.exe is the windowless interpreter — the worker runs with NO console window.
$vpyw = "$venv\\Scripts\\pythonw.exe"
if (-not (Test-Path $vpyw)) {{ $vpyw = $vpy }}  # fallback if pythonw is absent
& $vpy -m ensurepip --upgrade 2>$null
& $vpy -m pip install --quiet --upgrade pip 2>$null

# Install required dependencies into the venv (prebuilt wheels — no compiler needed)
Write-Host "[r00k] installing dependencies (pynacl aiohttp websockets)..."
& $vpy -m pip install --quiet pynacl aiohttp websockets

# Download band-worker bundle
$pyz = "$env:USERPROFILE\\.rook-band-worker\\band-worker.pyz"
New-Item -ItemType Directory -Force -Path (Split-Path $pyz) | Out-Null
Invoke-WebRequest -Uri "https://{domain}/band-worker.pyz" -OutFile $pyz

# Stop any existing worker FIRST — avoids duplicate processes and stale worker-ids
# lingering on the band (each worker process announces a fresh random id).
Write-Host "[r00k] stopping any existing band worker..."
Stop-ScheduledTask -TaskName "RookBandWorker" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object {{ $_.CommandLine -like "*band-worker.pyz*" }} |
    ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Seconds 1

# HID backend: Windows is native — hid.* uses SendInput via user32.dll (no install).
# The task runs at logon in the interactive session, so input injection works.
Write-Host "[r00k] HID backend: native Windows SendInput (no extra setup needed)."

# Register as Scheduled Task so worker restarts at every logon  (band: {band_name})
$workerArgs = "`"$pyz`" --hub {hub_public} --ws --psk {band_psk} --name $env:COMPUTERNAME --update-url https://{domain}/band-worker.json"
$action = New-ScheduledTaskAction -Execute $vpyw -Argument $workerArgs
$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "RookBandWorker" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "RookBandWorker"
Write-Host "[r00k] Band worker installed as Scheduled Task (RookBandWorker)."
'''

BASH_BOOTSTRAP = '''#!/bin/bash
set -e

# R00K Band Worker Bootstrap (Linux/Mac/Termux)

install_python() {{
    echo "[r00k] Python not found. Installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip curl
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3 python3-pip curl
    elif command -v pacman &>/dev/null; then
        sudo pacman -Sy --noconfirm python python-pip curl
    elif command -v apk &>/dev/null; then
        sudo apk add python3 py3-pip curl
    elif command -v brew &>/dev/null; then
        brew install python3
    elif command -v pkg &>/dev/null; then
        pkg install -y python curl
    else
        echo "[r00k] ERROR: No supported package manager found."
        echo "[r00k] Install Python 3 manually and re-run this script."
        exit 1
    fi
}}

install_curl() {{
    if ! command -v curl &>/dev/null; then
        echo "[r00k] curl not found. Installing..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y -qq curl
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y curl
        elif command -v pacman &>/dev/null; then
            sudo pacman -Sy --noconfirm curl
        elif command -v pkg &>/dev/null; then
            pkg install -y curl
        fi
    fi
}}

# ---- HID backend (Linux): ydotool + ydotoold so hid.* works out of the box ----
# Wayland needs ydotool (xdotool is X11-only); ydotool also covers X11. Mac/Windows
# use their own native backends; Termux/Android does not use this path.
setup_hid_linux() {{
    [ "$(uname -s)" = "Linux" ] || return 0
    case "$PREFIX" in /data/data/com.termux*) return 0 ;; esac

    if ! command -v ydotool &>/dev/null; then
        echo "[r00k] installing ydotool (HID backend)..."
        if command -v apt-get &>/dev/null; then sudo apt-get install -y -qq ydotool || true
        elif command -v dnf &>/dev/null; then sudo dnf install -y ydotool || true
        elif command -v pacman &>/dev/null; then sudo pacman -Sy --noconfirm ydotool || true
        elif command -v apk &>/dev/null; then sudo apk add ydotool || true
        else echo "[r00k] WARNING: no known package manager for ydotool; HID unavailable"; fi
    fi
    command -v ydotool &>/dev/null || {{ echo "[r00k] WARNING: ydotool missing; HID disabled"; return 0; }}

    # /dev/uinput access. Active seat sessions get an ACL automatically; the udev
    # rule + input group cover headless and post-reboot. All best-effort (sudo).
    sudo modprobe uinput 2>/dev/null || true
    if [ ! -e /etc/udev/rules.d/99-rook-uinput.rules ]; then
        echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/99-rook-uinput.rules >/dev/null 2>&1 || true
        sudo udevadm control --reload-rules 2>/dev/null || true
        sudo udevadm trigger /dev/uinput 2>/dev/null || true
    fi
    sudo usermod -aG input "$USER" 2>/dev/null || true

    # ydotoold as our own user service (portable across distro unit naming).
    if command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
        mkdir -p ~/.config/systemd/user
        cat > ~/.config/systemd/user/rook-ydotoold.service << RKYD
[Unit]
Description=ydotoold (rook HID backend daemon)

[Service]
ExecStart=$(command -v ydotoold)
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
RKYD
        systemctl --user daemon-reload
        systemctl --user enable --now rook-ydotoold.service 2>/dev/null || true
        if systemctl --user is-active --quiet rook-ydotoold.service; then
            echo "[r00k] HID backend ready (ydotool + ydotoold)."
        else
            echo "[r00k] WARNING: ydotoold inactive — HID may need a relogin for /dev/uinput access."
        fi
    fi
}}

# ---- Termux/Android: native deps + persistence helper --------------------
IS_TERMUX=0
case "$PREFIX" in /data/data/com.termux*) IS_TERMUX=1 ;; esac

setup_termux_service() {{
    # Persist via termux-services (runit). Falls back to nohup this session if
    # runsvdir isn't supervising yet (first install before a Termux restart).
    pkg install -y termux-services >/dev/null 2>&1 || true
    SVDIR="$PREFIX/var/service/rook-band-worker"
    mkdir -p "$SVDIR"
    cat > "$SVDIR/run" << TXSVC
#!$PREFIX/bin/sh
termux-wake-lock 2>/dev/null || true
exec $WORKER_CMD 2>&1
TXSVC
    chmod +x "$SVDIR/run"

    # Termux:Boot autostart (no-op unless the Termux:Boot addon is installed):
    # bring up runsvdir on boot so it supervises our service.
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/rook-band-worker" << TXBOOT
#!$PREFIX/bin/sh
termux-wake-lock 2>/dev/null || true
. $PREFIX/etc/profile.d/start-services.sh 2>/dev/null || true
TXBOOT
    chmod +x "$HOME/.termux/boot/rook-band-worker"

    if command -v sv >/dev/null 2>&1 && pgrep -x runsvdir >/dev/null 2>&1; then
        sv up rook-band-worker 2>/dev/null || true
        sleep 2
        if sv status rook-band-worker 2>/dev/null | grep -q "^run"; then
            echo "[r00k] Band worker RUNNING (termux-service: rook-band-worker)."
            return 0
        fi
    fi
    termux-wake-lock 2>/dev/null || true
    nohup $WORKER_CMD >> "$HOME/.rook-band-worker/worker.log" 2>&1 &
    sleep 2
    if kill -0 $! 2>/dev/null; then
        echo "[r00k] Band worker RUNNING (nohup PID $!). Log: ~/.rook-band-worker/worker.log"
        echo "[r00k] TIP: restart Termux once so runsvdir supervises rook-band-worker across restarts."
    else
        echo "[r00k] ERROR: worker exited immediately. Check ~/.rook-band-worker/worker.log"
        tail -n 20 "$HOME/.rook-band-worker/worker.log" 2>/dev/null
        exit 1
    fi
}}

if [ "$IS_TERMUX" = "1" ]; then
    echo "[r00k] Termux detected — installing native deps (libsodium, termux-api)..."
    pkg install -y libsodium termux-api >/dev/null 2>&1 || true
    # pynacl: link Termux's prebuilt libsodium instead of compiling the bundled
    # copy, whose configure mis-detects memset_explicit on Android API < 34.
    export SODIUM_INSTALL=system
fi

install_curl

PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    install_python
    if command -v python3 &>/dev/null; then
        PYTHON=python3
    elif command -v python &>/dev/null; then
        PYTHON=python
    else
        echo "[r00k] ERROR: Python installation failed."
        exit 1
    fi
fi

echo "[r00k] Python: $($PYTHON --version)"

# Use a dedicated venv — sidesteps PEP 668 (externally-managed), missing system pip,
# and --user path quirks. The venv always gets its own pip via ensurepip.
VENV="$HOME/.rook-band-worker/venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "[r00k] creating venv at $VENV ..."
    if ! $PYTHON -m venv "$VENV" 2>/dev/null; then
        # venv module missing — install it, then retry
        if command -v apt-get &>/dev/null; then sudo apt-get install -y -qq python3-venv || true
        elif command -v pacman &>/dev/null; then sudo pacman -Sy --noconfirm python || true
        fi
        $PYTHON -m venv "$VENV" || {{ echo "[r00k] ERROR: could not create venv"; exit 1; }}
    fi
fi
VPY="$VENV/bin/python"

# Ensure pip inside the venv (ensurepip is bundled with venv; belt-and-suspenders)
"$VPY" -m ensurepip --upgrade &>/dev/null || true
"$VPY" -m pip install --quiet --upgrade pip &>/dev/null || true

# Install required dependencies into the venv (prebuilt wheels — no compiler needed)
echo "[r00k] installing dependencies (pynacl aiohttp websockets)..."
"$VPY" -m pip install --quiet pynacl aiohttp websockets || {{
    echo "[r00k] ERROR: dependency install failed. See output above."
    exit 1
}}

# Download band-worker bundle
PYZ="$HOME/.rook-band-worker/band-worker.pyz"
mkdir -p "$HOME/.rook-band-worker"
curl -fsSL https://{domain}/band-worker.pyz -o "$PYZ"
chmod +x "$PYZ"

WORKER_NAME=$(hostname)
if [ "$IS_TERMUX" = "1" ]; then
    # Termux hostname is always "localhost" — use the device model instead.
    MODEL=$(getprop ro.product.model 2>/dev/null | tr ' ' '-')
    [ -n "$MODEL" ] && WORKER_NAME="$MODEL"
fi
# band: {band_name}
WORKER_CMD="$VPY $PYZ --hub {hub_public} --ws --psk {band_psk} --name $WORKER_NAME --update-url https://{domain}/band-worker.json"

# Swap in the new worker. CRITICAL: when this installer is launched *by the
# running worker* (a band-driven update), this script shares the worker's
# process tree / systemd cgroup. Stopping the old worker from inside this
# script (e.g. `pkill`) therefore kills THIS script mid-swap, before the new
# worker starts — stranding the host. So we never kill the old worker
# in-process: we let the service manager own the stop+start (it finishes the
# restart even if this script dies), and on no-service-manager hosts we launch
# the new worker in its OWN session before reaping the old one.
echo "[r00k] installing/replacing band worker..."

if [ "$IS_TERMUX" = "1" ]; then
    setup_termux_service
    # runit owns the swap; restarting is safe even if this script is the caller.
    sv restart rook-band-worker 2>/dev/null || sv up rook-band-worker 2>/dev/null || true
    echo "[r00k] Band worker (re)started via termux-services."
elif command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/rook-band-worker.service << ROOKSVC
[Unit]
Description=Rook Band Worker
After=network-online.target rook-ydotoold.service
Wants=network-online.target rook-ydotoold.service

[Service]
ExecStart=$WORKER_CMD
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
ROOKSVC
    systemctl --user daemon-reload
    systemctl --user enable rook-band-worker >/dev/null 2>&1 || true
    # Enable lingering so the service survives logout/reboot (best-effort)
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true
    # Hand the swap to systemd. `restart` stops the old instance and starts the
    # new one from the unit above — owned by the user manager, NOT this script —
    # so it completes even if stopping the old worker kills this caller.
    echo "[r00k] (re)starting via systemd user service..."
    systemctl --user restart rook-band-worker
    echo "[r00k] Band worker RUNNING (systemd user service: rook-band-worker)."
else
    # No service manager. Start the NEW worker in its own session FIRST (so it
    # survives even if reaping the old worker kills this script), then reap any
    # other band-worker processes except the one we just launched.
    mkdir -p ~/.rook-band-worker
    setsid bash -c "exec $WORKER_CMD >> $HOME/.rook-band-worker/worker.log 2>&1" </dev/null >/dev/null 2>&1 &
    NEWPID=$!
    sleep 2
    for pid in $(pgrep -f "band-worker.pyz" 2>/dev/null); do
        [ "$pid" = "$NEWPID" ] && continue
        kill "$pid" 2>/dev/null || true
    done
    if kill -0 "$NEWPID" 2>/dev/null; then
        echo "[r00k] Band worker RUNNING in background (PID $NEWPID, own session). Log: ~/.rook-band-worker/worker.log"
    else
        echo "[r00k] ERROR: worker exited immediately. Check: ~/.rook-band-worker/worker.log"
        tail -n 20 ~/.rook-band-worker/worker.log 2>/dev/null
        exit 1
    fi
fi

# HID backend setup runs LAST, on purpose: the worker is already on the band, so a
# slow/hung/failed ydotool install (e.g. a stalled pacman) can never strand it.
# The worker detects its backend lazily on the first hid.* call, by which time
# ydotool is installed — so no restart is needed.
setup_hid_linux
'''


# `curl -fsSL https://<host>/rook | bash` — installs the band TUI as `rook`.
# stdin is the curl pipe, so all interactive prompts read from /dev/tty.
_ROOK_INSTALLER = r'''#!/usr/bin/env bash
set -euo pipefail
BASE="${ROOK_BASE:-__BASE__}"
DEST="${ROOK_BIN:-$HOME/.local/bin}"
CONF="$HOME/.config/rook/band.conf"
PY="$(command -v python3 || command -v python || true)"
[ -z "$PY" ] && { echo "[rook] python3 is required" >&2; exit 1; }
mkdir -p "$DEST" "$(dirname "$CONF")"
echo "[rook] installing TUI -> $DEST/rook"
curl -fsSL "$BASE/rook.py" -o "$DEST/rook"
chmod +x "$DEST/rook"
if [ -r /dev/tty ]; then
  DEFU="bake"
  printf "[rook] dashboard user [%s]: " "$DEFU" > /dev/tty
  read -r WU < /dev/tty || true; WU="${WU:-$DEFU}"
  printf "[rook] dashboard password: " > /dev/tty
  read -rs WP < /dev/tty || true; printf "\n" > /dev/tty
  if [ -n "${WP:-}" ]; then
    ( umask 077; printf '# rook band connection (chmod 600)\nurl=%s\nuser=%s\npass=%s\n' "$BASE" "$WU" "$WP" > "$CONF" )
    chmod 600 "$CONF"
    echo "[rook] saved login -> $CONF"
  fi
fi
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "[rook] NOTE: $DEST is not on your PATH. Add it, e.g.:";
     echo "        echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac
echo "[rook] done — launching (next time just run: rook)"
if [ -r /dev/tty ]; then exec "$DEST/rook" < /dev/tty; else echo "[rook] run: rook"; fi
'''


class CombinedServer:
    """Single-port server: HTTP for bootstrap + WebSocket for worker connections."""

    def __init__(self, port: int = 7005, auth_token: str = "", domain: str = "hub.example.com",
                 web_user: str = "", web_pass: str = "",
                 band_psk: str = "",
                 hub_host: str = "127.0.0.1", hub_port: int = 7474,
                 hub_public: str = "hub.example.com:443", band_name: str = "rook-band"):
        self.port = port
        self.auth_token = auth_token
        self.domain = domain
        self.web_user = web_user
        self.web_pass = web_pass
        self.band_psk = band_psk
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.hub_public = hub_public  # host:port workers call back to (templated into installers)
        self.band_name = band_name    # cosmetic band identity, shown in dashboard + installer

        # First-run setup wizard values (data/setup.json, gitignored) win over
        # the constructor/config defaults so secrets never live in version control.
        from . import setup_store
        _s = setup_store.load()
        if _s.get("band_psk"):
            self.band_psk = _s["band_psk"]
        if _s.get("hub_public"):
            self.hub_public = _s["hub_public"]
        if _s.get("pyz_domain"):
            self.domain = _s["pyz_domain"]
        if _s.get("band_name"):
            self.band_name = _s["band_name"]
        self._band = None  # MultiBandClient — joins the hub to track workers + invoke caps
        self._push_task = None  # background: push signed manifest to behind workers
        # Known bands for the dashboard selector. PSKs stay server-side; the UI
        # only ever sees the band_id label (first 8 hex of SHA256(PSK)[:16]).
        self._bands = setup_store.load_bands()
        self._bans = setup_store.load_bans()   # deauthed workers (by name / worker_id)
        self._band_names: dict[str, str] = {}  # band_id label -> friendly name
        self._primary_label = ""               # the configured band; not removable
        try:
            from telesthete.protocol.crypto import derive_band_id
            for _b in self._bands:
                _lbl = derive_band_id(_b["psk"]).hex()[:8]
                self._band_names.setdefault(_lbl, _b["name"])
            if self.band_psk:
                self._primary_label = derive_band_id(self.band_psk).hex()[:8]
        except Exception:
            log.warning("could not derive band labels; selector names may be blank")
        self._workers: dict[str, RemoteWorker] = {}
        self._on_worker_connect = None
        self._on_worker_disconnect = None
        self._on_worker_chat = None  # async callback(worker_name, content, worker_id) -> response
        self._app = web.Application(middlewares=[self._basic_auth_middleware])
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/worker", self._worker_bootstrap)
        self._app.router.add_get("/worker.py", self._worker_script)
        self._app.router.add_get("/band-worker.pyz", self._band_worker_pyz)
        self._app.router.add_get("/band-worker.json", self._band_worker_manifest)
        self._app.router.add_get("/apk", self._worker_apk)
        self._app.router.add_get("/rook", self._band_installer)  # curl | bash installer for the TUI
        self._app.router.add_get("/rook.py", self._band_cli)     # the standalone TUI script itself
        self._app.router.add_get("/api/bands", self._api_bands)
        self._app.router.add_post("/api/bands", self._api_add_band)
        self._app.router.add_delete("/api/bands/{id}", self._api_remove_band)
        self._app.router.add_get("/api/band/workers", self._api_band_workers)
        self._app.router.add_post("/api/band/call", self._api_band_call)
        self._app.router.add_get("/api/band/bans", self._api_bans)
        self._app.router.add_post("/api/band/ban", self._api_ban)
        self._app.router.add_post("/api/band/unban", self._api_unban)
        self._app.router.add_get("/ws", self._websocket_handler)
        # Auth routes (handled by middleware, these are just route stubs)
        async def _noop(r): return web.Response(text="")
        self._app.router.add_get("/login", _noop)
        self._app.router.add_post("/login", _noop)
        self._app.router.add_get("/logout", _noop)
        self._app.router.add_get("/setup", self._setup_page)
        self._app.router.add_post("/setup", self._setup_submit)
        self._app.router.add_get("/health", self._health)
        self._runner: web.AppRunner | None = None

        # Register web UI routes before server starts
        try:
            from ..modules.web_ui import register_routes
            register_routes(self._app)
        except Exception as e:
            log.warning("Web UI routes not registered: %s", e)

    def _make_session_cookie(self) -> str:
        """Derive the session token from the dashboard credentials."""
        import hashlib
        return hashlib.sha256(f"{self.web_user}:{self.web_pass}:r00k-dash".encode()).hexdigest()[:32]

    def _check_creds(self, user: str, passwd: str) -> bool:
        """Validate login. Username is enforced only when one is configured,
        so a bare password still works for password-only setups."""
        if self.web_user:
            return user == self.web_user and passwd == self.web_pass
        return passwd == self.web_pass

    @web.middleware
    async def _basic_auth_middleware(self, request: web.Request, handler):
        """Password-only gate, mirroring the MCP /tokens endpoint.

        A single shared password (``web_pass``) unlocks the dashboard. Once
        entered it sets a session cookie; headless callers can pass the
        password via HTTP Basic auth (in either the user or pass field).
        Installer/health endpoints stay public. Auth is off when no password
        is configured.
        """
        import base64
        from . import setup_store

        # Always public: worker bootstrap/artifacts, health, websockets.
        exempt = ("/ws", "/health", "/worker", "/worker.py", "/band-worker.pyz",
                  "/band-worker.json", "/apk", "/rook", "/rook.py")
        is_exempt = request.path == "/ws/ui" or any(
            request.path == p or request.path.startswith(p + "/") for p in exempt)

        # First-run gate: until the band has a PSK + public hub address, force the
        # setup wizard. Runs before the password shortcut so an unconfigured,
        # password-less hub still can't be used until it's set up.
        if not setup_store.is_configured():
            if request.path == "/setup":
                return await handler(request)
            if is_exempt:
                return await handler(request)
            if "text/html" in request.headers.get("Accept", ""):
                raise web.HTTPFound("/setup")
            return web.Response(status=503, text="rook hub not configured — open /setup in a browser")

        if is_exempt or not self.web_pass:
            return await handler(request)

        # Login endpoint — username + password.
        if request.path == "/login" and request.method == "POST":
            try:
                data = await request.post()
                if self._check_creds(data.get("user", ""), data.get("pass", "")):
                    resp = web.HTTPFound("/")
                    resp.set_cookie("rook_session", self._make_session_cookie(),
                                    max_age=30 * 86400, httponly=True, samesite="Lax")
                    raise resp
            except web.HTTPFound:
                raise
            except Exception:
                pass
            return web.Response(text=self._login_page("Invalid credentials"), content_type="text/html")

        if request.path == "/login":
            return web.Response(text=self._login_page(), content_type="text/html")

        if request.path == "/logout":
            resp = web.HTTPFound("/login")
            resp.del_cookie("rook_session")
            raise resp

        # Check session cookie
        session = request.cookies.get("rook_session", "")
        if session == self._make_session_cookie():
            return await handler(request)

        # Check basic auth (for API/curl): password in either field.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user, _, passwd = decoded.partition(":")
                if self._check_creds(user, passwd):
                    resp = await handler(request)
                    resp.set_cookie("rook_session", self._make_session_cookie(),
                                    max_age=30 * 86400, httponly=True, samesite="Lax")
                    return resp
            except Exception:
                pass

        # No valid auth — redirect to login page for browsers, 401 for API
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise web.HTTPFound("/login")

        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="r00k"'},
            text="Unauthorized",
        )

    def _login_page(self, error: str = "") -> str:
        err = f'<div class="err">{error}</div>' if error else ""
        return f"""<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>♖ ROOK</title>
<style>
body{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0b0f14;color:#e6edf3;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.box{{background:#11161d;border:1px solid #222c38;padding:32px;border-radius:10px;width:300px;box-shadow:0 8px 40px rgba(0,0,0,.4)}}
h1{{font-size:1.4rem;margin:0 0 4px;color:#2dd4a7}}
p{{color:#7d8896;font-size:.8rem;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:10px;margin:0 0 12px;background:#0b0f14;color:#e6edf3;border:1px solid #222c38;border-radius:6px;font-family:inherit;font-size:.95rem}}
button{{width:100%;padding:10px;background:#1a9e7a;color:#fff;border:0;border-radius:6px;cursor:pointer;font-family:inherit;font-size:.95rem}}
button:hover{{background:#22b88f}}
.err{{color:#f76a6a;font-size:.8rem;margin-bottom:10px}}
</style></head>
<body><div class="box">
<h1>♖ ROOK</h1>
<p>Band control panel. Sign in to continue.</p>
{err}
<form method="POST" action="/login">
<input name="user" placeholder="Username" autofocus required autocomplete="username">
<input name="pass" type="password" placeholder="Password" required autocomplete="current-password">
<button type="submit">Sign in</button>
</form>
</div></body></html>"""

    def _setup_page_html(self, error: str = "") -> str:
        from . import setup_store
        import html as _html
        s = setup_store.load()
        band_name = _html.escape(s.get("band_name") or self.band_name)
        hub_public = _html.escape(s.get("hub_public") or "")
        pyz_domain = _html.escape(s.get("pyz_domain") or "")
        band_psk = _html.escape(s.get("band_psk") or setup_store.gen_psk())
        err = f'<div class="err">{_html.escape(error)}</div>' if error else ""
        return f"""<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>♖ ROOK — setup</title>
<style>
body{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0b0f14;color:#e6edf3;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.box{{background:#11161d;border:1px solid #222c38;padding:32px;border-radius:10px;width:420px;max-width:92vw;box-shadow:0 8px 40px rgba(0,0,0,.4)}}
h1{{font-size:1.4rem;margin:0 0 4px;color:#2dd4a7}}
p{{color:#7d8896;font-size:.8rem;margin:0 0 18px}}
label{{display:block;font-size:.75rem;color:#9aa7b4;margin:14px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:10px;background:#0b0f14;color:#e6edf3;border:1px solid #222c38;border-radius:6px;font-family:inherit;font-size:.95rem}}
small{{display:block;color:#5d6773;font-size:.68rem;margin-top:4px}}
button{{width:100%;padding:11px;margin-top:20px;background:#1a9e7a;color:#fff;border:0;border-radius:6px;cursor:pointer;font-family:inherit;font-size:.95rem}}
button:hover{{background:#22b88f}}
.err{{color:#f76a6a;font-size:.8rem;margin-bottom:10px}}
</style></head>
<body><div class="box">
<h1>♖ ROOK — first-run setup</h1>
<p>Configure the band this hub serves. Saved to <code>data/setup.json</code> (never committed).</p>
{err}
<form method="POST" action="/setup">
<label>Band name</label>
<input name="band_name" value="{band_name}" placeholder="my-band" autofocus>
<small>Cosmetic label for this band, shown in the dashboard + installer.</small>
<label>Public hub address</label>
<input name="hub_public" value="{hub_public}" placeholder="hub.mydomain.com:443" required>
<small>host:port workers connect back to (the band WS endpoint).</small>
<label>Installer download domain</label>
<input name="pyz_domain" value="{pyz_domain}" placeholder="hub.mydomain.com" required>
<small>Domain that serves <code>band-worker.pyz</code> in the one-line installer.</small>
<label>Band PSK</label>
<input name="band_psk" value="{band_psk}" required>
<small>Pre-shared key defining band identity. Keep it secret; change it to rotate.</small>
<button type="submit">Save &amp; activate band</button>
</form>
</div></body></html>"""

    async def _setup_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._setup_page_html(), content_type="text/html")

    async def _setup_submit(self, request: web.Request) -> web.Response:
        from . import setup_store
        data = await request.post()
        vals = {
            "band_name": (data.get("band_name") or "").strip() or "rook-band",
            "hub_public": (data.get("hub_public") or "").strip(),
            "pyz_domain": (data.get("pyz_domain") or "").strip(),
            "band_psk": (data.get("band_psk") or "").strip(),
        }
        if not vals["hub_public"] or not vals["band_psk"] or not vals["pyz_domain"]:
            return web.Response(
                text=self._setup_page_html("Public hub address, installer domain, and band PSK are all required."),
                content_type="text/html",
            )
        saved = setup_store.save(vals)
        # Apply live so the installer one-liners reflect the new band immediately.
        self.band_name = saved["band_name"]
        self.band_psk = saved["band_psk"]
        self.hub_public = saved["hub_public"]
        self.domain = saved["pyz_domain"]
        log.info("Setup wizard: band '%s' configured (hub=%s)", self.band_name, self.hub_public)
        raise web.HTTPFound("/")

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Remote server on port %d (HTTP + WebSocket)", self.port)

        # Join the band so the dashboard can list workers + invoke caps.
        # Best-effort: a missing/unreachable hub must not take down the
        # installer server (the bootstrap endpoints don't need the band).
        try:
            from ..band_mcp.client import MultiBandClient
            psks = [b["psk"] for b in self._bands] or [self.band_psk]
            self._band = MultiBandClient(psks=psks, hub_host=self.hub_host,
                                         hub_port=self.hub_port)
            await self._band.start()
            log.info("band client joined hub %s:%d (%d band(s))",
                     self.hub_host, self.hub_port, len(psks))
            if os.environ.get("ROOK_PUSH_UPDATES", "1") != "0":
                self._push_task = asyncio.create_task(self._push_loop())
        except Exception as e:
            log.warning("band client failed to start (%s); dashboard band view disabled", e)
            self._band = None

    async def _push_loop(self) -> None:
        """Auto-converge: push the current signed manifest to apply-capable
        workers reporting an older build. Fail-closed — no-op until a *signed*
        manifest exists, and it only targets workers advertising worker.apply."""
        manifest_path = Path(__file__).parent / "band-worker.json"
        pushed: dict[str, tuple] = {}      # worker_id -> (target_build, ts)
        REPUSH_SECS = 180.0                # don't re-push the same build too soon
        while True:
            try:
                await asyncio.sleep(20.0)
                if self._band is None:
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text())
                except Exception:
                    continue
                if not manifest.get("sig"):
                    continue  # unsigned -> workers would reject; don't bother
                target = int(manifest.get("build", 0))
                if target <= 0:
                    continue
                now = time.time()
                for wid, w in list(self._band.workers.items()):
                    if self._ban_match(w.get("name"), wid):
                        continue  # deauthed — never feed a banned node updates
                    if "worker.apply" not in (w.get("caps") or []):
                        continue  # can't receive an in-band push yet
                    b = w.get("build")
                    if not isinstance(b, int) or b >= target:
                        continue
                    last = pushed.get(wid)
                    if last and last[0] == target and (now - last[1]) < REPUSH_SECS:
                        continue
                    pushed[wid] = (target, now)
                    name = w.get("name", wid)
                    log.info("push update %s: build %s -> %s", name, b, target)
                    try:
                        reply = await self._band.call(
                            "worker.apply", args={"manifest": manifest},
                            target=wid, timeout=30)
                        log.info("push %s reply: %s", name, reply.get("result", reply))
                    except Exception as e:
                        log.warning("push to %s failed: %s", name, e)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("push loop error")

    async def stop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None
        if self._band is not None:
            try:
                await self._band.stop()
            except Exception:
                log.exception("band client stop failed")
            self._band = None
        if self._runner:
            await self._runner.cleanup()

    # -- HTTP endpoints --

    async def _index(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "").lower()
        kw = {"token": self.auth_token, "domain": self.domain}

        # Only auto-serve bootstrap at /worker, not /
        # / always shows the help page

        # Browser — serve dashboard directly
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            try:
                from ..modules.web_ui import WEB_DIR
                index_path = WEB_DIR / "index.html"
                if index_path.exists():
                    return web.Response(text=index_path.read_text(encoding="utf-8"), content_type="text/html")
            except Exception:
                pass

        # CLI — show instructions
        text = f"""
  R ☠ ☠ K  Band Worker Installer
  =================================

  Linux / Mac:
    curl -fsSL https://{self.domain}/worker | bash

  Windows (PowerShell, on Windows only):
    iex (irm https://{self.domain}/worker)

  Force a variant (if auto-detect guesses wrong):
    curl -fsSL "https://{self.domain}/worker?os=unix" | bash
    iex (irm "https://{self.domain}/worker?os=windows")

  Endpoints:
    /worker           bootstrap script (auto-detects OS; ?os=windows|unix to force)
    /band-worker.pyz  self-contained band-worker zipapp
    /worker.py        legacy exec-worker script
    /ws               legacy websocket endpoint
    /health           server status
"""
        return web.Response(text=text, content_type="text/plain")

    async def _worker_bootstrap(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "").lower()
        kw = {"domain": self.domain, "band_psk": self.band_psk,
              "hub_public": self.hub_public, "band_name": self.band_name}
        # Explicit override wins, for determinism: /worker?os=windows|unix
        os_q = request.query.get("os", "").lower()
        if os_q in ("windows", "win"):
            want_ps = True
        elif os_q in ("unix", "linux", "mac", "macos", "darwin", "posix"):
            want_ps = False
        else:
            # PowerShell is cross-platform (pwsh on Linux/macOS), so "powershell"
            # in the UA alone is NOT enough — only serve the Windows installer
            # when the client is actually running on Windows. Everything else
            # (curl, wget, browsers, pwsh-on-Linux) gets the bash script.
            is_ps = ("powershell" in ua) or ("pwsh" in ua)
            on_windows = ("windows" in ua) or ("win32" in ua) or ("win64" in ua)
            want_ps = is_ps and on_windows
        if want_ps:
            return web.Response(text=PS_BOOTSTRAP.format(**kw), content_type="text/plain")
        return web.Response(text=BASH_BOOTSTRAP.format(**kw), content_type="text/plain")

    async def _worker_script(self, request: web.Request) -> web.Response:
        return web.Response(text=WORKER_SCRIPT, content_type="text/plain")

    async def _band_worker_pyz(self, request: web.Request) -> web.Response:
        pyz_path = Path(__file__).parent / "band-worker.pyz"
        if not pyz_path.exists():
            return web.Response(
                status=404,
                text="band-worker.pyz not built yet. Run: python3 rook/remote/build_band_worker.py",
            )
        data = pyz_path.read_bytes()
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=band-worker.pyz"},
        )

    async def _band_worker_manifest(self, request: web.Request) -> web.Response:
        """Serve the signed OTA manifest (band-worker.json) next to the pyz.
        Public + no-cache so converging workers always see the current build."""
        manifest_path = Path(__file__).parent / "band-worker.json"
        if not manifest_path.exists():
            return web.Response(
                status=404,
                text="band-worker.json not built yet. Run: python3 rook/remote/build_band_worker.py",
            )
        return web.Response(
            body=manifest_path.read_bytes(),
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    async def _worker_apk(self, request: web.Request) -> web.Response:
        """Serve the native Android worker APK (built by android/, dropped here)."""
        apk_path = Path(__file__).parent / "rook-worker.apk"
        if not apk_path.exists():
            return web.Response(
                status=404,
                text="rook-worker.apk not built yet. Build android/ and drop the APK here.",
            )
        return web.FileResponse(
            apk_path,
            headers={
                "Content-Disposition": "attachment; filename=rook-worker.apk",
                "Content-Type": "application/vnd.android.package-archive",
            },
        )

    async def _api_bands(self, request: web.Request) -> web.Response:
        """Known bands for the dashboard selector: ``[{id, name}]`` where ``id``
        is the band_id label. Raw PSKs are never sent to the browser."""
        out = [{"id": lbl, "name": name, "primary": lbl == self._primary_label}
               for lbl, name in self._band_names.items()]
        out.sort(key=lambda x: x["name"])
        return web.json_response(out)

    async def _api_add_band(self, request: web.Request) -> web.Response:
        """Add a band by PSK (authenticated dashboard users only). The PSK is
        persisted server-side in setup.json (gitignored) and never echoed back;
        the band is joined live so its workers appear without a restart."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        name = (data.get("name") or "").strip() or "band"
        psk = (data.get("psk") or "").strip()
        if not psk:
            return web.json_response({"error": "psk required"}, status=400)
        try:
            from telesthete.protocol.crypto import derive_band_id
            label = derive_band_id(psk).hex()[:8]
        except Exception as e:
            return web.json_response({"error": f"crypto unavailable: {e}"}, status=500)

        from . import setup_store
        # Persist as an extra band (primary stays derived from band_psk). Dedupe.
        extras = [b for b in setup_store.load_bands() if b["psk"] != self.band_psk]
        if psk != self.band_psk and all(b["psk"] != psk for b in extras):
            extras.append({"name": name, "psk": psk})
            setup_store.save_bands(extras)
        self._band_names[label] = name
        self._bands = setup_store.load_bands()
        try:
            await self._band.add_band(psk)
        except Exception as e:
            log.warning("live join of band %s failed: %s", label, e)
        return web.json_response({"id": label, "name": name})

    async def _api_remove_band(self, request: web.Request) -> web.Response:
        """Remove a band by band_id label. The primary (configured) band can't
        be removed — that's the dashboard's own band."""
        label = request.match_info.get("id", "")
        if not label:
            return web.json_response({"error": "id required"}, status=400)
        if label == self._primary_label:
            return web.json_response({"error": "cannot remove the primary band"}, status=400)
        from . import setup_store
        try:
            from telesthete.protocol.crypto import derive_band_id
            extras = [b for b in setup_store.load_bands()
                      if b["psk"] != self.band_psk
                      and derive_band_id(b["psk"]).hex()[:8] != label]
        except Exception as e:
            return web.json_response({"error": f"crypto unavailable: {e}"}, status=500)
        setup_store.save_bands(extras)
        self._band_names.pop(label, None)
        if self._band is not None:
            try:
                await self._band.remove_band(label)
            except Exception as e:
                log.warning("live leave of band %s failed: %s", label, e)
        return web.json_response({"removed": label})

    async def _api_band_workers(self, request: web.Request) -> web.Response:
        """Live band roster from our hub-joined MultiBandClient. Each worker is
        tagged with the ``band`` (band_id label) it was seen on; an optional
        ``?band=<id>`` query filters to one band."""
        import time
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        want = request.query.get("band") or ""
        now = time.time()
        out = []
        for w in self._band.workers.values():
            band = w.get("band")
            if want and band != want:
                continue
            out.append({
                "worker_id": w["worker_id"],
                "name": w.get("name"),
                "caps": w.get("caps", []),
                "plugins": w.get("plugins", []),
                "band": band,
                "version": w.get("version"),
                "build": w.get("build"),
                "last_seen_age_secs": round(now - w.get("last_seen", 0.0), 2),
                # deauthed nodes normally park off-band and vanish from the roster;
                # flag any still lingering (not yet parked / uncooperative).
                "banned": self._ban_match(w.get("name"), w.get("worker_id")),
            })
        out.sort(key=lambda x: x["name"] or "")
        return web.json_response(out)

    async def _api_band_call(self, request: web.Request) -> web.Response:
        """Invoke a capability on the band and return the worker's reply."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        cap = data.get("cap")
        if not cap:
            return web.json_response({"error": "cap required"}, status=400)
        args = data.get("args") or {}
        target = data.get("worker_id") or data.get("target")
        # Refuse to drive a deauthed worker (its name may persist across restarts).
        if target:
            tw = self._band.workers.get(target) if self._band else None
            if self._ban_match(tw.get("name") if tw else None, target):
                return web.json_response(
                    {"ok": False, "error": "worker is deauthed (banned)"}, status=403)
        try:
            timeout = float(data.get("timeout", 15.0))
        except (TypeError, ValueError):
            timeout = 15.0
        try:
            reply = await self._band.call(cap=cap, args=args, target=target, timeout=timeout)
            return web.json_response(reply)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "timeout waiting for reply"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)

    # -- deauth / ban --------------------------------------------------------

    def _ban_match(self, name: str | None, worker_id: str | None) -> bool:
        """True if a worker with this (name, worker_id) is on the deauth list.
        Name is the durable match (survives a worker restart); worker_id is exact."""
        for b in self._bans:
            if worker_id and b.get("worker_id") and b["worker_id"] == worker_id:
                return True
            if name and b.get("name") and b["name"] == name:
                return True
        return False

    def _sign_deauth(self, worker_id: str, name: str, reason: str) -> dict | None:
        """An ed25519-signed worker.deauth payload, signed with the same OTA
        key. None if this host holds no signing key (then it's denylist-only)."""
        from .update_keys import load_signing_key, _canonical_payload
        import base64
        sk = load_signing_key()
        if sk is None:
            return None
        body = {"worker_id": worker_id, "name": name,
                "issued_at": int(time.time()), "reason": reason[:500]}
        sig = sk.sign(_canonical_payload(body)).signature
        return {**body, "sig": base64.b64encode(sig).decode("ascii")}

    async def _api_bans(self, request: web.Request) -> web.Response:
        """The current deauth list (for the dashboard 'Banned' view)."""
        return web.json_response(self._bans)

    async def _api_ban(self, request: web.Request) -> web.Response:
        """Deauth a worker: add it to the persistent controller denylist (hidden,
        no OTA pushes, calls refused) AND send it a signed worker.deauth so a
        cooperative node parks itself off-band. Body: {worker_id?, name?, reason?}."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        worker_id = str(data.get("worker_id") or "").strip()
        name = str(data.get("name") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if not worker_id and not name:
            return web.json_response({"error": "worker_id or name required"}, status=400)
        # Fill in whichever of (name, worker_id) is missing from the live roster.
        live = self._band.workers.get(worker_id) if worker_id else None
        if live is None and name:
            for w in self._band.workers.values():
                if w.get("name") == name:
                    live = w
                    break
        if live is not None:
            name = name or (live.get("name") or "")
            worker_id = worker_id or (live.get("worker_id") or "")

        # 1) Denylist first — always works, so even if the in-band deauth misses,
        #    the worker is immediately hidden, cut off from calls, and no longer
        #    receives OTA pushes.
        if not self._ban_match(name or None, worker_id or None):
            self._bans.append({"name": name, "worker_id": worker_id,
                               "at": int(time.time()), "reason": reason})
            try:
                setup_store.save_bans(self._bans)
            except Exception:
                log.exception("failed to persist ban list")

        # 2) Best-effort signed deauth so a cooperative worker parks off-band.
        deauth = self._sign_deauth(worker_id, name, reason)
        if deauth is None:
            sent = {"ok": False, "error": "no signing key on host — controller denylist only"}
            log.warning("ban %s: no signing key; denylist only", name or worker_id)
        elif worker_id:
            try:
                sent = await self._band.call("worker.deauth", args={"payload": deauth},
                                             target=worker_id, timeout=20)
            except Exception as e:
                sent = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            sent = {"ok": False, "error": "worker offline — denylisted, will park if it returns"}
        return web.json_response({"ok": True,
                                  "banned": {"name": name, "worker_id": worker_id},
                                  "deauth_sent": sent})

    async def _api_unban(self, request: web.Request) -> web.Response:
        """Remove a worker from the controller denylist. NOTE: a cooperative node
        that already parked itself off-band must be revived locally (clear
        ~/.rook-band-worker/banned + restart) — the controller can't reach a
        dormant node in-band. Body: {worker_id?, name?}."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        worker_id = str(data.get("worker_id") or "").strip()
        name = str(data.get("name") or "").strip()
        before = len(self._bans)
        self._bans = [b for b in self._bans
                      if not ((worker_id and b.get("worker_id") == worker_id)
                              or (name and b.get("name") == name))]
        try:
            setup_store.save_bans(self._bans)
        except Exception:
            log.exception("failed to persist ban list")
        return web.json_response({"ok": True, "removed": before - len(self._bans),
                                  "note": "revive a dormant node locally to rejoin"})

    async def _band_cli(self, request: web.Request) -> web.Response:
        """Serve the `rook band` terminal control panel as a standalone script
        (the payload the /rook installer downloads)."""
        p = Path(__file__).resolve().parents[1] / "cli" / "band_tui.py"
        try:
            text = p.read_text()
        except Exception:
            return web.Response(status=404, text="# band cli unavailable\n")
        if not text.startswith("#!"):
            text = "#!/usr/bin/env python3\n" + text
        return web.Response(text=text, content_type="text/x-python",
                            headers={"Content-Disposition": 'inline; filename="rook"'})

    async def _band_installer(self, request: web.Request) -> web.Response:
        """`curl -fsSL https://<host>/rook | bash` — install the TUI as `rook`,
        ask for the dashboard login (from the terminal, since stdin is the pipe),
        save it, and launch."""
        base = f"https://{self.domain}"
        script = _ROOK_INSTALLER.replace("__BASE__", base)
        return web.Response(text=script, content_type="text/x-shellscript",
                            headers={"Content-Disposition": 'inline; filename="rook-install.sh"'})

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text=json.dumps({
            "status": "ok",
            "workers": len(self._workers),
            "band_workers": len(self._band.workers) if self._band else 0,
        }), content_type="application/json")

    # -- WebSocket endpoint --

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, receive_timeout=300)
        await ws.prepare(request)

        worker = None
        try:
            # First message must be registration
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)

            if msg.get("type") != "register":
                await ws.close(message=b"Expected registration")
                return ws

            if self.auth_token and msg.get("token") != self.auth_token:
                await ws.close(message=b"Invalid token")
                log.warning("Worker rejected: bad token from %s", request.remote)
                return ws

            worker_id = str(uuid.uuid4())[:8]
            # Create a wrapper that adapts aiohttp WS to our RemoteWorker interface
            worker = AioHttpWorker(
                id=worker_id,
                ws=ws,
                name=msg.get("name", "unnamed"),
                platform=msg.get("platform", "unknown"),
                hostname=msg.get("hostname", "unknown"),
            )
            self._workers[worker_id] = worker
            log.info("Worker connected: [%s] %s (%s/%s)", worker_id, worker.name, worker.platform, worker.hostname)

            await ws.send_json({"type": "registered", "id": worker_id})

            # Register as communication channel
            if self._on_worker_connect:
                try:
                    self._on_worker_connect(worker.name, worker.platform, worker.hostname, worker_id)
                except Exception as e:
                    log.error("Worker connect callback failed: %s", e)

            # Listen for responses
            async for raw_msg in ws:
                if raw_msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(raw_msg.data)
                        if data.get("type") == "result":
                            worker.handle_response(data)
                        elif data.get("type") == "heartbeat":
                            worker.last_active = time.time()
                        elif data.get("type") == "chat":
                            content = data.get("content", "")
                            if content and self._on_worker_chat:
                                async def _handle_chat(ws_ref, w_name, msg, w_id):
                                    try:
                                        response = await self._on_worker_chat(w_name, msg, w_id)
                                        await ws_ref.send_json({
                                            "type": "chat_response",
                                            "content": response,
                                        })
                                    except Exception as e:
                                        await ws_ref.send_json({
                                            "type": "chat_response",
                                            "content": f"Error: {e}",
                                        })
                                asyncio.create_task(_handle_chat(ws, worker.name, content, worker.id))
                    except json.JSONDecodeError:
                        pass
                elif raw_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break

        except (asyncio.TimeoutError, Exception) as e:
            log.info("Worker disconnected: %s (%s)", worker.name if worker else "unknown", e)
        finally:
            if worker:
                for future in worker._pending.values():
                    if not future.done():
                        future.set_result({"stdout": "", "stderr": "Worker disconnected", "returncode": -1})
                self._workers.pop(worker.id, None)
                log.info("Worker removed: [%s] %s", worker.id, worker.name)
                if self._on_worker_disconnect:
                    try:
                        self._on_worker_disconnect(worker.name, worker.id)
                    except Exception as e:
                        log.error("Worker disconnect callback failed: %s", e)

        return ws

    # -- Public API (used by tools) --

    def get_worker(self, name: str) -> AioHttpWorker | None:
        """Find worker by name or ID. Prefers alive connections."""
        matches = []
        for w in self._workers.values():
            if w.name == name or w.id == name:
                matches.append(w)
        if not matches:
            return None
        # Prefer alive workers
        alive = [w for w in matches if not w.ws.closed]
        return alive[0] if alive else matches[0]

    def list_workers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": w.id,
                "name": w.name,
                "platform": w.platform,
                "hostname": w.hostname,
                "alive": not w.ws.closed,
                "connected": f"{time.time() - w.connected_at:.0f}s ago",
                "last_active": f"{time.time() - w.last_active:.0f}s ago",
            }
            for w in self._workers.values()
        ]


class AioHttpWorker:
    """Worker wrapper using aiohttp WebSocket."""

    def __init__(self, id: str, ws: web.WebSocketResponse, name: str, platform: str, hostname: str):
        self.id = id
        self.ws = ws
        self.name = name
        self.platform = platform
        self.hostname = hostname
        self.connected_at = time.time()
        self.last_active = time.time()
        self._pending: dict[str, asyncio.Future] = {}

    async def execute(self, command: str, timeout: float = 60) -> dict[str, Any]:
        req_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self.ws.send_json({
            "type": "exec",
            "id": req_id,
            "command": command,
        })
        self.last_active = time.time()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"stdout": "", "stderr": "Command timed out", "returncode": -1}

    def handle_response(self, data: dict) -> None:
        req_id = data.get("id", "")
        future = self._pending.pop(req_id, None)
        if future and not future.done():
            future.set_result(data)

    async def update(self, new_script: str, timeout: float = 30) -> dict[str, Any]:
        """Send updated worker script to the worker."""
        req_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self.ws.send_json({
            "type": "update",
            "id": req_id,
            "script": new_script,
        })

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"stdout": "", "stderr": "Update timed out", "returncode": -1}

    async def uninstall(self, timeout: float = 30) -> dict[str, Any]:
        """Send uninstall command to the worker."""
        req_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self.ws.send_json({
            "type": "uninstall",
            "id": req_id,
        })

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"stdout": "", "stderr": "Uninstall timed out", "returncode": -1}


def _cli_main() -> None:
    """Argparse entry point: python -m rook.remote.bootstrap [options]."""
    import argparse

    ap = argparse.ArgumentParser(description="Rook band-worker installer server")
    ap.add_argument("--port", type=int, default=7005, help="HTTP listen port")
    ap.add_argument("--domain", default="hub.example.com", help="Public domain for installer URLs")
    ap.add_argument("--psk", default="",
                    dest="band_psk", help="Band pre-shared key embedded in bootstrap scripts "
                                          "(blank = configure via the /setup wizard)")
    ap.add_argument("--hub-public", default="hub.example.com:443",
                    help="host:port workers call back to (templated into installers)")
    ap.add_argument("--band-name", default="rook-band", help="Cosmetic band label")
    ap.add_argument("--token", default="", help="Legacy exec-worker auth token")
    ap.add_argument("--web-user", default="", help="(legacy, unused) Web UI username")
    ap.add_argument("--web-pass", default="", help="Dashboard admin password (empty = no auth)")
    ap.add_argument("--hub-host", default="127.0.0.1", help="Telesthete hub host for the dashboard band client")
    ap.add_argument("--hub-port", type=int, default=7474, help="Telesthete hub port")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    server = CombinedServer(
        port=args.port,
        domain=args.domain,
        band_psk=args.band_psk,
        auth_token=args.token,
        web_user=args.web_user,
        web_pass=args.web_pass,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        hub_public=args.hub_public,
        band_name=args.band_name,
    )

    async def _run() -> None:
        await server.start()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await server.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    _cli_main()
