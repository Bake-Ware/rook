"""Combined HTTP + WebSocket server for remote workers on a single port."""

from __future__ import annotations

import asyncio
import json
import logging
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

# Register as Scheduled Task so worker restarts at every logon
$workerArgs = "`"$pyz`" --hub mcp.bakeforge.com:443 --ws --psk {band_psk} --name $env:COMPUTERNAME"
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
WORKER_CMD="$VPY $PYZ --hub mcp.bakeforge.com:443 --ws --psk {band_psk} --name $WORKER_NAME"

# Stop any existing worker FIRST — avoids duplicate processes and stale worker-ids
# lingering on the band (each worker process announces a fresh random id).
echo "[r00k] stopping any existing band worker..."
if command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user stop rook-band-worker 2>/dev/null || true
fi
# kill stray nohup/foreground worker processes (any install mode)
pkill -f "band-worker.pyz" 2>/dev/null || true
sleep 1

# Install as systemd --user service for persistence across reboots
if command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
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
    systemctl --user enable --now rook-band-worker
    # Enable lingering so the service survives logout/reboot (best-effort)
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true
    sleep 2
    if systemctl --user is-active --quiet rook-band-worker; then
        echo "[r00k] Band worker RUNNING (systemd user service: rook-band-worker)."
    else
        echo "[r00k] WARNING: systemd service didn't start — falling back to nohup."
        nohup $WORKER_CMD >> ~/.rook-band-worker/worker.log 2>&1 &
        echo "[r00k] Band worker started in background (PID $!). Log: ~/.rook-band-worker/worker.log"
    fi
else
    nohup $WORKER_CMD >> ~/.rook-band-worker/worker.log 2>&1 &
    sleep 2
    if kill -0 $! 2>/dev/null; then
        echo "[r00k] Band worker RUNNING in background (PID $!). Log: ~/.rook-band-worker/worker.log"
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


class CombinedServer:
    """Single-port server: HTTP for bootstrap + WebSocket for worker connections."""

    def __init__(self, port: int = 7005, auth_token: str = "", domain: str = "rook.bakeforge.com",
                 web_user: str = "", web_pass: str = "",
                 band_psk: str = "rook-bakenet-default-2026"):
        self.port = port
        self.auth_token = auth_token
        self.domain = domain
        self.web_user = web_user
        self.web_pass = web_pass
        self.band_psk = band_psk
        self._workers: dict[str, RemoteWorker] = {}
        self._on_worker_connect = None
        self._on_worker_disconnect = None
        self._on_worker_chat = None  # async callback(worker_name, content, worker_id) -> response
        self._app = web.Application(middlewares=[self._basic_auth_middleware])
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/worker", self._worker_bootstrap)
        self._app.router.add_get("/worker.py", self._worker_script)
        self._app.router.add_get("/band-worker.pyz", self._band_worker_pyz)
        self._app.router.add_get("/ws", self._websocket_handler)
        # Auth routes (handled by middleware, these are just route stubs)
        async def _noop(r): return web.Response(text="")
        self._app.router.add_get("/login", _noop)
        self._app.router.add_post("/login", _noop)
        self._app.router.add_get("/logout", _noop)
        self._app.router.add_get("/health", self._health)
        self._runner: web.AppRunner | None = None

        # Register web UI routes before server starts
        try:
            from ..modules.web_ui import register_routes
            register_routes(self._app)
        except Exception as e:
            log.warning("Web UI routes not registered: %s", e)

    def _make_session_cookie(self) -> str:
        """Generate a session token from credentials."""
        import hashlib
        return hashlib.sha256(f"{self.web_user}:{self.web_pass}:r00k".encode()).hexdigest()[:32]

    @web.middleware
    async def _basic_auth_middleware(self, request: web.Request, handler):
        """Auth via cookie session or basic auth. WS and health exempt."""
        import base64

        # Always exempt
        exempt = ("/ws", "/health", "/worker", "/worker.py", "/band-worker.pyz")
        if request.path == "/ws/ui":
            return await handler(request)
        if any(request.path == p or request.path.startswith(p + "/") for p in exempt) or not self.web_user:
            return await handler(request)

        # Login endpoint
        if request.path == "/login" and request.method == "POST":
            try:
                data = await request.post()
                user = data.get("user", "")
                passwd = data.get("pass", "")
                if user == self.web_user and passwd == self.web_pass:
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

        # Check basic auth (for API/curl)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if user == self.web_user and passwd == self.web_pass:
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
        return f"""<!DOCTYPE html>
<html><head><title>♖ ROOK Login</title>
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: monospace; display: flex; justify-content: center; align-items: center; height: 100vh; }}
.box {{ background: #161b22; border: 1px solid #30363d; padding: 32px; border-radius: 8px; width: 300px; }}
h2 {{ margin-bottom: 16px; }}
input {{ width: 100%; padding: 8px; margin: 4px 0 12px 0; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 4px; font-family: monospace; }}
button {{ width: 100%; padding: 8px; background: #58a6ff; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-family: monospace; }}
.err {{ color: #f85149; font-size: 12px; margin-bottom: 8px; }}
</style></head>
<body><div class="box">
<h2>♖ ROOK</h2>
{"<div class='err'>" + error + "</div>" if error else ""}
<form method="POST" action="/login">
<input name="user" placeholder="Username" autofocus>
<input name="pass" type="password" placeholder="Password">
<button type="submit">Login</button>
</form>
</div></body></html>"""

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Remote server on port %d (HTTP + WebSocket)", self.port)

    async def stop(self) -> None:
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

  Windows (PowerShell):
    iex (irm https://{self.domain}/worker)

  Endpoints:
    /worker           bootstrap script (auto-detects OS)
    /band-worker.pyz  self-contained band-worker zipapp
    /worker.py        legacy exec-worker script
    /ws               legacy websocket endpoint
    /health           server status
"""
        return web.Response(text=text, content_type="text/plain")

    async def _worker_bootstrap(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "").lower()
        kw = {"domain": self.domain, "band_psk": self.band_psk}
        if "powershell" in ua:
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

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text=json.dumps({
            "status": "ok",
            "workers": len(self._workers),
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
    ap.add_argument("--domain", default="rook.bakeforge.com", help="Public domain for installer URLs")
    ap.add_argument("--psk", default="rook-bakenet-default-2026",
                    dest="band_psk", help="Band pre-shared key embedded in bootstrap scripts")
    ap.add_argument("--token", default="", help="Legacy exec-worker auth token")
    ap.add_argument("--web-user", default="", help="Web UI username (empty = no auth)")
    ap.add_argument("--web-pass", default="", help="Web UI password")
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
