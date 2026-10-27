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
#: in the wheel): the ``required`` files of services/voice/models.json, which
#: has their URLs and digests; services/voice/fetch_models.py downloads them.
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin", "smart-turn-v3.2-cpu.onnx")
#: 16 kHz mono 16-bit PCM, at most 30 s: the voice service's /api/transcribe limit.
MAX_PCM = 30 * 16000 * 2
EXTRAS_HINT = "pip install 'rook[voice]'"
#: Longest stop() waits for a model load in progress to finish.
BOOT_WAIT = 180
#: A load stop() gave up waiting for, with its teardown to follow; the next
#: engine waits for it rather than start on its modules and environment.
_STALE: "asyncio.Task | None" = None
STALE_REASON = ("The previous voice engine is still loading its models (a setting changed "
                "while they loaded). Voice starts with the new settings once that load "
                "finishes; restart the hub if it never does.")
#: The voice service's /api/voice limit: text per request.
MAX_SPEAK = 700


def wav(pcm: bytes, rate: int) -> bytes:
    """Mono 16-bit PCM as a WAV file (what /api/voice answers)."""
    import io
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes(pcm)
    return out.getvalue()


def _speak_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text or len(text) > MAX_SPEAK:
        raise ValueError(f"speak 1 to {MAX_SPEAK} characters at a time")
    return text


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


def has_userinfo(url: str) -> bool:
    from ..home.llm import has_userinfo as check
    return check(url)


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
        self.state = "stopped"        # stopped | waiting | starting | ready | error
        self.error = ""
        self.started_at = 0.0
        self.ready_at = 0.0
        self.app: Any = None
        self._lifespan: Any = None
        self._bridges: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._set_env: list[str] = []
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self.state, self.error, self.started_at = "starting", "", time.time()
        self._stopping = False
        if _STALE is not None and not _STALE.done():
            # Never start on the old load's modules and environment (its
            # VOICE_TOKEN, say): wait for it to finish and be torn down.
            self.state, self.error = "waiting", STALE_REASON
            log.warning("voice: %s", STALE_REASON)
        self._task = asyncio.get_running_loop().create_task(self._boot())

    async def _boot(self) -> None:
        stale = _STALE
        if stale is not None and not stale.done():
            await asyncio.wait({stale})
            if self._stopping:
                return
            self.state, self.error, self.started_at = "starting", "", time.time()
        try:
            # The service reads its configuration from the environment, much
            # of it at import: set the hub's values first, never overriding
            # what the hub's own environment already says.
            for name, value in self.env.items():
                if name not in os.environ:
                    os.environ[name] = value
                    self._set_env.append(name)
            server = await asyncio.to_thread(importlib.import_module, self.package + ".server")
            if self._stopping:
                return
            providers = importlib.import_module(self.package + ".providers")
            # Whisper, Kokoro and Smart Turn load here: seconds of CPU work,
            # off the hub's event loop.
            provider = await asyncio.to_thread(providers.Provider)
            if self._stopping:
                # Stopped (a setting changed) while the models loaded: never
                # start this app; stop() tears down once this returns.
                return
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
        global _STALE
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done() and self.state == "waiting":
            # Only waiting on an older load: nothing of its own runs yet.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif task is not None and not task.done():
            # Cancelling would not stop the import or model load running in
            # its thread: wait for it, so two loads never run side by side and
            # the modules and environment are not torn down under it.
            await asyncio.wait({task}, timeout=BOOT_WAIT)
            if not task.done():
                log.error("voice: the engine is still loading after %d s; it is torn down "
                          "once it finishes, and the next start waits for that", BOOT_WAIT)
                _STALE = asyncio.get_running_loop().create_task(self._finish_late(task))
                self.state = "stopped"
                return
        await self._teardown()

    async def _finish_late(self, task: asyncio.Task) -> None:
        await asyncio.wait({task})
        await self._teardown()
        log.info("voice: the late engine load finished and was torn down")

    async def _teardown(self) -> None:
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
            if self.state == "waiting":
                return _not_ready(STALE_REASON)
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

    def voices(self) -> dict:
        """The voice service's voices: ``{voices, default, catalog?}`` (its /voices)."""
        if self.state != "ready":
            raise RuntimeError("voice is not ready on this hub")
        provider = self.app.state.provider
        tts = getattr(provider, "tts", None)
        if tts is not None:
            return tts.describe()
        return {"voices": list(provider.voices), "default": provider.default_voice}

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        """``text`` spoken in ``voice`` (blank: the service's default), as WAV."""
        if self.state != "ready":
            raise RuntimeError("voice is not ready on this hub")
        text = _speak_text(text)
        provider = self.app.state.provider
        tts = importlib.import_module(self.package + ".tts")
        chosen = tts.resolve_voice(provider, voice or provider.default_voice)
        if chosen is None:
            raise ValueError(f"unknown voice {voice!r}")
        pcm, rate = await asyncio.wait_for(provider.synthesize(text, chosen), 30)
        if not pcm:
            raise RuntimeError("the voice engine returned no audio")
        return wav(pcm, rate)


