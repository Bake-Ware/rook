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

from rook.hub import site as site_mod
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
    for s in voice_mod.SETTINGS:              # every variable a voice setting reads
        for var in s.env_names("voice"):
            monkeypatch.delenv(var, raising=False)
    # These tests build hub nodes the way rook hub serve does (it says the
    # site is served before building one); the two-process case sets False.
    monkeypatch.setattr(site_mod, "_SERVED", True)


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
    # rook hub serve itself says the site is served, before plugins load.
    monkeypatch.setattr(site_mod, "_SERVED", False)
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
            # Even a mount the reserved list missed never shadows a dashboard route.
            before = len(seen)
            node.site._exact["/health"] = ("plugin:voice", node.plugin("voice").serve_alias)
            try:
                async with http.get(base + "/health") as r:
                    assert "band_workers" in await r.json()
            finally:
                del node.site._exact["/health"]
            assert len(seen) == before
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


# -- review fixes ------------------------------------------------------------------------------

def test_without_rook_hub_serve_voice_stays_off_and_says_why(tmp_path, monkeypatch):
    # The two-process layout (rook-dashboard + rook-mcp): nothing hands /voice/
    # to the plugin, so it must not load, claim to serve, or load models.
    monkeypatch.setattr(site_mod, "_SERVED", False)
    monkeypatch.setattr(eng, "missing_extras", lambda: [])
    node = node_for(tmp_path, engine="remote", remote_url="http://192.0.2.7:8900")
    assert node.plugin("voice") is None
    st = node.host.status["voice"]
    assert st["state"] == "unavailable" and "rook hub serve" in st["reason"]
    page = voice_mod.page_data(node.settings, node, None)
    assert page["status"]["served"] is False
    assert page["requirements"]["served"] is False
    assert "rook hub serve" in page["requirements"]["reason"]


def test_credentials_in_the_voice_server_address_are_refused(tmp_path):
    r = voice_mod.requirements("remote", "http://bob:hunter2@192.0.2.7:8900", "", served=True)
    assert "user name or password" in r["reason"] and "hunter2" not in r["reason"]
    assert eng.RemoteEngine("http://bob:hunter2@192.0.2.7:8900", "/voice").host == "192.0.2.7:8900"


@pytest.mark.asyncio
async def test_stop_waits_for_a_model_load_in_progress(tmp_path, fake_engine, monkeypatch):
    gate, loading = asyncio.Event(), asyncio.Event()
    real = asyncio.to_thread

    async def slow(fn, *a, **k):
        if getattr(fn, "__name__", "") == "Provider":
            loading.set()
            await gate.wait()
        return await real(fn, *a, **k)
    monkeypatch.setattr(eng.asyncio, "to_thread", slow)
    engine = eng.LocalEngine("fakevoice", {"WHISPER_MODEL": "tiny.en"}, "/voice")
    engine.start()
    try:
        await asyncio.wait_for(loading.wait(), 5)
        stopping = asyncio.ensure_future(engine.stop())
        await asyncio.sleep(0.1)
        # The load is still running: stop waits instead of tearing down under it.
        assert not stopping.done()
        assert os.environ.get("WHISPER_MODEL") == "tiny.en" and "fakevoice.server" in sys.modules
        gate.set()
        await asyncio.wait_for(stopping, 5)
    finally:
        gate.set()
    # The stale load never started its app; only then was it all forgotten.
    assert engine.app is None and engine._lifespan is None and engine.state == "stopped"
    assert "WHISPER_MODEL" not in os.environ and "fakevoice.server" not in sys.modules


@web.middleware
async def own_host_only(request, handler):
    """A voice server behind a name-based proxy: any Host but its own is
    another site (cloudflared, Caddy and nginx answer 404/421)."""
    ip, port = request.transport.get_extra_info("sockname")[:2]
    if request.host != f"{ip}:{port}":
        return web.json_response({"error": "misdirected"}, status=421)
    return await handler(request)


