"""The voice hub plugin (rook/hub/plugins/voice): voice served on the hub site.

* without the voice extras it does not load, and says why in plain words;
* engine "here" runs the voice service in the hub process (a fake service
  package stands in for services/voice: the real one needs the speech models);
* engine "remote" forwards /voice/* (WebSocket included) to a voice server on
  another machine, end to end through ``rook hub serve``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import textwrap
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rook.hub.node import HubNode
from rook.hub.plugins import voice as voice_mod
from rook.hub.plugins.voice import engine as eng
from rook.hub.settings_store import SettingsStore

from tests.test_hub_serve import EchoRelay, free_port, hub_env  # noqa: F401 (fixture)

VOICE_ENV = ("VOICE_MODEL_DIR", "WHISPER_MODEL", "VOICE_TOKEN", "ROOK_VOICE_ENGINE",
             "ROOK_VOICE_REMOTE_URL", "VOICE_ALLOW_ANONYMOUS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in VOICE_ENV:
        monkeypatch.delenv(var, raising=False)


def node_for(tmp_path, **stored):
    store = SettingsStore(tmp_path / "settings.db")
    for key, value in stored.items():
        store.set(f"voice.{key}", "hub", "", value=value, actor="test")
    return HubNode(str(tmp_path), entry_points=False, settings_store=store)


async def wait_for(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


# -- not loaded, and why ---------------------------------------------------------------

def test_without_the_extras_it_stays_unloaded_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "missing_extras", lambda: ["webrtcvad", "faster-whisper"])
    node = node_for(tmp_path)
    assert node.plugin("voice") is None
    st = node.host.status["voice"]
    assert st["state"] == "unavailable" and st["namespace"] == "voice"
    assert "pip install 'rook[voice]'" in st["reason"] and "webrtcvad" in st["reason"]
    assert "remote" in st["reason"]                     # the way out without extras
    from rook.hub.plugin_admin import cell_from_status
    cell = cell_from_status(False, st)
    assert cell["state"] == "unavailable" and "rook[voice]" in cell["reason"]
    assert cell["toggle"] == "enable"                    # try again once installed
    # The Voice page still opens, and says the same.
    page = voice_mod.page_data(node.settings, node, None)
    assert page["status"]["loaded"] is False and "rook[voice]" in page["status"]["reason"]
    assert page["requirements"]["extras_missing"] == ["webrtcvad", "faster-whisper"]


def test_requirements_name_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "missing_extras", lambda: [])
    r = voice_mod.requirements("here", "", str(tmp_path), ("no_such_voice_pkg",))
    assert r["engine_code"] is None and "code is not installed" in r["reason"]
    r = voice_mod.requirements("here", "", str(tmp_path))
    assert r["engine_code"] == "services.voice"           # a source checkout
    assert r["models_missing"] == list(eng.MODEL_FILES) and str(tmp_path) in r["reason"]
    for name in eng.MODEL_FILES:
        (tmp_path / name).write_bytes(b"x")
    assert voice_mod.requirements("here", "", str(tmp_path))["reason"] == ""
    assert "address is not set" in voice_mod.requirements("remote", "", "")["reason"]
    assert voice_mod.requirements("remote", "http://192.0.2.7:8900", "")["reason"] == ""


def test_the_voice_settings_kept_their_keys_and_variables(tmp_path):
    from rook.hub.settings_schema import Schema
    s = Schema()
    for key, env in (("voice.token", "VOICE_TOKEN"), ("voice.whisper_model", "WHISPER_MODEL"),
                     ("voice.default_voice", "VOICE"), ("voice.llm_url", "VLLM_URL"),
                     ("voice.assistant_name", "ROOK_VOICE_ASSISTANT_NAME"),
                     ("voice.model_dir", "VOICE_MODEL_DIR")):
        e = s.get(key)
        assert e is not None and e.origin == "hub-plugin" and env in e.env_names(), key
    assert s.get("voice.token").setting.secret
    assert s.plugins()["voice"]["title"] == "Voice"
    assert s.get("voice.engine").setting.choices == ("here", "remote")


# -- engine "here" ------------------------------------------------------------------------

FAKE_SERVER = '''
import contextlib, os
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

@contextlib.asynccontextmanager
async def lifespan(app):
    app.state.provider = getattr(app.state, "preloaded_provider", None)
    yield

async def health(request):
    return JSONResponse({"ok": True, "root": request.scope.get("root_path"),
                         "path": request.url.path, "token": os.environ.get("VOICE_TOKEN")})

async def transcribe(request):
    return JSONResponse({"text": await request.app.state.provider.transcribe(await request.body())})

async def ws(websocket):
    await websocket.accept()
    msg = await websocket.receive_text()
    await websocket.send_text("echo:" + msg)
    await websocket.close(code=4409)

app = Starlette(routes=[Route("/health", health), Route("/api/transcribe", transcribe, methods=["POST"]),
                        WebSocketRoute("/ws", ws)], lifespan=lifespan)
'''

FAKE_PROVIDERS = '''
import os
class Provider:
    def __init__(self):
        self.model = os.environ.get("WHISPER_MODEL", "?")
    async def transcribe(self, pcm):
        return f" heard {len(pcm)} bytes with {self.model} "
'''


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    pkg = tmp_path / "pkgs" / "fakevoice"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")
    (pkg / "providers.py").write_text(textwrap.dedent(FAKE_PROVIDERS), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    monkeypatch.setattr(voice_mod.Voice, "ENGINE_PACKAGES", ("fakevoice",))
    monkeypatch.setattr(eng, "missing_extras", lambda: [])
    models = tmp_path / "models"
    models.mkdir()
    for name in eng.MODEL_FILES:
        (models / name).write_bytes(b"weights")
    yield SimpleNamespace(models=models)
    for name in [m for m in sys.modules if m.startswith("fakevoice")]:
        del sys.modules[name]


def site_app(node):
    @web.middleware
    async def plugin_paths(request, handler):
        served = node.site.match(request.path)
        return await served(request) if served is not None else await handler(request)
    async def dashboard(request):
        return web.json_response({"dashboard": True})
    app = web.Application(middlewares=[plugin_paths])
    app.router.add_get("/health", dashboard)
    return app


@pytest.mark.asyncio
async def test_engine_here_serves_the_voice_app_in_process(tmp_path, fake_engine):
    node = node_for(tmp_path, model_dir=str(fake_engine.models), whisper_model="tiny.en")
    voice = node.plugin("voice")
    assert voice is not None, node.host.status["voice"]
    await node.start()
    try:
        assert await wait_for(lambda: voice.engine.state == "ready"), voice.engine.error
        # Only what an operator stored reaches the service, and the hub's
        # environment is never overridden.
        assert os.environ["WHISPER_MODEL"] == "tiny.en"
        assert os.environ["VOICE_MODEL_DIR"] == str(fake_engine.models)
        assert "VOICE_TOKEN" not in os.environ
        async with TestClient(TestServer(site_app(node))) as http:
            r = await http.get("/voice", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/voice/"
            body = await (await http.get("/voice/health")).json()
            assert body["root"] == "/voice" and body["path"] == "/voice/health"
            assert (await (await http.get("/health")).json()) == {"dashboard": True}
            # The Android app's root path reaches the same app.
            r = await http.post("/api/transcribe", data=b"\0\0" * 8)
            assert (await r.json())["text"].strip() == "heard 16 bytes with tiny.en"
            ws = await http.ws_connect("/voice/ws")
            await ws.send_str("hi")
            assert (await ws.receive()).data == "echo:hi"
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4409
            await ws.close()
        # The Voice page's microphone check, in process.
        res = await voice.page_action(node.settings, {
            "action": "voice_transcribe", "pcm": base64.b64encode(b"\1\0" * 160).decode()}, "human:op")
        assert res["ok"] and res["text"] == "heard 320 bytes with tiny.en" and res["seconds"] == 0.01
        with pytest.raises(ValueError):
            await voice.transcribe(b"\0")              # not whole samples
        check = await voice.check()
        assert check["ok"] and voice.status()["served"]
        assert voice.status_cap() == {"served": True, "engine": "here", "state": "ready",
                                      "paths": ["/voice", "/api/voice", "/api/transcribe", "/voices"]}
    finally:
        await node.stop()
    # Stopped: nothing served, the service forgotten, its environment gone.
    assert node.site.match("/voice/ws") is None and node.site.match("/api/transcribe") is None
    assert "fakevoice.server" not in sys.modules
    assert "WHISPER_MODEL" not in os.environ and "VOICE_MODEL_DIR" not in os.environ


@pytest.mark.asyncio
async def test_engine_here_answers_503_while_the_models_load(tmp_path, fake_engine, monkeypatch):
    gate = asyncio.Event()
    real = asyncio.to_thread

    async def slow(fn, *a, **k):
        if getattr(fn, "__name__", "") == "Provider":
            await gate.wait()
        return await real(fn, *a, **k)
    monkeypatch.setattr(eng.asyncio, "to_thread", slow)
    node = node_for(tmp_path, model_dir=str(fake_engine.models), root_aliases=False)
    await node.start()
    try:
        voice = node.plugin("voice")
        assert node.site.match("/api/transcribe") is None      # aliases off
        async with TestClient(TestServer(site_app(node))) as http:
            r = await http.get("/voice/health")
            assert r.status == 503 and "starting" in (await r.json())["error"]
            gate.set()
            assert await wait_for(lambda: voice.engine.state == "ready")
            assert (await http.get("/voice/health")).status == 200
    finally:
        gate.set()
        await node.stop()


@pytest.mark.asyncio
async def test_switching_to_remote_reloads_and_a_missing_address_is_said(tmp_path, monkeypatch):
    import rook.hub.plugins.settings as settings_plugin
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    monkeypatch.setattr(eng, "missing_extras", lambda: ["webrtcvad"])
    node = node_for(tmp_path)
    await node.start()
    try:
        assert node.plugin("voice") is None
        node.settings.set("voice.engine", "remote", actor="human:op")
        await asyncio.sleep(0.1)
        assert node.plugin("voice") is None
        assert "address is not set" in node.host.status["voice"]["reason"]
        node.settings.set("voice.remote_url", "http://192.0.2.7:8900", actor="human:op")
        assert await wait_for(lambda: node.plugin("voice") is not None)
        assert node.plugin("voice").status()["remote_host"] == "192.0.2.7:8900"
        assert node.site.match("/voice/ws") is not None
    finally:
        await node.stop()


# -- engine "remote", through rook hub serve ----------------------------------------------

def fake_voice_server(seen):
    async def health(request):
        seen.append(("health", request.headers.get("X-Forwarded-Prefix")))
        return web.json_response({"ok": True, "version": "2.0.0", "protocol": 2})

    async def transcribe(request):
        seen.append(("transcribe", request.headers.get("Authorization")))
        if request.headers.get("Authorization") != "Bearer voice-key-for-tests":
            return web.json_response({"detail": "A valid voice key is required."}, status=401)
        pcm = await request.read()
        return web.json_response({"text": f"turn on the kitchen lights ({len(pcm)})", "seconds": 1})

    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        seen.append(("ws", request.query.get("token")))
        async for msg in sock:
            if msg.data == "bye":
                await sock.close(code=4401)
                break
            if msg.type == aiohttp.WSMsgType.BINARY:
                await sock.send_bytes(b"RK2A" + msg.data)
            else:
                await sock.send_str("echo:" + msg.data)
        return sock

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/api/transcribe", transcribe)
    app.router.add_get("/ws", ws)
    return app


@pytest.mark.asyncio
async def test_engine_remote_end_to_end_through_the_hub(hub_env, monkeypatch):  # noqa: F811
    from rook.hub.serve import Hub, parse_args
    from rook.remote.bootstrap import prepare
    seen: list = []
    upstream = TestServer(fake_voice_server(seen))
    await upstream.start_server()
    monkeypatch.setenv("ROOK_VOICE_ENGINE", "remote")
    monkeypatch.setenv("ROOK_VOICE_REMOTE_URL", str(upstream.make_url("")).rstrip("/"))
    monkeypatch.setenv("VOICE_TOKEN", "voice-key-for-tests")
    loop = asyncio.get_running_loop()
    relay, _ = await loop.create_datagram_endpoint(EchoRelay, local_addr=("127.0.0.1", hub_env.relay))
    ap, args = parse_args(hub_env.argv)
    prepare(ap, args)
    hub = Hub(args, hub_env.argv)
    await hub.start()
    try:
        node = hub.hub_node()
        assert node.plugin("voice") is not None, node.host.status.get("voice")
        base = hub_env.base
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/voice/health") as r:
                assert r.status == 200 and (await r.json())["version"] == "2.0.0"
            assert seen[-1] == ("health", "/voice")
            async with http.get(base + "/health") as r:     # still the dashboard's
                assert "band_workers" in await r.json()
            async with http.post(base + "/api/transcribe", data=b"\0\0" * 100,
                                 headers={"Authorization": "Bearer voice-key-for-tests",
                                          "Content-Type": "audio/L16"}) as r:
                assert r.status == 200 and "kitchen lights (200)" in (await r.json())["text"]
            async with http.post(base + "/voice/api/transcribe", data=b"\0\0") as r:
                assert r.status == 401                       # the voice server's own auth
            async with http.ws_connect(base.replace("http", "ws") + "/voice/ws?token=abc") as ws:
                await ws.send_str('{"type":"hello","protocol":2}')
                assert (await ws.receive(timeout=5)).data == 'echo:{"type":"hello","protocol":2}'
                await ws.send_bytes(b"\1\2")
                assert (await ws.receive(timeout=5)).data == b"RK2A\1\2"
                await ws.send_str("bye")
                msg = await ws.receive(timeout=5)
                assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4401
            assert ("ws", "abc") in seen

            # Manage > Voice, through the dashboard's account API.
            store = hub.server._accounts.store
            tok = store.new_session(hub.server._accounts.bootstrap_id)
            hdr = {"Cookie": "rook_account=" + tok, "Origin": base}
            async with http.get(base + "/account/settings/api?view=voice", headers=hdr) as r:
                page = await r.json()
                assert r.status == 200, page
            assert page["status"]["engine"] == "remote" and page["status"]["served"]
            assert page["token_set"] is True and page["settings"]["engine"]["locked"]
            body = {"action": "voice_transcribe", "csrf": page["csrf"],
                    "pcm": base64.b64encode(b"\0\0" * 1600).decode()}
            async with http.post(base + "/account/settings/api", json=body, headers=hdr) as r:
                res = await r.json()
                assert r.status == 200 and res["text"].startswith("turn on the kitchen lights"), res
            async with http.post(base + "/account/settings/api", headers=hdr,
                                 json={"action": "voice_check", "csrf": page["csrf"]}) as r:
                assert (await r.json())["check"]["ok"] is True

            # Switched off on the Plugins page: the paths go away at once.
            off = await node.set_plugin_enabled("voice", False, "human:op")
            assert off["ok"]
            before = len(seen)
            async with http.get(base + "/voice/health") as r:
                assert r.status in (401, 404)                # the dashboard's answer now
            assert len(seen) == before
    finally:
        await hub.stop()
        relay.close()
        await upstream.close()


@pytest.mark.asyncio
async def test_remote_engine_says_when_the_voice_server_is_down(tmp_path):
    port = free_port()
    remote = eng.RemoteEngine(f"http://127.0.0.1:{port}", "/voice", token="k")
    try:
        health = await remote.health()
        assert health["ok"] is False and health["state"] == "unreachable"
        with pytest.raises(Exception):
            await remote.transcribe(b"\0\0")
        app = web.Application()

        async def handler(request):
            return await remote.handle(request)
        app.router.add_route("*", "/voice/{tail:.*}", handler)
        async with TestClient(TestServer(app)) as http:
            r = await http.get("/voice/health")
            assert r.status == 502 and "did not answer" in (await r.json())["error"]
    finally:
        await remote.stop()
    no_key = eng.RemoteEngine("http://192.0.2.7:8900", "/voice")
    with pytest.raises(RuntimeError, match="no voice key"):
        await no_key.transcribe(b"\0\0")