# -- another machine -------------------------------------------------------------

#: Request headers never forwarded (hop-by-hop, or the proxy's to set).
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host",
                  "content-length", "sec-websocket-key", "sec-websocket-version",
                  "sec-websocket-extensions", "sec-websocket-accept"})
_MAX_BODY = 16 * 1024 * 1024
_WS_MAX = 8 * 1024 * 1024      # the voice service's own ws_max_size
#: The only cookie that goes to the voice server and back: its admin session.
ADMIN_COOKIE = "voice_admin"
#: Voice WebSockets one client address may hold open through the hub.
WS_PER_CLIENT = 16


def _voice_cookie(header: str) -> str:
    """The ``voice_admin`` pairs of a Cookie header, as sent; nothing else
    (the dashboard's session cookie never leaves the hub)."""
    keep = [part.strip() for part in (header or "").split(";")
            if part.strip().partition("=")[0].strip() == ADMIN_COOKIE]
    return "; ".join(keep)


def _set_cookie(value: str, prefix: str) -> str | None:
    """A voice server Set-Cookie fit for the hub's site: only ``voice_admin``,
    its Path moved under ``prefix``, no Domain. Anything else is dropped."""
    parts = [p.strip() for p in (value or "").split(";")]
    if not parts or parts[0].partition("=")[0].strip() != ADMIN_COOKIE:
        return None
    out, path = [parts[0]], prefix + "/admin"
    for attr in parts[1:]:
        name, _, val = attr.partition("=")
        lname = name.strip().lower()
        if not attr or lname == "domain":
            continue
        if lname == "path":
            val = val.strip().rstrip("/")
            path = val if val == prefix or val.startswith(prefix + "/") else prefix + val
            continue
        out.append(attr)
    out.append("Path=" + path)
    return "; ".join(out)


