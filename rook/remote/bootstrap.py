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
# R00K Worker Bootstrap (Windows)
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
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}}

Write-Host "[r00k] Python: $(python --version)"

# Download and run worker
$wk = "$env:TEMP\\rook_worker.py"
Invoke-WebRequest -Uri "https://{domain}/worker.py" -OutFile $wk
python $wk --server wss://{domain}/ws --token "{token}" --name "$env:COMPUTERNAME"
'''

BASH_BOOTSTRAP = '''#!/bin/bash
set -e

# R00K Worker Bootstrap (Linux/Mac/Termux)

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
        # Termux
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

# Ensure curl exists first
install_curl

# Ensure Python exists
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

# Download and run worker
curl -sL https://{domain}/worker.py -o /tmp/rook_worker.py
$PYTHON /tmp/rook_worker.py --server wss://{domain}/ws --token "{token}" --name "$(hostname)"
'''


class CombinedServer:
    """Single-port server: HTTP for bootstrap + WebSocket for worker connections."""

    def __init__(self, port: int = 7005, auth_token: str = "", domain: str = "rook.bake.systems",
                 web_user: str = "", web_pass: str = ""):
        self.port = port
        self.auth_token = auth_token
        self.domain = domain
        self.web_user = web_user
        self.web_pass = web_pass
        self._workers: dict[str, RemoteWorker] = {}
        self._on_worker_connect = None
        self._on_worker_disconnect = None
        self._on_worker_chat = None  # async callback(worker_name, content, worker_id) -> response
        self._app = web.Application(middlewares=[self._basic_auth_middleware])
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/worker", self._worker_bootstrap)
        self._app.router.add_get("/worker.py", self._worker_script)
        self._app.router.add_get("/ws", self._websocket_handler)
        self._app.router.add_get("/health", self._health)
        self._runner: web.AppRunner | None = None

    @web.middleware
    async def _basic_auth_middleware(self, request: web.Request, handler):
        """Basic auth on HTTP endpoints. WS and health are exempt."""
        # Skip auth for WS upgrade, health, and if no creds configured
        if request.path in ("/ws", "/health", "/worker", "/worker.py") or not self.web_user:
            return await handler(request)

        import base64
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if user == self.web_user and passwd == self.web_pass:
                    return await handler(request)
            except Exception:
                pass

        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="r00k"'},
            text="Unauthorized",
        )

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

        # Browser or plain request — show instructions
        text = f"""
  R ☠ ☠ K  Remote Worker
  ========================

  Linux / Mac:
    curl -sL https://{self.domain}/worker | bash

  Windows (PowerShell):
    iex (irm https://{self.domain}/worker)

  Manual:
    curl -sL https://{self.domain}/worker.py -o worker.py
    python3 worker.py --server wss://{self.domain}/ws --name mypc

  Endpoints:
    /worker     bootstrap script (auto-detects OS)
    /worker.py  raw python worker script
    /ws         websocket endpoint for workers
    /health     server status
"""
        return web.Response(text=text, content_type="text/plain")

    async def _worker_bootstrap(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "").lower()
        kw = {"token": self.auth_token, "domain": self.domain}
        if "powershell" in ua:
            return web.Response(text=PS_BOOTSTRAP.format(**kw), content_type="text/plain")
        return web.Response(text=BASH_BOOTSTRAP.format(**kw), content_type="text/plain")

    async def _worker_script(self, request: web.Request) -> web.Response:
        return web.Response(text=WORKER_SCRIPT, content_type="text/plain")

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
