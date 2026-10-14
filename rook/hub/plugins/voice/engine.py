"""Where the voice plugin's speech runs: in this process, or on another machine.

* :class:`LocalEngine` imports the voice service (``services/voice``, shipped
  in an installed hub as ``rook._voice_engine``) into the hub process and
  serves its FastAPI app through :class:`rook.hub.asgi_bridge.AsgiBridge`.
  The speech models load in a thread, so the hub keeps answering while they
  do; until then the voice paths answer 503 "starting".
* :class:`RemoteEngine` forwards the voice paths, WebSocket included, to a
  standalone voice server (``python -m services.voice.server`` on a GPU box),
  so clients reach voice through the hub's one address.

Nothing here imports numpy, the speech models or FastAPI at module import:
a hub without the voice extras only pays for these few functions.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("rook.hub.plugins.voice")

#: Where the voice service's code lives: an installed hub carries it inside the
#: rook package (pyproject force-include); a source checkout runs it from
#: services/voice, the directory the GPU host's release.sh deploys.
ENGINE_PACKAGES = ("rook._voice_engine", "services.voice")

#: Python modules the in-process engine needs -> the pip package that has it
#: (``pip install 'rook[voice]'`` installs them all).
REQUIRED = {"fastapi": "fastapi", "numpy": "numpy", "webrtcvad": "webrtcvad",
            "faster_whisper": "faster-whisper", "kokoro_onnx": "kokoro-onnx",
            "onnxruntime": "onnxruntime", "httpx": "httpx"}
#: Model weights the engine loads from its model folder (never in Git, never
#: in the wheel: services/voice/models.json has the Smart Turn URL and digest).
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin", "smart-turn-v3.2-cpu.onnx")
#: 16 kHz mono 16-bit PCM, at most 30 s: the voice service's /api/transcribe limit.
MAX_PCM = 30 * 16000 * 2
EXTRAS_HINT = "pip install 'rook[voice]'"


def find_engine(packages: tuple = ENGINE_PACKAGES) -> str | None:
    """The importable voice service package, without importing its modules."""
    for pkg in packages:
        try:
            if importlib.util.find_spec(pkg + ".server") is not None:
                return pkg
        except (ImportError, ValueError):
            continue
    return None


def missing_extras() -> list[str]:
    """pip packages the in-process engine needs that are not installed."""
    out = []
    for module, pip in REQUIRED.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(pip)
    return out


def missing_models(model_dir: str | Path) -> list[str]:
    d = Path(model_dir)
    return [name for name in MODEL_FILES if not (d / name).is_file()]


def _strip_prefix(path: str, prefix: str) -> str:
    sub = path[len(prefix):] if prefix and path.startswith(prefix) else path
    return sub or "/"


def _not_ready(text: str, status: int = 503):
    from aiohttp import web
    return web.json_response({"error": text}, status=status, headers={"Retry-After": "5"})


class LocalEngine:
    """The voice service's app, in this process."""

    kind = "here"

    def __init__(self, package: str, env: dict[str, str], prefix: str) -> None:
        self.package = package
        self.env = env
        self.prefix = prefix
        self.state = "stopped"        # stopped | starting | ready | error
        self.error = ""
        self.started_at = 0.0
        self.ready_at = 0.0
        self.app: Any = None
        self._lifespan: Any = None
        self._bridges: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._set_env: list[str] = []

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self.state, self.error, self.started_at = "starting", "", time.time()
        self._task = asyncio.get_running_loop().create_task(self._boot())

    async def _boot(self) -> None:
        try:
            # The service reads its configuration from the environment, much
            # of it at import: set the hub's values first, never overriding
            # what the hub's own environment already says.
            for name, value in self.env.items():
                if name not in os.environ:
                    os.environ[name] = value
                    self._set_env.append(name)
            server = await asyncio.to_thread(importlib.import_module, self.package + ".server")
            providers = importlib.import_module(self.package + ".providers")
            # Whisper, Kokoro and Smart Turn load here: seconds of CPU work,
            # off the hub's event loop.
            provider = await asyncio.to_thread(providers.Provider)
            self.app = server.app
            self.app.state.preloaded_provider = provider
            from ...asgi_bridge import AsgiBridge, Lifespan
            self._lifespan = Lifespan(self.app, timeout=60.0)
            await self._lifespan.start()
            self._bridges = {
                "prefix": AsgiBridge(self.app, lifespan=self._lifespan, root_path=self.prefix),
                "root": AsgiBridge(self.app, lifespan=self._lifespan, root_path=""),
            }
            self.state, self.ready_at = "ready", time.time()
            log.info("voice: engine ready in this process (%.1f s)", self.ready_at - self.started_at)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - shown on the Voice page
            self.state, self.error = "error", f"{type(e).__name__}: {e}"[:400]
            log.exception("voice: the engine failed to start")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for bridge in self._bridges.values():
            bridge.stopping = True
        if self._lifespan is not None:
            try:
                await self._lifespan.stop()
            except Exception:
                log.exception("voice: engine shutdown failed")
        self._lifespan, self._bridges, self.app = None, {}, None
        # Forget the service's modules and the environment set for them, so a
        # reload (a changed setting) imports it again with the new values.
        for name in [m for m in sys.modules if m == self.package or m.startswith(self.package + ".")]:
            del sys.modules[name]
        for name in self._set_env:
            os.environ.pop(name, None)
        self._set_env = []
        self.state = "stopped"

    # -- serving ---------------------------------------------------------------
    async def handle(self, request: Any, *, alias: bool = False):
        if self.state != "ready":
            if self.state == "starting":
                return _not_ready("Voice is starting: the speech models are loading.")
            return _not_ready("Voice is not running on this hub. " + (self.error or ""))
        return await self._bridges["root" if alias else "prefix"].handle(request)

    async def health(self) -> dict:
        return {"ok": self.state == "ready", "state": self.state, "error": self.error}

    async def transcribe(self, pcm: bytes) -> str:
        if self.state != "ready":
            raise RuntimeError("voice is not ready on this hub")
        return (await asyncio.wait_for(self.app.state.provider.transcribe(pcm), 30) or "").strip()