def _is_loopback(addr: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def client_address(request: Any) -> str:
    """The client's address: the tunnel's X-Forwarded-For when a loopback
    proxy (cloudflared) sent the request, else the peer."""
    peer = request.remote or ""
    if _is_loopback(peer):
        fwd = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        if fwd:
            return fwd
    return peer or "unknown"


#: How often a synthesis is tried again while the voice server is busy with
#: another line, and the first wait between tries (seconds; it grows).
BUSY_RETRIES = 4
BUSY_WAIT = 0.4


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
        self._ws_open: dict[str, int] = {}

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    @property
    def host(self) -> str:
        from ..home.llm import url_host
        return url_host(self.base)

    def _ssl(self) -> Any:
        return bool(self.verify_tls)

    def _http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            # No pool limit (aiohttp's default is 100): long-lived voice
            # WebSockets must never starve forwarded requests; WS_PER_CLIENT
            # bounds what one client can hold.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=0),
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10),
                auto_decompress=False)
        return self._session

    #: The hub's own calls ask for plain bodies: the session never decompresses
    #: (forwarded replies pass through as they are), so a gzipped answer from a
    #: CDN in front of the voice server would not parse.
    OWN = {"Accept-Encoding": "identity"}

    @staticmethod
    async def _body(r: Any) -> bytes:
        """A reply body, decompressed if the server compressed it anyway."""
        import zlib
        data = await r.read()
        enc = (r.headers.get("Content-Encoding") or "").strip().lower()
        try:
            if enc in ("gzip", "x-gzip"):
                return zlib.decompress(data, 16 + zlib.MAX_WBITS)
            if enc == "deflate":
                try:
                    return zlib.decompress(data)
                except zlib.error:
                    return zlib.decompress(data, -zlib.MAX_WBITS)
        except zlib.error as e:
            raise ValueError(f"the voice server sent a bad {enc} body") from e
        return data

    async def _json(self, r: Any) -> Any:
        import json
        return json.loads(await self._body(r))

    def url(self, sub_path: str, query: str = "", ws: bool = False) -> str:
        base = self.base
        if ws:
            base = "ws" + base[4:] if base.startswith("http") else base
        return base + sub_path + (("?" + query) if query else "")

    def forward_url(self, raw_sub: str, query: str = "", ws: bool = False):
        """The voice server's URL for a client path, exactly as the client
        encoded it (no re-encoding, no normalisation)."""
        from yarl import URL
        return URL(self.url(raw_sub, query, ws=ws), encoded=True)

    def upstream_host(self) -> str:
        """The Host the voice server is addressed by (what aiohttp sends for
        :attr:`base`: no port when it is the scheme's default)."""
        from yarl import URL
        u = URL(self.base)
        host = (u.raw_host or "").rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        if u.port is not None and not u.is_default_port():
            host += f":{u.port}"
        return host

    def _headers(self, request: Any) -> dict:
        # Never the dashboard's session or Cloudflare Access identity: only
        # the voice server's own admin cookie goes.
        out = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP and k.lower() != "cookie"
               and not k.lower().startswith("cf-access-")}
        cookie = _voice_cookie(request.headers.get("Cookie", ""))
        if cookie:
            out["Cookie"] = cookie
        # Host stays the voice server's own (aiohttp sets it from the URL): a
        # name-based proxy in front of it routes on it. The voice admin's
        # same-origin check compares Origin with that Host, so a page on this
        # hub's own origin (the hub already holds the same check: Origin is
        # https://<the Host it was asked for>) is presented as the voice
        # server's origin; any other Origin goes as sent and is refused there.
        origin = request.headers.get("Origin")
        if origin and origin.lower() == ("https://" + request.host).lower():
            out["Origin"] = "https://" + self.upstream_host()
        peer = request.remote or ""
        prior = request.headers.get("X-Forwarded-For")
        # Behind a local tunnel its chain is the client's; this hub's loopback
        # hop would only hide the client from a voice server that trusts the
        # hub (uvicorn's FORWARDED_ALLOW_IPS), so it is not appended.
        if prior and _is_loopback(peer):
            out["X-Forwarded-For"] = prior
        else:
            out["X-Forwarded-For"] = f"{prior}, {peer}" if prior else peer
        out["X-Forwarded-Proto"] = request.scheme
        out["X-Forwarded-Host"] = request.host
        out["X-Forwarded-Prefix"] = self.prefix
        return out

    def _reply_headers(self, up_headers: Any):
        from multidict import CIMultiDict
        out: Any = CIMultiDict()
        for k, v in up_headers.items():
            lk = k.lower()
            if lk in _HOP:
                continue
            if lk == "set-cookie":
                v = _set_cookie(v, self.prefix)
                if v is None:
                    continue
            out.add(k, v)
        return out

    async def handle(self, request: Any, *, alias: bool = False):
        import aiohttp
        from aiohttp import web
        from urllib.parse import unquote
        raw = request.rel_url.raw_path
        # /voice/../x must never reach the voice server as /x (encoded or not).
        if any(seg in (".", "..") for seg in unquote(raw).split("/")):
            return web.json_response({"error": "Bad path."}, status=400)
        sub = raw if alias else _strip_prefix(raw, self.prefix)
        if (request.method == "GET" and request.headers.get("Upgrade", "").lower() == "websocket"):
            return await self._websocket(request, sub)
        if (request.content_length or 0) > _MAX_BODY:
            return web.json_response({"error": "Request too large."}, status=413)
        body = await request.read() if request.can_read_body else None
        try:
            async with self._http().request(
                    request.method, self.forward_url(sub, request.rel_url.raw_query_string),
                    headers=self._headers(request), data=body, allow_redirects=False,
                    ssl=self._ssl(), timeout=aiohttp.ClientTimeout(total=120, connect=10)) as up:
                # Bodies pass through as sent (auto_decompress is off), so
                # Content-Encoding goes with them; Content-Length does not
                # (the reply is re-chunked).
                headers = self._reply_headers(up.headers)
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
        from aiohttp import web
        who = client_address(request)
        if self._ws_open.get(who, 0) >= WS_PER_CLIENT:
            return web.json_response({"error": "Too many voice connections from this address."},
                                     status=429)
        self._ws_open[who] = self._ws_open.get(who, 0) + 1
        try:
            return await self._websocket_open(request, sub)
        finally:
            left = self._ws_open.get(who, 1) - 1
            if left > 0:
                self._ws_open[who] = left
            else:
                self._ws_open.pop(who, None)

    async def _websocket_open(self, request: Any, sub: str):
        import aiohttp
        from aiohttp import web
        headers = {}
        if request.headers.get("Authorization"):
            headers["Authorization"] = request.headers["Authorization"]
        proto = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
                 if p.strip()]
        try:
            up = await self._http().ws_connect(
                self.forward_url(sub, request.rel_url.raw_query_string, ws=True),
                headers=headers,
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
            async with self._http().get(self.url("/health"), ssl=self._ssl(), headers=self.OWN,
                                        timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await self._json(r)
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
                headers={**self.OWN, "Content-Type": "audio/L16",
                         "Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=40)) as r:
            data = await self._json(r)
            if r.status != 200:
                detail = data.get("detail") if isinstance(data, dict) else None
                raise RuntimeError(f"the voice server said HTTP {r.status}"
                                   + (f": {detail}" if detail else ""))
            return str((data or {}).get("text") or "").strip()

    async def voices(self) -> dict:
        import aiohttp
        async with self._http().get(self.url("/voices"), ssl=self._ssl(), headers=self.OWN,
                                    timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await self._json(r)
            if r.status != 200 or not isinstance(data, dict):
                raise RuntimeError(f"the voice server said HTTP {r.status}")
            return data

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        import json
        import aiohttp
        text = _speak_text(text)
        body: dict = {"text": text}
        if voice:
            body["voice"] = voice
        headers = {**self.OWN, "Content-Type": "application/json"}
        if self.token:   # a keyless request works only where the server allows guests
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(BUSY_RETRIES + 1):
            async with self._http().post(
                    self.url("/api/voice"), data=json.dumps(body).encode(), ssl=self._ssl(),
                    headers=headers, timeout=aiohttp.ClientTimeout(total=40)) as r:
                data = await self._body(r)
                if r.status == 200:
                    return data
                detail = ""
                try:
                    detail = str((json.loads(data) or {}).get("detail") or "")
                except (ValueError, AttributeError):
                    pass
            # It speaks one line at a time and says 503 "busy" to the next.
            if r.status == 503 and "busy" in detail.lower() and attempt < BUSY_RETRIES:
                await asyncio.sleep(BUSY_WAIT * (attempt + 1))
                continue
            raise RuntimeError(f"the voice server said HTTP {r.status}"
                               + (f": {detail}" if detail else ""))
        raise RuntimeError("the voice server stayed busy")
