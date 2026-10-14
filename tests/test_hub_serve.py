"""rook hub serve: the dashboard and the MCP server in one process."""
import asyncio
import json
import socket
from types import SimpleNamespace

import aiohttp
import pytest

from rook.hub import serve as hub_serve
from rook.hub.serve import DashboardBand, Hub, is_mcp_path, parse_args
from rook.remote import upstream

PSK = "alpha-bravo-charlie-delta-echo-foxtrot"
TOKEN = "static-token-for-tests-0123456789"
ACCEPT = {"Accept": "application/json, text/event-stream"}


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoRelay(asyncio.DatagramProtocol):
    """Stands in for telesthete-hub: echoes datagrams that start with PING
    (what the test sends through /band), ignores band traffic."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.startswith(b"PING"):
            self.transport.sendto(b"PONG" + data[4:], addr)


def test_public_mcp_paths_match_the_documented_proxy_split():
    for p in ("/mcp", "/mcp/", "/band", "/tokens/account-api", "/authorize", "/token",
              "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
              "/skill/rook.skill", "/healthz"):
        assert is_mcp_path(p), p
    for p in ("/", "/tokens", "/health", "/account/tokens/api", "/knowledge/account-api",
              "/internal/mask", "/mcpx", "/bands", "/install", "/api/band/call"):
        assert not is_mcp_path(p), p


def test_options_merge_both_processes(monkeypatch):
    for var in ("ROOK_HUB", "ROOK_HUB_HOST", "ROOK_HUB_PORT", "ROOK_MCP_BIND", "ROOK_BAND_PSK",
                "ROOK_MCP_MAX_BODY"):
        monkeypatch.delenv(var, raising=False)
    _, a = parse_args([])
    assert (a.hub_host, a.hub_port) == ("127.0.0.1", 7474)
    assert (a.mcp_bind_host, a.mcp_bind_port) == ("127.0.0.1", 8765)
    assert (a.bind, a.port) == ("0.0.0.0", 7005) and a.flag_settings == {}
    assert a.mcp_max_body == 16 * 1024 * 1024
    # An MCP unit's environment carried over as is.
    monkeypatch.setenv("ROOK_HUB", "relay:7575")
    monkeypatch.setenv("ROOK_MCP_BIND", "off")
    _, a = parse_args([])
    assert (a.hub_host, a.hub_port) == ("relay", 7575) and a.mcp_bind_host is None
    # The dashboard's spelling wins over rook-mcp's.
    _, a = parse_args(["--hub-host", "10.0.0.2", "--mcp-bind", "0.0.0.0:9000", "--psk", "a,b"])
    assert (a.hub_host, a.hub_port) == ("10.0.0.2", 7575)
    assert (a.mcp_bind_host, a.mcp_bind_port) == ("0.0.0.0", 9000)
    assert a.psks == ["a", "b"] and a.band_psk == "a"
    assert a.flag_settings["core.mcp.listen"] == ("--mcp-bind", "0.0.0.0:9000")
    assert a.flag_settings["core.band.key"] == ("--psk", "a")


@pytest.mark.asyncio
async def test_dashboard_calls_to_the_hub_node_keep_the_band_path():
    """In two processes a dashboard call to worker `rook` crossed the band
    (risk ceiling, band-unauthenticated check, journaled); in one it must not
    become a privileged in-process call."""
    seen = []

    class Node:
        worker_id = "hubnode"

        def entry(self):
            return {"name": "rook", "roles": ["is_hub"]}

        async def dispatch(self, cap, args, identity, source="local"):
            seen.append((cap, args, identity, source))
            return {"ok": True, "result": "done"}

    class Client:
        _local = Node()
        authz = None
        workers = {"w1": {"name": "w1"}}

        async def call(self, **kw):
            seen.append(("remote", kw["target"]))
            return {"ok": True}

    view = DashboardBand(Client())
    assert view.workers == {"w1": {"name": "w1"}}
    reply = await view.call("hub.info", {"x": 1}, target="hubnode", identity="human:dashboard")
    assert reply["ok"] and reply["from"] == "hubnode"
    assert seen[-1] == ("hub.info", {"x": 1}, "human:dashboard", "band")
    await view.call("sys.info", target="w1")
    assert seen[-1] == ("remote", "w1")
    view.authz = "set through"           # attribute writes reach the shared client
    assert Client.authz == "set through" or view._client.authz == "set through"


async def _mcp(http, base, token=TOKEN):
    auth = {"Authorization": "Bearer " + token, **ACCEPT}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "1"}}}
    async with http.post(base + "/mcp", json=init, headers=auth) as r:
        if r.status != 200:
            return r.status, None
        sid = r.headers["mcp-session-id"]
        await r.read()
    hdr = {**auth, "mcp-session-id": sid}
    async with http.post(base + "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                         headers=hdr) as r:
        assert r.status == 202
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "rook_whoami", "arguments": {}}}
    async with http.post(base + "/mcp", json=call, headers=hdr) as r:
        text = await r.text()
    data = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
    if not data:
        data = [json.loads(text)]
    async with http.delete(base + "/mcp", headers=hdr) as r:
        pass
    return 200, json.loads(data[0]["result"]["content"][0]["text"])


@pytest.fixture
def hub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    for var in ("ROOK_HUB", "ROOK_HUB_HOST", "ROOK_HUB_PORT", "ROOK_MCP_BIND", "ROOK_CHAT_DB",
                "ROOK_KNOWLEDGE_ADMIN_URL", "ROOK_TOKEN_ADMIN_URL", "ROOK_GUIDANCE_ADMIN_URL",
                "ROOK_MASK_URL", "ROOK_MCP_PERSIST", "ROOK_MCP_STATIC_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    port, mcp_port, relay = free_port(), free_port(), free_port(socket.SOCK_DGRAM)
    argv = ["--bind", "127.0.0.1", "--port", str(port), "--mcp-bind", f"127.0.0.1:{mcp_port}",
            "--hub-port", str(relay), "--web-pass", "pw", "--psk", PSK,
            "--static-token", TOKEN, "--domain", f"http://127.0.0.1:{port}"]
    return SimpleNamespace(argv=argv, port=port, mcp_port=mcp_port, relay=relay,
                           base=f"http://127.0.0.1:{port}", mcp_base=f"http://127.0.0.1:{mcp_port}")


@pytest.mark.asyncio
async def test_merged_hub_end_to_end(hub_env, monkeypatch):
    from rook.band_mcp import client as band_client
    from rook.remote.bootstrap import prepare
    starts = []
    real_start = band_client.MultiBandClient.start

    async def counting_start(self):
        starts.append(self)
        return await real_start(self)
    monkeypatch.setattr(band_client.MultiBandClient, "start", counting_start)

    loop = asyncio.get_running_loop()
    relay, _ = await loop.create_datagram_endpoint(EchoRelay, local_addr=("127.0.0.1", hub_env.relay))
    ap, args = parse_args(hub_env.argv)
    prepare(ap, args)
    hub = Hub(args, hub_env.argv)
    await hub.start()
    try:
        # One of each, shared by both halves.
        assert len(starts) == 1 and starts[0] is hub.client
        assert hub.server._band._client is hub.client
        assert hub.server._enrollment is hub.mcp.enrollment
        assert hub.mcp.accounts is hub.server._accounts.store
        assert hub.server._chat is hub.mcp.mcp._rook_chat
        assert hub.server._settings_store is hub.mcp.mcp._rook_hub.settings_store
        assert hub.server._authz is hub.client.authz is not None
        assert hub.client.authz._banned == hub.server._ban_match
        assert hub.server._enrollment_task is not None and hub.mcp._pump is not None

        async with aiohttp.ClientSession() as http:
            # Dashboard paths stay the dashboard's.
            async with http.get(hub_env.base + "/health") as r:
                assert r.status == 200 and "band_workers" in await r.json()
            async with http.get(hub_env.base + "/guidance/account-api") as r:
                assert r.status == 401 and r.headers.get("WWW-Authenticate", "").startswith("Basic")
            # MCP on the dashboard port and on the old MCP port.
            for base in (hub_env.base, hub_env.mcp_base):
                status, who = await _mcp(http, base)
                assert status == 200 and who["kind"] == "shared" and who["verified"]
                status, _ = await _mcp(http, base, token="wrong-token-0123456789")
                assert status == 401
            async with http.get(hub_env.mcp_base + "/") as r:
                assert r.status == 404   # the old port serves only the MCP app
            async with http.get(hub_env.base + "/healthz",
                                headers={"Authorization": "Bearer " + TOKEN}) as r:
                assert (await r.json())["ok"] is True
            async with http.get(hub_env.base + "/.well-known/oauth-protected-resource/mcp") as r:
                assert r.status == 200

            # /account/<x>/api reaches the MCP side in memory, with the same
            # session, operator and CSRF checks.
            def no_http(*a, **k):
                raise AssertionError("went over HTTP")
            monkeypatch.setattr(upstream.aiohttp, "ClientSession", no_http)
            store = hub.server._accounts.store
            tok = store.new_session(hub.server._accounts.bootstrap_id)
            csrf = store.session(tok)["csrf"]
            hdr = {"Cookie": "rook_account=" + tok, "Origin": hub_env.base}
            async with http.get(hub_env.base + "/account/guidance/api", headers=hdr) as r:
                assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
            async with http.post(hub_env.base + "/account/tokens/api", headers=hdr,
                                 json={"op": "create", "name": "merged hub test"}) as r:
                assert r.status == 403   # no CSRF token
            async with http.post(hub_env.base + "/account/tokens/api", headers=hdr,
                                 json={"op": "create", "name": "merged hub test", "csrf": csrf}) as r:
                assert r.status == 200, await r.text()
                minted = (await r.json())["token"]
            status, who = await _mcp(http, hub_env.base, token=minted)
            assert status == 200 and who["verified"] and who["kind"] != "shared"
            member = store.create_local("member", "a long enough test password")
            async with http.get(hub_env.base + "/account/tokens/api",
                                headers={"Cookie": "rook_account=" + store.new_session(member)}) as r:
                assert r.status == 403

            # /band on the dashboard port bridges a WebSocket to the relay.
            async with http.ws_connect(hub_env.base.replace("http", "ws") + "/band") as ws:
                await ws.send_bytes(b"PING-1")
                assert await ws.receive_bytes(timeout=5) == b"PONG-1"
    finally:
        await hub.stop()
        relay.close()
    # Everything stopped: ports closed, no in-process upstream left behind.
    assert upstream.local_app(f"http://127.0.0.1:{hub_env.mcp_port}/x") is None
    for port in (hub_env.port, hub_env.mcp_port):
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_without_the_old_mcp_port(hub_env, monkeypatch):
    from rook.remote.bootstrap import prepare
    argv = [a for a in hub_env.argv]
    argv[argv.index("--mcp-bind") + 1] = "off"
    # Windows: no SIGUSR1 (AttributeError) must not stop the hub from starting.
    from rook.band_mcp import memory_diagnostics

    def no_sigusr():
        raise AttributeError("module 'signal' has no attribute 'SIGUSR1'")
    monkeypatch.setattr(memory_diagnostics, "install", no_sigusr)
    ap, args = parse_args(argv)
    prepare(ap, args)
    hub = Hub(args, argv)
    await hub.start()
    try:
        # The default ROOK_*_ADMIN_URL port still resolves in memory.
        assert upstream.local_app("http://127.0.0.1:8765/guidance/account-api") is hub.mcp.app
        async with aiohttp.ClientSession() as http:
            status, _ = await _mcp(http, hub_env.base)
            assert status == 200
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", hub_env.mcp_port)
    finally:
        await hub.stop()


def test_rook_hub_serve_is_wired(monkeypatch):
    import sys
    from rook import __main__ as cli
    from rook.hubdeploy import cli as deploy_cli
    got = []
    monkeypatch.setattr(hub_serve, "main", lambda argv=None: got.append(argv))
    monkeypatch.setattr(sys, "argv", ["rook", "hub", "serve", "--port", "1"])
    cli.main()
    assert deploy_cli.main(["serve", "--port", "2"]) == 0
    assert got == [["--port", "1"], ["--port", "2"]]


def test_hub_deploy_knows_the_single_hub_service(tmp_path):
    from rook.hubdeploy import config as cfgmod
    from rook.hubdeploy import deploy as dp
    cfg = cfgmod.from_dict({"root": str(tmp_path), "python": "/opt/rook-hub/venv/bin/python",
                            "services": {"hub": {"health": ["http://127.0.0.1:7005/health"]}}})
    (svc,) = cfg.ordered()
    assert svc.unit == "rook-hub.service" and svc.disruptive
    assert svc.modules[0] == "rook.hub.serve" and "rook.band_mcp" in svc.packages
    assert "-m rook.hub.serve" in dp.units_text(cfg)
    # Without a services block it is still the two-process layout.
    cfg = cfgmod.from_dict({"root": str(tmp_path), "python": "/usr/bin/python3"})
    assert [s.name for s in cfg.ordered()] == ["dashboard", "mcp"]


@pytest.mark.asyncio
async def test_compat_rook_mcp_still_serves_alone(hub_env):
    """rook-mcp (two-process layout) runs McpService under uvicorn as before."""
    import argparse
    from rook.band_mcp import server as mcp_server
    from rook.paths import data_path
    args = argparse.Namespace(
        hub_host="127.0.0.1", hub_port=hub_env.relay, bind_host="127.0.0.1",
        bind_port=hub_env.mcp_port, psks=[PSK], allowed_hosts="", public_url="",
        persist_path=data_path("oauth.json", "/nonexistent/oauth.json"),
        static_token=TOKEN, journal_path="", flag_settings={})
    task = asyncio.create_task(mcp_server._amain(args))
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(100):
                try:
                    async with http.get(hub_env.mcp_base + "/healthz",
                                        headers={"Authorization": "Bearer " + TOKEN}) as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.1)
            status, who = await _mcp(http, hub_env.mcp_base)
            assert status == 200 and who["verified"]
            async with http.get(hub_env.mcp_base + "/health") as r:
                assert r.status == 404     # no dashboard in this process
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_stops_accepting_first_and_survives_a_failing_step():
    order = []

    class Site:
        def __init__(self, name):
            self.name = name

        async def stop(self):
            order.append("listen:" + self.name)

    class Runner:
        def __init__(self, name):
            self.name, self.sites = name, [Site(name)]

        async def cleanup(self):
            order.append("cleanup:" + self.name)

    class Rec:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def stop(self):
            order.append(self.name)
            if self.fail:
                raise RuntimeError(self.name + " broke")

    hub = Hub(SimpleNamespace(), [])
    hub._mcp_runner = Runner("mcp-port")
    hub.server = Rec("dashboard")
    hub.server._runner = Runner("main-port")
    hub.bridge = SimpleNamespace(stopping=False)
    hub.mcp = Rec("mcp-service")
    hub.mcp.ws_bridge = Rec("ws-peers", fail=True)
    hub.lifespan = Rec("lifespan", fail=True)
    hub.client = Rec("band-client")
    hub._unregister = lambda: order.append("unregister")
    await hub.stop()
    assert order[:2] == ["listen:mcp-port", "listen:main-port"]
    assert hub.bridge.stopping
    assert order[2:] == ["ws-peers", "lifespan", "cleanup:mcp-port", "dashboard",
                         "unregister", "mcp-service", "band-client"]