# -- another machine -------------------------------------------------------------

#: Request headers never forwarded (hop-by-hop, or the proxy's to set).
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host",
                  "content-length", "sec-websocket-key", "sec-websocket-version",
                  "sec-websocket-extensions", "sec-websocket-accept"})
_MAX_BODY = 16 * 1024 * 1024
_WS_MAX = 8 * 1024 * 1024      # the voice service's own ws_max_size


class RemoteEngine:
    """Forwards the voice paths to a standalone voice server."""

    kind = "remote"

    def __init__(self, base_url: str, prefix: str, *, verify_tls: bool = True,
                 token: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.prefix = prefix
        self.verify_tls = verify_tls
        self.token = token or None     # the hub's own voice key (Voice page checks)
        self.state = "ready"
        self.error = ""
        self._session: Any = None

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.base).netloc

    def _ssl(self) -> Any:
        return bool(self.verify_tls)

    def _http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10),
                auto_decompress=False)
        return self._session

    def url(self, sub_path: str, query: str = "", ws: bool = False) -> str:
        base = self.base
        if ws:
            base = "ws" + base[4:] if base.startswith("http") else base
        return base + sub_path + (("?" + query) if query else "")

    def _headers(self, request: Any) -> dict:
        out = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        peer = request.remote or ""
        prior = request.headers.get("X-Forwarded-For")
        out["X-Forwarded-For"] = f"{prior}, {peer}" if prior else peer
        out["X-Forwarded-Proto"] = request.scheme
        out["X-Forwarded-Host"] = request.host
        out["X-Forwarded-Prefix"] = self.prefix
        return out

    async def handle(self, request: Any, *, alias: bool = False):
        import aiohttp
        sub = request.path if alias else _strip_prefix(request.path, self.prefix)
        if (request.method == "GET" and request.headers.get("Upgrade", "").lower() == "websocket"):
            return await self._websocket(request, sub)
        from aiohttp import web
        if (request.content_length or 0) > _MAX_BODY:
            return web.json_response({"error": "Request too large."}, status=413)
        body = await request.read() if request.can_read_body else None
        try:
            async with self._http().request(
                    request.method, self.url(sub, request.rel_url.raw_query_string),
                    headers=self._headers(request), data=body, allow_redirects=False,
                    ssl=self._ssl(), timeout=aiohttp.ClientTimeout(total=120, connect=10)) as up:
                # Bodies pass through as sent (auto_decompress is off), so
                # Content-Encoding goes with them; Content-Length does not
                # (the reply is re-chunked).
                headers = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}
                resp = web.StreamResponse(status=up.status, reason=up.reason, headers=headers)
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("voice: forwarding %s to %s failed: %s", sub, self.host, e)
            return _not_ready(f"The voice server at {self.host} did not answer.", 502)

    async def _websocket(self, request: Any, sub: str):
        import aiohttp
        from aiohttp import web
        headers = {}
        if request.headers.get("Authorization"):
            headers["Authorization"] = request.headers["Authorization"]
        proto = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
                 if p.strip()]
        try:
            up = await self._http().ws_connect(
                self.url(sub, request.rel_url.raw_query_string, ws=True), headers=headers,
                protocols=proto, max_msg_size=_WS_MAX, ssl=self._ssl(), heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10))
        except aiohttp.WSServerHandshakeError as e:
            # The voice server refused the upgrade (a bad key closes after
            # accept, so this is rarer): say so with its status.
            return web.json_response({"error": "The voice server refused the connection."},
                                     status=e.status if 400 <= e.status < 600 else 502)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("voice: WebSocket to %s failed: %s", self.host, e)
            return _not_ready(f"The voice server at {self.host} did not answer.", 502)
        down = web.WebSocketResponse(max_msg_size=_WS_MAX, heartbeat=40,
                                     protocols=[up.protocol] if up.protocol else ())
        await down.prepare(request)

        async def upward():
            async for msg in down:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await up.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await up.send_bytes(msg.data)
                else:
                    break
            await up.close(code=down.close_code or 1000)

        async def downward():
            async for msg in up:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await down.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await down.send_bytes(msg.data)
                else:
                    break
            # Pass the voice server's close code on (4401 bad key, 4409 busy).
            await down.close(code=up.close_code or 1000)

        tasks = [asyncio.ensure_future(upward()), asyncio.ensure_future(downward())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not up.closed:
                await up.close()
            if not down.closed:
                await down.close()
        return down

    async def health(self) -> dict:
        import aiohttp
        t0 = time.monotonic()
        try:
            async with self._http().get(self.url("/health"), ssl=self._ssl(),
                                        timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json(content_type=None)
                ok = r.status == 200 and isinstance(data, dict) and bool(data.get("ok"))
                return {"ok": ok, "state": "ready" if ok else "error",
                        "version": (data or {}).get("version") if isinstance(data, dict) else None,
                        "latency_ms": round((time.monotonic() - t0) * 1000),
                        "error": "" if ok else f"HTTP {r.status}"}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            return {"ok": False, "state": "unreachable",
                    "error": f"cannot reach {self.host}: {type(e).__name__}"}

    async def transcribe(self, pcm: bytes) -> str:
        import aiohttp
        if not self.token:
            raise RuntimeError("the hub has no voice key for the voice server: set "
                               "voice.token (Client bearer token) to a key it accepts")
        async with self._http().post(
                self.url("/api/transcribe"), data=pcm, ssl=self._ssl(),
                headers={"Content-Type": "audio/L16", "Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=40)) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                detail = data.get("detail") if isinstance(data, dict) else None
                raise RuntimeError(f"the voice server said HTTP {r.status}"
                                   + (f": {detail}" if detail else ""))
            return str((data or {}).get("text") or "").strip()