@pytest.mark.asyncio
async def test_a_late_load_holds_the_next_engine_until_it_is_torn_down(
        tmp_path, fake_engine, monkeypatch):
    gate, loading = asyncio.Event(), asyncio.Event()
    real = asyncio.to_thread

    async def slow(fn, *a, **k):
        if getattr(fn, "__name__", "") == "Provider":
            loading.set()
            await gate.wait()
        return await real(fn, *a, **k)
    monkeypatch.setattr(eng.asyncio, "to_thread", slow)
    monkeypatch.setattr(eng, "BOOT_WAIT", 0.1)
    monkeypatch.setattr(eng, "_STALE", None)
    old = eng.LocalEngine("fakevoice", {"WHISPER_MODEL": "tiny.en"}, "/voice")
    old.start()
    new = None
    try:
        await asyncio.wait_for(loading.wait(), 5)
        await old.stop()                         # gives up waiting after BOOT_WAIT
        assert os.environ.get("WHISPER_MODEL") == "tiny.en"   # still the old load's
        new = eng.LocalEngine("fakevoice", {"WHISPER_MODEL": "base.en"}, "/voice")
        new.start()
        # It never starts on the old load's modules and environment, and says why.
        assert new.state == "waiting" and "still loading" in new.error
        assert (await new.health())["state"] == "waiting"
        gate.set()
        assert await wait_for(lambda: new.state == "ready"), new.error
        assert os.environ["WHISPER_MODEL"] == "base.en"
        assert new.app.state.provider.model == "base.en"
    finally:
        gate.set()
        if new is not None:
            await new.stop()
    assert "WHISPER_MODEL" not in os.environ and "fakevoice.server" not in sys.modules
    # Stopping an engine that is only waiting is immediate.
    gate.clear()
    loading.clear()
    old = eng.LocalEngine("fakevoice", {}, "/voice")
    old.start()
    await asyncio.wait_for(loading.wait(), 5)
    await old.stop()
    waiting = eng.LocalEngine("fakevoice", {}, "/voice")
    waiting.start()
    assert waiting.state == "waiting"
    await asyncio.wait_for(waiting.stop(), 1)
    assert waiting.state == "stopped"
    gate.set()
    await asyncio.wait_for(eng._STALE, 5)


def recording_voice_server(seen):
    async def anything(request):
        seen.append({"path": request.raw_path, "headers": dict(request.headers)})
        resp = web.json_response({"ok": True})
        resp.headers.add("Set-Cookie", "rook_account=from-voice; Path=/")
        resp.headers.add("Set-Cookie", "voice_admin=tok123; HttpOnly; Path=/admin; "
                                       "Domain=voice.example; Secure; SameSite=Strict")
        return resp
    app = web.Application(middlewares=[own_host_only])
    app.router.add_route("*", "/{tail:.*}", anything)
    return app


def proxy_app(remote):
    app = web.Application()

    async def handler(request):
        return await remote.handle(request)

    async def alias(request):
        return await remote.handle(request, alias=True)
    app.router.add_route("*", "/voice/{tail:.*}", handler)
    for path in voice_mod.ALIASES:
        app.router.add_route("*", path, alias)
    return app


