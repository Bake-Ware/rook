"""rook.kit.actions.run_action against real subprocesses and tiny local
http/tcp/ws servers."""

import asyncio
import base64
import datetime
import functools
import json
import os
import ssl
import sys
import time

import pytest
import pytest_asyncio
from aiohttp import web

from rook.kit.actions import ActionError, Secret, Unavailable, run_action
from rook.kit.actions import engine

PY = sys.executable
LINUX = {"os": "linux", "arch": "x86_64", "name": "w1"}


def py(code):
    """argv running a python snippet; the snippet sees the rest in sys.argv[1:]."""
    return [PY, "-c", code]


ECHO_ARGV = "import json,sys; print(json.dumps(sys.argv[1:]))"


def hard(fn, seconds=20):
    """An asyncio test that can never hang CI: it fails after ``seconds``."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        async with asyncio.timeout(seconds):
            return await fn(*a, **kw)
    return pytest.mark.asyncio(wrapper)


async def run(action, args=None, **kw):
    kw.setdefault("worker", LINUX)
    return await run_action(action, args or {}, **kw)


# --- dispatch -------------------------------------------------------------------

@hard
async def test_unknown_type_is_unavailable_not_a_crash():
    with pytest.raises(Unavailable, match="'serial' isn't supported"):
        await run({"serial": {"port": "/dev/ttyUSB0"}})
    with pytest.raises(ActionError, match="no type"):
        await run({"parse": "text"})
    with pytest.raises(ActionError, match="more than one type"):
        await run({"exec": {"argv": ["x"]}, "http": {"url": "http://x"}})


# --- exec -----------------------------------------------------------------------

@hard
async def test_exec_argv_groups_parse_and_returns():
    action = {"exec": {"argv": py(ECHO_ARGV) + ["info", ["-s", "{{args.state}}"], ["-n", "{{args.n}}"],
                                                "{{args.evil}}"]},
              "parse": "json"}
    out = await run(action, {"state": "Seeding", "evil": "$(id); `x` | y"})
    assert out == ["info", "-s", "Seeding", "$(id); `x` | y"]


@hard
async def test_exec_stdin_env_cwd_and_default_cwd_is_tmp(tmp_path):
    code = ("import os,sys,json; print(json.dumps({'in': sys.stdin.read(), 'lang': os.environ.get('LANG'),"
            " 'cwd': os.getcwd(), 'home': 'HOME' in os.environ}))")
    action = {"exec": {"argv": py(code), "stdin": "on {{args.n}}", "env": {"LANG": "C"}}, "parse": "json",
              "returns": ["in", "lang", "cwd", "home"]}
    out = await run(action, {"n": 0}, tmp=str(tmp_path))
    assert out == {"in": "on 0", "lang": "C", "cwd": str(tmp_path), "home": True}
    sub = tmp_path / "data"
    sub.mkdir()
    action["exec"]["cwd"] = "{{plugin.data}}"
    assert (await run(action, {"n": 1}, plugin_data=str(sub)))["cwd"] == str(sub)


@hard
async def test_exec_bad_exit_fails_with_stderr_and_redacts_secrets():
    code = "import sys; sys.stderr.write('auth failed for ' + sys.argv[1]); sys.exit(3)"
    action = {"exec": {"argv": py(code) + ["{{secret:pw}}"]}}
    with pytest.raises(ActionError) as e:
        await run(action, secrets={"pw": "hunter22"})
    assert "exited 3" in str(e.value) and "auth failed for ***" in str(e.value)
    assert "hunter22" not in str(e.value) and e.value.__cause__ is None
    action["exec"]["ok_exit"] = [0, 3]
    assert await run(action, secrets={"pw": "hunter22"}) == ""


@hard
async def test_exec_secret_setting_redacted_and_async_resolver():
    code = "import sys; sys.stderr.write(' '.join(sys.argv[1:])); sys.exit(1)"

    async def vault(name):
        await asyncio.sleep(0)
        return {"tok": "tok-xyz"}.get(name)

    action = {"exec": {"argv": py(code) + ["{{settings.pw}}", "{{secret:tok}}"]}}
    with pytest.raises(ActionError) as e:
        await run(action, settings={"pw": Secret("p@ssw0rd")}, secrets=vault)
    assert "p@ssw0rd" not in str(e.value) and "tok-xyz" not in str(e.value) and "***" in str(e.value)
    with pytest.raises(ActionError, match="secret nope is not in the vault"):
        await run({"exec": {"argv": ["x", "{{secret:nope}}"]}}, secrets=vault)

    def broken(name):
        raise RuntimeError("hub unreachable")

    with pytest.raises(ActionError, match="vault lookup of tok failed"):
        await run({"exec": {"argv": ["x", "{{secret:tok}}"]}}, secrets=broken)


@hard
async def test_exec_per_os_override():
    action = {"exec": {"argv": py("print('linux')"), "windows": {"argv": py("print('windows')")}}}
    assert await run(action) == "linux"
    assert await run(action, worker={"os": "windows"}) == "windows"


@hard
async def test_exec_try_first_installed_binary(tmp_path):
    action = {"exec": {"try": [{"argv": ["no-such-grim-xyz", "{{tmp}}/s.png"]},
                               {"argv": py("import sys; open(sys.argv[1], 'wb').write(b'\\x89PNG')")
                                + ["{{tmp}}/s.png"]}],
                       "output": "{{tmp}}/s.png"}}
    out = await run(action, tmp=str(tmp_path))
    assert out["name"] == "s.png" and out["mime"] == "image/png" and out["bytes"] == 4
    with pytest.raises(Unavailable, match="no-such-a, no-such-b"):
        await run({"exec": {"try": [{"argv": ["no-such-a"]}, {"argv": ["no-such-b"]}]}})
    with pytest.raises(Unavailable, match="not installed"):
        await run({"exec": {"argv": ["/nonexistent/bin/tool"]}})


@hard
async def test_exec_missing_top_level_arg_fails_cleanly():
    with pytest.raises(ActionError, match=r"\{\{args.path\}\} is not set"):
        await run({"exec": {"argv": ["ls", "{{args.path}}"]}})


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie waiting for its parent counts as gone
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@hard
async def test_exec_timeout_kills_the_process_tree(tmp_path):
    pidfile = tmp_path / "pids"
    code = ("import subprocess,sys,time,os;"
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            "open(sys.argv[1],'w').write(f'{os.getpid()} {c.pid}'); time.sleep(60)")
    t0 = time.monotonic()
    with pytest.raises(ActionError, match="timed out after 1s") as e:
        await run({"exec": {"argv": py(code) + [str(pidfile)]}}, timeout=1)
    assert e.value.retry and time.monotonic() - t0 < 10
    pids = [int(p) for p in pidfile.read_text().split()]
    for _ in range(50):
        if not any(map(_alive, pids)):
            break
        await asyncio.sleep(0.05)
    assert not any(map(_alive, pids)), pids


@hard
async def test_exec_output_over_limit(monkeypatch):
    monkeypatch.setattr(engine, "MAX_OUTPUT", 1000)
    with pytest.raises(ActionError, match="over 1000 bytes"):
        await run({"exec": {"argv": py("print('x' * 5000)")}})


@hard
async def test_exec_parse_variants():
    out = "id=1\nstate=up\n"
    assert await run({"exec": {"argv": py(f"print({out!r}, end='')")}, "parse": "kv"}) == \
        {"id": "1", "state": "up"}
    assert await run({"exec": {"argv": py("print('a\\nb')")}, "parse": "lines"}) == ["a", "b"]
    rx = {"regex": r"^(?P<k>\w+)=(?P<v>\w+)$", "each": True}
    assert await run({"exec": {"argv": py(f"print({out!r}, end='')")}, "parse": rx,
                      "returns": {"key": "k"}}) == [{"key": "id"}, {"key": "state"}]


# --- http -----------------------------------------------------------------------

async def _echo(request):
    return web.json_response({
        "method": request.method, "raw_path": request.raw_path, "query": dict(request.query),
        "auth": request.headers.get("Authorization"), "x": request.headers.get("X-Thing"),
        "ctype": request.content_type, "body": (await request.read()).decode()})


async def _status(request):
    n = int(request.match_info["n"])
    return web.Response(status=n, text=f"status {n} for token {request.query.get('t', '')}")


async def _png(request):
    return web.Response(body=b"\x89PNGdata", content_type="image/png")


async def _empty(request):
    return web.Response(status=204)


@pytest_asyncio.fixture
async def http_base():
    app = web.Application()
    app.router.add_route("*", "/echo/{tail:.*}", _echo)
    app.router.add_get("/status/{n}", _status)
    app.router.add_get("/snap.png", _png)
    app.router.add_post("/empty", _empty)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}"
    await asyncio.wait_for(runner.cleanup(), 10)


@hard
async def test_http_query_headers_json_and_escaped_path(http_base):
    action = {"http": {"method": "POST", "url": "http://{{appliance.host}}/echo/{{args.p}}",
                       "query": {"action": "{{args.action}}", "opt": "{{args.opt}}"},
                       "headers": {"X-Thing": "v-{{args.n}}"},
                       "json": {"n": "{{args.n}}", "name": "{{args.p}}"}}}
    out = await run(action, {"p": "a/../b?c", "action": "on&off=1", "n": 3},
                    appliance={"host": http_base})
    assert out["method"] == "POST"
    assert out["raw_path"].startswith("/echo/a%2F..%2Fb%3Fc?")
    assert out["query"] == {"action": "on&off=1"}   # opt was unset: dropped
    assert out["x"] == "v-3" and out["ctype"] == "application/json"
    assert json.loads(out["body"]) == {"n": 3, "name": "a/../b?c"}


@hard
async def test_http_auth_form_body(http_base):
    base = f"http://{http_base}/echo/x"
    basic = await run({"http": {"url": base, "auth": {"basic": ["admin", "{{secret:pw}}"]}}},
                      secrets={"pw": "pw1"})
    assert basic["auth"] == "Basic " + base64.b64encode(b"admin:pw1").decode()
    bearer = await run({"http": {"url": base, "auth": {"bearer": "{{settings.tok}}"}}},
                       settings={"tok": "t0k"})
    assert bearer["auth"] == "Bearer t0k"
    form = await run({"http": {"method": "PUT", "url": base, "form": {"a": "{{args.a}}"}}}, {"a": "1 2"})
    assert form["body"] == "a=1+2" and form["ctype"] == "application/x-www-form-urlencoded"
    body = await run({"http": {"method": "POST", "url": base, "body": "raw {{args.a}}"}}, {"a": "x"})
    assert body["body"] == "raw x"


@hard
async def test_http_header_injection_refused(http_base):
    with pytest.raises(ActionError, match="control character"):
        await run({"http": {"url": f"http://{http_base}/echo/x", "headers": {"X-Thing": "{{args.v}}"}}},
                  {"v": "a\r\nX-Evil: 1"})


@hard
async def test_http_status_ok_and_redacted_errors(http_base):
    url = f"http://{http_base}/status/{{{{args.n}}}}"
    action = {"http": {"url": url, "query": {"t": "{{secret:tok}}"}}, "parse": "text"}
    with pytest.raises(ActionError) as e:
        await run(action, {"n": 503}, secrets={"tok": "sekrit-1"})
    assert "HTTP 503" in str(e.value) and "sekrit-1" not in str(e.value) and e.value.retry
    with pytest.raises(ActionError) as e:
        await run(action, {"n": 404}, secrets={"tok": "sekrit-1"})
    assert not e.value.retry
    action["http"]["ok"] = [404]
    assert await run(action, {"n": 404}, secrets={"tok": "x"}) == "status 404 for token x"


@hard
async def test_http_bytes_and_empty_json(http_base):
    out = await run({"http": {"url": f"http://{http_base}/snap.png"}, "parse": "bytes"})
    assert out["mime"] == "image/png" and out["bytes"] == 8 and out["name"] == "snap.png"
    assert await run({"http": {"method": "POST", "url": f"http://{http_base}/empty"}}) is None


@hard
async def test_http_connection_refused_is_retryable():
    with pytest.raises(ActionError, match="http failed") as e:
        await run({"http": {"url": "http://127.0.0.1:9/x"}})
    assert e.value.retry


# --- tls helper ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def server_tls(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    d = tmp_path_factory.mktemp("tls")
    (d / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    return ctx


@hard
async def test_https_self_signed_needs_verify_false(server_tls):
    app = web.Application()
    async def info(request):
        return web.json_response({"kvmd": True})

    app.router.add_get("/api/info", info)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        url = f"https://127.0.0.1:{port}/api/info"
        with pytest.raises(ActionError, match="http failed"):
            await run({"http": {"url": url}})
        assert await run({"http": {"url": url, "tls": {"verify": False}}}) == {"kvmd": True}
    finally:
        await runner.cleanup()


# --- tcp -----------------------------------------------------------------------

async def _tcp_server(handler, tls=None):
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls)
    return server, server.sockets[0].getsockname()[1]


async def _upper_line(reader, writer):
    try:
        line = await reader.readline()
        writer.write(line.upper() + b"rest-not-read\n")
        await writer.drain()
    finally:   # an open server-side socket makes Server.wait_closed() hang (3.12+)
        writer.close()


@hard
async def test_tcp_until_newline_close_and_bytes():
    server, port = await _tcp_server(_upper_line)
    async with server:
        base = {"host": "127.0.0.1", "port": "{{args.port}}", "send": "power {{args.state}}\n"}
        assert await run({"tcp": base}, {"port": port, "state": "on"}) == "POWER ON"
        assert await run({"tcp": {**base, "until": "close"}, "parse": "lines"},
                         {"port": port, "state": "on"}) == ["POWER ON", "rest-not-read"]
        assert await run({"tcp": {**base, "until": {"bytes": 5}}}, {"port": port, "state": "on"}) == "POWER"
        with pytest.raises(ActionError, match="control character"):
            await run({"tcp": base}, {"port": port, "state": "on\nreboot"})


@hard
async def test_tcp_map_send_is_one_json_line_and_tls(server_tls):
    async def echo(reader, writer):
        try:
            writer.write(await reader.readline())
            await writer.drain()
        finally:
            writer.close()

    server, port = await _tcp_server(echo, tls=server_tls)
    async with server:
        action = {"tcp": {"host": "127.0.0.1", "port": port, "tls": {"verify": False},
                          "send": {"cmd": "{{args.cmd}}", "n": "{{args.n}}"}}, "parse": "json"}
        assert await run(action, {"cmd": 'x"\n', "n": 2}) == {"cmd": 'x"\n', "n": 2}
        action["tcp"]["tls"] = True
        with pytest.raises(ActionError, match="tcp failed"):
            await run(action, {"cmd": "x", "n": 1})


@hard
async def test_tcp_refused_and_timeout():
    with pytest.raises(ActionError, match="tcp failed") as e:
        await run({"tcp": {"host": "127.0.0.1", "port": 9, "send": "x\n"}})
    assert e.value.retry

    async def silent(reader, writer):
        try:
            await reader.read()   # say nothing until the client hangs up
        finally:
            writer.close()

    server, port = await _tcp_server(silent)
    async with server:
        with pytest.raises(ActionError, match="tcp timed out after 0.3s"):
            await run({"tcp": {"host": "127.0.0.1", "port": port}}, timeout=0.3)


# --- ws ------------------------------------------------------------------------

@hard
async def test_ws_one_message_one_reply():
    import websockets

    seen = {}

    async def handler(ws):
        req = getattr(ws, "request", None)
        seen["auth"] = req.headers.get("Authorization") if req else ws.request_headers.get("Authorization")
        msg = await ws.recv()
        await ws.send(json.dumps({"got": json.loads(msg)}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        action = {"ws": {"url": f"ws://127.0.0.1:{port}/",
                         "headers": {"Authorization": "Bearer {{secret:t}}"},
                         "send": {"type": "request", "uri": "{{args.uri}}"}},
                  "parse": "json", "returns": {"uri": "got.uri"}}
        assert await run(action, {"uri": "ssap://x"}, secrets={"t": "abc"}) == {"uri": "ssap://x"}
        assert seen["auth"] == "Bearer abc"


# --- file ----------------------------------------------------------------------

@hard
async def test_file_write_then_read(tmp_path):
    data = str(tmp_path / "data")
    w = await run({"file": {"write": "{{plugin.data}}/{{args.name}}.json", "content": {"v": "{{args.v}}"}}},
                  {"name": "state", "v": 3}, plugin_data=data)
    assert w == {"path": f"{data}/state.json", "bytes": 7}
    assert await run({"file": {"read": "{{plugin.data}}/{{args.name}}.json"}, "parse": "json"},
                     {"name": "state"}, plugin_data=data) == {"v": 3}
    with pytest.raises(ActionError, match="single file name"):
        await run({"file": {"read": "{{plugin.data}}/{{args.name}}"}}, {"name": "../../etc/passwd"},
                  plugin_data=data)
    with pytest.raises(ActionError, match="file failed"):
        await run({"file": {"read": "{{plugin.data}}/missing"}}, plugin_data=data)
    with pytest.raises(ActionError, match="needs read or write"):
        await run({"file": {}})