@pytest.mark.asyncio
async def test_remote_keeps_the_dashboards_credentials_and_bad_paths_on_the_hub():
    from yarl import URL
    seen: list = []
    upstream = TestServer(recording_voice_server(seen))
    await upstream.start_server()
    remote = eng.RemoteEngine(str(upstream.make_url("")).rstrip("/"), "/voice")
    try:
        async with TestClient(TestServer(proxy_app(remote)),
                              cookie_jar=aiohttp.DummyCookieJar()) as http:
            r = await http.get("/voice/a%20b?x=1", headers={
                "Host": "hub.example", "Authorization": "Bearer voice-key",
                "Cookie": "rook_account=SECRET; voice_admin=abc; other=1",
                "X-Forwarded-For": "198.51.100.7",       # what a local tunnel says
                "Cf-Access-Jwt-Assertion": "jwt", "Cf-Access-Authenticated-User-Email": "a@b"})
            assert r.status == 200, await r.text()
            got = seen[-1]
            assert got["path"] == "/a%20b?x=1"                     # as the client encoded it
            hdr = {k.lower(): v for k, v in got["headers"].items()}
            assert hdr["cookie"] == "voice_admin=abc"              # never the dashboard session
            assert not [k for k in hdr if k.startswith("cf-access-")]
            # The voice server's own Host (its proxy routes on it); the hub's
            # name goes as X-Forwarded-Host.
            assert hdr["host"] == remote.upstream_host() and hdr["authorization"] == "Bearer voice-key"
            assert hdr["x-forwarded-host"] == "hub.example"
            assert hdr["x-forwarded-prefix"] == "/voice"
            # The tunnel's client, without this hub's loopback hop.
            assert hdr["x-forwarded-for"] == "198.51.100.7"
            # The Android app's root paths reach it too.
            for path in voice_mod.ALIASES:
                async with http.post(path, data=b"\0\0", headers={"Host": "hub.example"}) as r:
                    assert r.status == 200, path
                assert seen[-1]["path"] == path
            # Only the voice admin cookie comes back, under /voice, for this host.
            cookies = r.headers.getall("Set-Cookie")
            assert len(cookies) == 1 and cookies[0].startswith("voice_admin=tok123")
            assert "Path=/voice/admin" in cookies[0] and "Domain" not in cookies[0]
            assert "HttpOnly" in cookies[0] and "Secure" in cookies[0]
            # Dot segments, plain or encoded, are refused, never forwarded.
            n = len(seen)
            root = str(http.make_url("/")).rstrip("/")
            for bad in ("/voice/../escape", "/voice/%2e%2e/escape", "/voice/a%2F..%2F..%2Fescape",
                        "/voice/./health"):
                async with http.session.get(URL(root + bad, encoded=True)) as r:
                    assert r.status == 400, bad
            assert len(seen) == n
    finally:
        await remote.stop()
        await upstream.close()


@pytest.mark.asyncio
async def test_voice_admin_signs_in_through_the_hub(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from rook.hub.asgi_bridge import AsgiBridge
    from services.voice.admin import AdminStore, router
    store = AdminStore(tmp_path / "admin.sqlite3")
    store.bootstrap("admin", "test-password")

    async def invalidate(key):
        return None
    api = FastAPI()
    api.include_router(router(store, {}, invalidate))
    bridge = AsgiBridge(api)
    upapp = web.Application(middlewares=[own_host_only])
    upapp.router.add_route("*", "/{tail:.*}", bridge.handle)
    upstream = TestServer(upapp)
    await upstream.start_server()
    remote = eng.RemoteEngine(str(upstream.make_url("")).rstrip("/"), "/voice")
    try:
        async with TestClient(TestServer(proxy_app(remote)),
                              cookie_jar=aiohttp.DummyCookieJar()) as http:
            hdr = {"Host": "hub.example", "Origin": "https://hub.example", "X-Voice-Admin": "1",
                   "Cookie": "rook_account=SECRET"}
            r = await http.post("/voice/admin/api/login", headers=hdr,
                                json={"username": "admin", "password": "test-password"})
            assert r.status == 200, await r.text()
            set_cookie = r.headers["Set-Cookie"]
            assert "Path=/voice/admin" in set_cookie
            token = set_cookie.split(";")[0].split("=", 1)[1]
            r = await http.get("/voice/admin/api/session", headers={
                "Host": "hub.example", "Cookie": f"rook_account=SECRET; voice_admin={token}"})
            assert r.status == 200 and (await r.json())["csrf"]
            # Another site's page still cannot sign in, nor a plain-http origin.
            for origin in ("https://evil.example", "http://hub.example"):
                r = await http.post("/voice/admin/api/login", headers={**hdr, "Origin": origin},
                                    json={"username": "admin", "password": "test-password"})
                assert r.status == 403, origin
            # Failed sign-ins count per client (the tunnel's address), not
            # per hub: one client locking itself out leaves the others be.
            bad = {"username": "admin", "password": "wrong"}
            for _ in range(10):
                r = await http.post("/voice/admin/api/login", json=bad,
                                    headers={**hdr, "X-Forwarded-For": "198.51.100.7"})
                assert r.status == 401
            r = await http.post("/voice/admin/api/login", json=bad,
                                headers={**hdr, "X-Forwarded-For": "198.51.100.7"})
            assert r.status == 429
            r = await http.post("/voice/admin/api/login",
                                headers={**hdr, "X-Forwarded-For": "198.51.100.8"},
                                json={"username": "admin", "password": "test-password"})
            assert r.status == 200
    finally:
        await remote.stop()
        await upstream.close()


@pytest.mark.asyncio
async def test_remote_websockets_are_capped_per_client(monkeypatch):
    monkeypatch.setattr(eng, "WS_PER_CLIENT", 1)
    seen: list = []
    upstream = TestServer(fake_voice_server(seen))
    await upstream.start_server()
    remote = eng.RemoteEngine(str(upstream.make_url("")).rstrip("/"), "/voice")
    try:
        async with TestClient(TestServer(proxy_app(remote))) as http:
            first = await http.ws_connect("/voice/ws")
            await first.send_str("hi")
            assert (await first.receive(timeout=5)).data == "echo:hi"
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await http.ws_connect("/voice/ws")
            assert refused.value.status == 429
            # Plain requests still go through while the WebSocket is open.
            assert (await http.get("/voice/health")).status == 200
            await first.close()
            assert await wait_for(lambda: not remote._ws_open)
            again = await http.ws_connect("/voice/ws")
            await again.close()
    finally:
        await remote.stop()
        await upstream.close()


# -- the GPU voice server's configuration, from Settings > Voice ---------------------------

#: key -> (the variable services/voice reads, a value an operator stores, what the
#: service then gets in its environment).
GPU_SETTINGS = {
    "tts_default": ("VOICE_TTS_DEFAULT", "chatterbox:sojourn", "chatterbox:sojourn"),
    "tts_target_dbfs": ("VOICE_TTS_TARGET_DBFS", -18.5, "-18.5"),
    "tts_threads": ("VOICE_TTS_THREADS", 4, "4"),
    "chatterbox_device": ("VOICE_CHATTERBOX_DEVICE", "cuda:0", "cuda:0"),
    "chatterbox_python": ("VOICE_CHATTERBOX_PYTHON", "/opt/chatterbox/.venv/bin/python",
                          "/opt/chatterbox/.venv/bin/python"),
    "chatterbox_voices_dir": ("VOICE_CHATTERBOX_VOICES_DIR", "/srv/voices", "/srv/voices"),
    "chatterbox_start_timeout_s": ("VOICE_CHATTERBOX_START_TIMEOUT_S", 240, "240.0"),
    "tool_reasoning_effort": ("VOICE_TOOL_REASONING_EFFORT", "medium", "medium"),
    "tool_max_tokens": ("VOICE_TOOL_MAX_TOKENS", 2048, "2048"),
    "tool_max_steps": ("VOICE_TOOL_MAX_STEPS", 12, "12"),
    "tool_model_timeout_s": ("VOICE_TOOL_MODEL_TIMEOUT_S", 90, "90.0"),
    "front_url": ("VOICE_FRONT_URL", "http://127.0.0.1:1235/v1/chat/completions",
                  "http://127.0.0.1:1235/v1/chat/completions"),
    "front_model": ("VOICE_FRONT_MODEL", "front-small", "front-small"),
    "front_timeout_s": ("VOICE_FRONT_TIMEOUT_S", 20, "20.0"),
    "front_max_tokens": ("VOICE_FRONT_MAX_TOKENS", 150, "150"),
    "front_turns": ("VOICE_FRONT_TURNS", 4, "4"),
    "background_url": ("VOICE_BACKGROUND_URL", "http://127.0.0.1:1236/v1/chat/completions",
                       "http://127.0.0.1:1236/v1/chat/completions"),
    "background_model": ("VOICE_BACKGROUND_MODEL", "background-big", "background-big"),
    "background_effort": ("VOICE_BACKGROUND_EFFORT", "high", "high"),
    "background_max_steps": ("VOICE_BACKGROUND_MAX_STEPS", 6, "6"),
    "background_timeout_s": ("VOICE_BACKGROUND_TIMEOUT_S", 60, "60.0"),
    "primary_url": ("LLM_PRIMARY_URL", "https://llm.example.com/v1/chat/completions",
                    "https://llm.example.com/v1/chat/completions"),
    "primary_model": ("LLM_PRIMARY_MODEL", "hosted-model", "hosted-model"),
    "primary_timeout_s": ("LLM_PRIMARY_TIMEOUT_S", 5, "5.0"),
    "hass_url": ("VOICE_HASS_URL", "https://homeassistant.example.com:8123",
                 "https://homeassistant.example.com:8123"),
    "hass_verify_tls": ("VOICE_HASS_VERIFY_TLS", False, "0"),
    "pianobar_worker": ("VOICE_PIANOBAR_WORKER", "music-box", "music-box"),
    "home_lat": ("VOICE_HOME_LAT", 51.5, "51.5"),
    "home_lon": ("VOICE_HOME_LON", -0.12, "-0.12"),
    "timezone": ("VOICE_TZ", "Europe/London", "Europe/London"),
    "weather_units": ("VOICE_WEATHER_UNITS", "fahrenheit", "fahrenheit"),
    "weather_url": ("VOICE_WEATHER_URL", "https://weather.example.com/v1/forecast",
                    "https://weather.example.com/v1/forecast"),
}
#: Secrets: from the vault, never the settings database.
GPU_SECRETS = {"hass_token": "VOICE_HASS_TOKEN", "primary_api_key": "LLM_PRIMARY_API_KEY"}


class DictVault:
    def __init__(self, data):
        self.data = dict(data)

    def get(self, name, actor, via="get", task=None):
        return self.data[name]


def gpu_node(tmp_path, vault=None, **stored):
    store = SettingsStore(tmp_path / "settings.db")
    for key, value in stored.items():
        store.set(f"voice.{key}", "hub", "", value=value, actor="test")
    return HubNode(str(tmp_path), entry_points=False, settings_store=store, vault=vault)


def secret_values():
    return {name: f"{name}-value-for-tests" for name in GPU_SECRETS}


def test_gpu_server_settings_are_handed_to_the_voice_service(tmp_path, fake_engine):
    stored = {key: value for key, (_env, value, _out) in GPU_SETTINGS.items()}
    vault = DictVault({f"plugin.voice.{k}": v for k, v in secret_values().items()})
    node = gpu_node(tmp_path, vault, model_dir=str(fake_engine.models), **stored)
    voice = node.plugin("voice")
    assert voice is not None, node.host.status["voice"]
    env = voice.engine_env()
    for key, (var, _value, out) in GPU_SETTINGS.items():
        assert env.get(var) == out, key
    for key, var in GPU_SECRETS.items():
        assert env.get(var) == secret_values()[key], key
    # Nothing stored: none of them is set, so the service keeps its own defaults.
    bare = tmp_path / "bare"
    bare.mkdir()
    env = gpu_node(bare, model_dir=str(fake_engine.models)).plugin("voice").engine_env()
    assert not {v for v, _x, _y in GPU_SETTINGS.values()} & set(env)
    assert not set(GPU_SECRETS.values()) & set(env)


@pytest.mark.asyncio
async def test_engine_here_exports_them_and_the_hubs_environment_wins(
        tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("VOICE_TTS_THREADS", "2")
    vault = DictVault({"plugin.voice.hass_token": "ha-token-for-tests"})
    node = gpu_node(tmp_path, vault, model_dir=str(fake_engine.models),
                    hass_url="https://homeassistant.example.com:8123",
                    chatterbox_device="cuda:0", tts_threads=4, home_lat=51.5)
    voice = node.plugin("voice")
    await node.start()
    try:
        assert await wait_for(lambda: voice.engine.state == "ready"), voice.engine.error
        assert os.environ["VOICE_HASS_URL"] == "https://homeassistant.example.com:8123"
        assert os.environ["VOICE_HASS_TOKEN"] == "ha-token-for-tests"
        assert os.environ["VOICE_CHATTERBOX_DEVICE"] == "cuda:0"
        assert os.environ["VOICE_HOME_LAT"] == "51.5"
        assert os.environ["VOICE_TTS_THREADS"] == "2"       # the hub's own, untouched
        assert "VOICE_CHATTERBOX_PYTHON" not in os.environ  # not stored: the service default
    finally:
        await node.stop()
    for var in ("VOICE_HASS_URL", "VOICE_HASS_TOKEN", "VOICE_CHATTERBOX_DEVICE", "VOICE_HOME_LAT"):
        assert var not in os.environ
    assert os.environ["VOICE_TTS_THREADS"] == "2"


def test_a_value_from_another_variable_reaches_the_service_under_its_own(
        tmp_path, fake_engine, monkeypatch):
    # The automatic name and an alias-like other name: handed on under the
    # variable the service reads (a stored value would lose to them anyway).
    monkeypatch.setenv("ROOK_VOICE_HASS_URL", "https://ha.example.com:8123")
    monkeypatch.setenv("ROOK_VOICE_HASS_TOKEN", "ha-token-from-env")
    monkeypatch.setenv("ROOK_VOICE_TTS_THREADS", "3")
    monkeypatch.setenv("ROOK_VOICE_HASS_VERIFY_TLS", "off")
    monkeypatch.setenv("ROOK_VOICE_HOME_LAT", "north")            # invalid: left out
    monkeypatch.setenv("VOICE_PIANOBAR_WORKER", "music-box")      # the service's own name
    monkeypatch.setenv("ROOK_VOICE_PIANOBAR_WORKER", "other-box")
    node = gpu_node(tmp_path, DictVault({}), model_dir=str(fake_engine.models),
                    hass_url="https://stored.example.com:8123", tts_threads=6)
    voice = node.plugin("voice")
    assert voice.settings.source("hass_url") == "env"
    env = voice.engine_env()
    assert env["VOICE_HASS_URL"] == "https://ha.example.com:8123"
    assert env["VOICE_HASS_TOKEN"] == "ha-token-from-env"
    assert env["VOICE_TTS_THREADS"] == "3"
    assert env["VOICE_HASS_VERIFY_TLS"] == "0"
    assert "VOICE_HOME_LAT" not in env
    # Already under the service's own name: nothing to hand on, and it wins.
    assert "VOICE_PIANOBAR_WORKER" not in env
    # Only the service's variable gets the value: no other name carries the secret.
    assert [k for k, v in env.items() if v == "ha-token-from-env"] == ["VOICE_HASS_TOKEN"]


@pytest.mark.asyncio
async def test_engine_here_hands_on_rook_voice_names_and_the_services_own_wins(
        tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("ROOK_VOICE_HASS_URL", "https://ha.example.com:8123")
    monkeypatch.setenv("ROOK_VOICE_CHATTERBOX_DEVICE", "cuda:1")
    monkeypatch.setenv("VOICE_CHATTERBOX_DEVICE", "cuda:0")
    node = gpu_node(tmp_path, model_dir=str(fake_engine.models))
    voice = node.plugin("voice")
    await node.start()
    try:
        assert await wait_for(lambda: voice.engine.state == "ready"), voice.engine.error
        assert os.environ["VOICE_HASS_URL"] == "https://ha.example.com:8123"
        assert os.environ["VOICE_CHATTERBOX_DEVICE"] == "cuda:0"
    finally:
        await node.stop()
    assert "VOICE_HASS_URL" not in os.environ
    assert os.environ["VOICE_CHATTERBOX_DEVICE"] == "cuda:0"
    assert os.environ["ROOK_VOICE_HASS_URL"] == "https://ha.example.com:8123"


@pytest.mark.asyncio
async def test_engine_remote_never_exports_them(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "missing_extras", lambda: ["webrtcvad"])
    vault = DictVault({"plugin.voice.hass_token": "ha-token-for-tests"})
    node = gpu_node(tmp_path, vault, engine="remote", remote_url="http://192.0.2.7:8900",
                    hass_url="https://homeassistant.example.com:8123", chatterbox_device="cuda:0")
    voice = node.plugin("voice")
    assert voice is not None, node.host.status["voice"]
    await node.start()
    try:
        assert isinstance(voice.engine, eng.RemoteEngine)
        for var in ("VOICE_HASS_URL", "VOICE_HASS_TOKEN", "VOICE_CHATTERBOX_DEVICE",
                    "VOICE_MODEL_DIR"):
            assert var not in os.environ
    finally:
        await node.stop()


def test_gpu_secrets_are_masked_and_kept_in_the_vault(tmp_path):
    import json
    from tests.test_settings import FakeVault, _svc
    vault = FakeVault()
    svc = _svc(tmp_path, vault=vault)
    for key, var in GPU_SECRETS.items():
        entry = svc.schema.get(f"voice.{key}")
        assert entry.setting.secret and var in entry.env_names(), key
        secret = f"{key}-value-for-tests"
        res = svc.set(f"voice.{key}", secret, actor="human:op")
        assert vault.data[f"plugin.voice.{key}"] == secret
        assert secret not in json.dumps(res)
        row = svc.store.get(f"voice.{key}", "hub")
        assert row["value"] is None and row["secret_ref"] == f"plugin.voice.{key}"
        assert svc.store.history(key=f"voice.{key}")[0]["new"].startswith("fp:")
        assert secret not in json.dumps(svc.plugin_page("voice"))
        assert secret not in json.dumps(svc.resolve(f"voice.{key}"))
        assert secret not in (tmp_path / "settings.db").read_bytes().decode("latin-1")


def _service_defaults():
    """{variable: {defaults}} from every ``<x>.get("NAME", "default")`` in services/voice."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "services" / "voice"
    out: dict = {}
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and node.args[0].value.isupper()):
                continue
            default = node.args[1] if len(node.args) > 1 else None
            out.setdefault(node.args[0].value, set()).add(
                default.value if isinstance(default, ast.Constant) else None)
    return out


#: Hub defaults that differ from the service's on purpose: the model folder is
#: always the hub's (plugin data folder).
DEFAULTS_DIFFER = {"VOICE_MODEL_DIR"}


def test_every_voice_variable_is_read_by_the_service_with_the_same_default():
    found = _service_defaults()
    for s in voice_mod.SETTINGS:
        if not s.env:
            continue
        assert s.env in found, f"{s.name}: services/voice never reads {s.env}"
        if s.env in DEFAULTS_DIFFER:
            continue
        for raw in found[s.env] - {None}:
            # A blank variable is "unset" to the service: no default on the hub.
            want = None if raw == "" else s.coerce(raw)
            have = None if s.default == "" else s.default
            assert have == want, f"{s.name}: hub default {s.default!r}, service default {raw!r}"
    for key, (var, _v, _o) in GPU_SETTINGS.items():
        assert next(s for s in voice_mod.SETTINGS if s.name == key).env == var, key
