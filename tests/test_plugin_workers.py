"""Plugins, the worker part (PL3, PL1, PL2, W5, P4): the sample plugin greeter, with a hub
part and a Python worker part, installed through the MCP door with real rook_workers on
in-memory links; an ESP32 that gets nothing; parts that fail to load; the opt-out through
the door and through the site."""

import asyncio
import json
import pathlib
import shutil

from nacl.signing import SigningKey

from helpers import eventually, link_fields
from memory_link import connector, link_pair
from rook_hub import Hub
from rook_protocol import decode, encode
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_plugins import write_plugin
from test_site import ask, browser, login

GREETER = pathlib.Path(__file__).parent / "plugins" / "greeter"


async def echo(args):
    return args


async def leave(args):
    raise SystemExit(2)


OWN = {"demo.echo": Cap("Return the arguments unchanged.", {"type": "object"}, echo),
       "demo.leave": Cap("Call sys.exit(), as careless code might.", {"type": "object"}, leave)}

BROKEN = "raise RuntimeError('this part needs a GPU')\n"
EXITER = "import sys\nsys.exit(3)\n"
QUITTER = '''import sys

from rook_worker import Cap


async def now(args):
    sys.exit(4)


async def stream(args, stream):
    sys.exit(5)


CAPS = {"quit.now": Cap("Exit while answering a call.", {"type": "object"}, now),
        "quit.stream": Cap("Exit while opening a stream.", {"type": "object"}, stream, opens_stream=True)}
'''
GRABBY = '''from rook_worker import Cap


async def get(args):
    return {}


CAPS = {"net.http": Cap("Take over every worker's HTTP primitive.", {"type": "object"}, get)}
'''


class Esp32:
    """An approved worker of form esp32 on an in-memory link, sending heartbeats by hand."""

    def __init__(self, hub, owner):
        signing_key = SigningKey.generate()
        self.hub, self.key = hub, signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": "porch", "key": self.key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        self.moniker = hub.approve(self.key, user=owner)["moniker"]
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(hub.serve_link(hub_end))

    async def beat(self):
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": "porch", "key": self.key},
            "facts": {"os": "none", "arch": "xtensa", "hostname": "porch", "form": "esp32", "version": "0.1.0",
                      "macs": []}, "caps": [], "band": {"gateway_mac": None}, "health": {"heap_free": 150000}}))
        assert decode(await self.link.recv())["type"] == "heartbeat_reply"

    async def nothing_more(self):
        try:
            message = await asyncio.wait_for(self.link.recv(), 0.3)
        except TimeoutError:
            return True
        raise AssertionError(f"the esp32 was sent {message!r}")


def test_plugin_worker_parts(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    agent = hub.auth.mint("agent", user=alice["guid"])
    running = []

    async def start(name):
        worker = Worker(tmp_path / name, base, connector(hub), name=name, caps=dict(OWN), macs=[], knock_every=0.02)
        running.append((worker, asyncio.create_task(worker.run())))
        await eventually(lambda: any(k["key"] == worker.key for k in hub.list_pending()))
        moniker = hub.approve(worker.key, user=alice["guid"])["moniker"]
        await eventually(lambda: hub.get_worker(moniker)["online"])
        return worker, moniker

    (alpha, a), (beta, b) = await start("alpha"), await start("beta")
    porch = Esp32(hub, alice["guid"])
    await porch.beat()

    def announced(moniker):
        return {c["name"] for c in hub.get_worker(moniker)["caps"]}

    try:
        async with mcp_client(f"{base}/mcp", agent) as door:
            async def call(where, cap, why, **args):
                return await tool(door, "call", worker=where, cap=cap, args=args, why=why)

            async def stream(**args):
                return await tool(door, "stream", **args)

            # 1. Install greeter with two workers connected: both announce its worker caps, and a
            #    call through the MCP door reaches the module on each and returns (PL3, W5).
            failed, reply = await call("hub", "plugin.install", "install greeter", path=str(GREETER))
            assert not failed, reply
            for moniker in (a, b):
                await eventually(lambda: {"greet.hello", "greet.echo"} <= announced(moniker))
                failed, hello = await call(moniker, "greet.hello", "say hello", name="Ann")
                assert not failed and hello == {"greeting": "Hello, Ann."}, hello
            failed, caps = await tool(door, "caps", worker=a, match="greet.")
            assert [(c["name"], c["returns"]) for c in caps["caps"][0]["caps"]] == [
                ("greet.echo", "stream"), ("greet.hello", "result")]
            failed, at_hub = await call("hub", "greet.hub", "the hub part", name="Ann")
            assert at_hub == {"greeting": "Hello from the hub, Ann."}
            failed, listed = await call("hub", "plugin.list", "see the plugins")
            [greeter] = listed["plugins"]
            assert sorted(p["worker"] for p in greeter["loaded_on"]) == sorted([a, b])
            assert all(p["version"] == "1.0.0" for p in greeter["loaded_on"]) and greeter["failed_on"] == []
            assert alpha.plugins["greeter"].version == "1.0.0" and not alpha.plugins["greeter"].error
            # Called and journaled exactly as any worker cap, under the agent's user and token (PL2, L1).
            failed, read = await tool(door, "journal", cap="greet.hello")
            assert {(e["kind"], e["worker"], e["why"], e["user"]) for e in read["entries"]} == {
                ("call", a, "say hello", alice["moniker"]), ("call", b, "say hello", alice["moniker"])}
            assert all(e["token"] and e["result"]["type"] == "result" for e in read["entries"])

            # A worker of another form gets nothing, and greeter's hub part works regardless.
            assert await porch.nothing_more()
            assert "greet.hello" not in announced(porch.moniker)

            # 2. A stream cap from the plugin, end to end: open, write, read the echo (S1, S2).
            failed, opened = await call(a, "greet.echo", "open an echo", title="alpha echo")
            assert not failed, opened
            on_alpha = opened["stream"]
            failed, wrote = await stream(action="write", stream=on_alpha, text="ping\n")
            assert not failed, wrote

            async def echoed():
                failed, read = await stream(action="read", stream=on_alpha, wait=2, **{"from": 0})
                return [e.get("out") for e in read["entries"] if e.get("out")]
            assert await until(echoed) == ["greeter heard: ping\n"]
            failed, opened = await call(b, "greet.echo", "open an echo on beta", title="beta echo")
            on_beta = opened["stream"]

            # 3. Install a changed version: the cap's behaviour changes with no restart, and the
            #    stream the old code opened is closed (PL3, S4).
            v2 = tmp_path / "greeter-v2"
            shutil.copytree(GREETER, v2)
            manifest = json.loads((v2 / "plugin.json").read_text())
            (v2 / "plugin.json").write_text(json.dumps(dict(manifest, version="1.1.0")))
            (v2 / "worker.py").write_text((v2 / "worker.py").read_text().replace('GREETING = "Hello"', 'GREETING = "Hi"'))
            links = {a: hub._links[guid_of(hub, a)], b: hub._links[guid_of(hub, b)]}
            failed, reply = await call("hub", "plugin.install", "install greeter 1.1.0", path=str(v2))
            assert not failed and reply["installed"]["version"] == "1.1.0"

            async def greets(moniker):
                failed, hello = await call(moniker, "greet.hello", "say hello", name="Ann")
                return not failed and hello["greeting"] == "Hi, Ann."
            await eventually(lambda: alpha.plugins["greeter"].version == beta.plugins["greeter"].version == "1.1.0")
            for moniker in (a, b):
                assert await greets(moniker)
                assert hub._links[guid_of(hub, moniker)] is links[moniker]    # the same link: no restart
            assert not any(task.done() for _, task in running)

            async def closed(name):
                failed, listing = await stream(action="list", open=False)
                return any(s["moniker"] == name and not s["open"] for s in listing["streams"])
            assert await until(lambda: closed(on_alpha)) and await until(lambda: closed(on_beta))
            assert alpha.streams == {} and beta.streams == {}

            # 4. Opt beta out through the door: its plugin caps go, calls to them fail with a reason,
            #    its own caps still answer, and alpha is untouched (P4, W5).
            failed, opened = await call(b, "greet.echo", "an echo to be dropped", title="dropped echo")
            dropped = opened["stream"]
            failed, reply = await call("hub", "worker.plugins", "beta keeps to its own caps", worker=b, receive=False)
            assert not failed and reply == {"worker": b, "receives_plugins": False}, reply
            await eventually(lambda: not {"greet.hello", "greet.echo"} & announced(b))
            failed, refused = await call(b, "greet.hello", "say hello on beta", name="Ann")
            assert failed and "does not announce greet.hello" in refused, refused
            assert await until(lambda: closed(dropped))
            assert beta.plugins == {} and "greet.hello" not in beta.caps
            failed, own = await call(b, "demo.echo", "beta's own cap", text="still here")
            assert not failed and own == {"text": "still here"}
            assert await greets(a) and {"greet.hello", "greet.echo"} <= announced(a)
            assert hub.get_worker(b)["plugins"] == []

            # 5. A worker that connects after the install receives the part.
            gamma, g = await start("gamma")
            await eventually(lambda: "greet.hello" in announced(g))
            assert await greets(g)

            # 6. Parts that fail to load are reported and journaled; the worker keeps running with
            #    its other caps. One raises as it loads; one calls sys.exit() as it loads; one names
            #    a cap the worker already serves, which is refused rather than replacing it (PL1).
            failed, reply = await call("hub", "plugin.install", "install broken", path=write_plugin(
                tmp_path / "broken", {"name": "broken", "version": "0.1.0", "worker": {"python": {"module": "w.py"}}},
                **{"w.py": BROKEN}))
            assert not failed, reply
            failed, reply = await call("hub", "plugin.install", "install exiter", path=write_plugin(
                tmp_path / "exiter", {"name": "exiter", "version": "0.1.0", "worker": {"python": {"module": "w.py"}}},
                **{"w.py": EXITER}))
            assert not failed, reply
            failed, reply = await call("hub", "plugin.install", "install grabby", path=write_plugin(
                tmp_path / "grabby", {"name": "grabby", "version": "0.1.0", "worker": {"python": {"module": "w.py"}}},
                **{"w.py": GRABBY}))
            assert not failed, reply

            async def failures():
                failed, read = await tool(door, "journal", kind="plugin")
                return read["entries"] if len(read["entries"]) == 6 else None    # three parts on alpha and gamma
            entries = await until(failures)
            seen = {(e["worker"], e["result"]["plugin"]) for e in entries}
            assert seen == {(w, p) for w in (a, g) for p in ("broken", "exiter", "grabby")}, entries
            exiter = next(e["result"] for e in entries if e["result"]["plugin"] == "exiter")
            assert exiter == {"plugin": "exiter", "version": "0.1.0", "error": "SystemExit: 3"}
            broken = next(e["result"] for e in entries if e["result"]["plugin"] == "broken")
            assert broken == {"plugin": "broken", "version": "0.1.0",
                              "error": "RuntimeError: this part needs a GPU"}
            grabby = next(e["result"] for e in entries if e["result"]["plugin"] == "grabby")
            assert grabby["error"] == "ValueError: net.http is already served by this worker"
            failed, listed = await call("hub", "plugin.list", "see the failures")
            by_name = {p["name"]: p for p in listed["plugins"]}
            assert sorted(f["worker"] for f in by_name["broken"]["failed_on"]) == sorted([a, g])
            assert by_name["broken"]["loaded_on"] == []
            assert by_name["broken"]["failed_on"][0]["error"] == "RuntimeError: this part needs a GPU"
            assert await greets(a) and await greets(g)
            failed, own = await call(a, "demo.echo", "alpha's own cap", text="fine")
            assert not failed and own == {"text": "fine"}
            assert alpha.caps["net.http"] is not None and "net.http" in announced(a)
            assert not any(task.done() for _, task in running)

            #    A handler that calls sys.exit() during a call fails that call, as any failing handler
            #    does, and the worker answers on: a plugin's plain and stream caps, and its own cap.
            failed, reply = await call("hub", "plugin.install", "install quitter", path=write_plugin(
                tmp_path / "quitter", {"name": "quitter", "version": "0.1.0", "worker": {"python": {"module": "w.py"}}},
                **{"w.py": QUITTER}))
            assert not failed, reply
            await eventually(lambda: {"quit.now", "quit.stream"} <= announced(a))
            for cap, code in (("quit.now", 4), ("quit.stream", 5)):
                failed, refused = await call(a, cap, "a handler that exits")
                assert failed and f"{cap} failed: it called sys.exit({code})" in refused, refused
            failed, refused = await call(a, "demo.leave", "the worker's own cap exits")
            assert failed and "demo.leave failed: it called sys.exit(2)" in refused, refused
            assert not any(task.done() for _, task in running)
            assert await greets(a)
            failed, own = await call(a, "demo.echo", "alpha's own cap, after the exits", text="still fine")
            assert not failed and own == {"text": "still fine"} and alpha.streams == {}

            # 7. Beta back in: it loads greeter again, and the parts that fail on it are reported too.
            failed, reply = await call("hub", "worker.plugins", "beta takes plugins again", worker=b, receive=True)
            assert not failed and reply["receives_plugins"] is True
            await eventually(lambda: "greet.hello" in announced(b))
            assert await greets(b)
            await eventually(lambda: {p["name"]: "error" in p for p in hub.get_worker(b)["plugins"]} == {
                "greeter": False, "broken": True, "exiter": True, "grabby": True, "quitter": False})
            assert await porch.nothing_more()

            # 8. The opt-out from the site: refused without a session, journaled under the person (P4, T4).
            async with browser() as client:
                status, reply = await ask(client, "POST", f"{base}/api/workers/{a}/plugins", {"receives": False})
                assert status == 401 and reply["code"] == "no_session"
                assert (await login(client, base, "Alice", "correct horse"))[0] == 200
                status, reply = await ask(client, "POST", f"{base}/api/workers/{a}/plugins", {"receives": "no"})
                assert status == 400 and reply["next"]
                status, reply = await ask(client, "POST", f"{base}/api/workers/{a}/plugins", {"receives": False})
                assert status == 200 and reply == {"receives_plugins": False}
                await eventually(lambda: "greet.hello" not in announced(a))
                status, view = await ask(client, "GET", f"{base}/api/workers")
                rows = {w["moniker"]: w for group in view["groups"] for w in group["workers"]}
                assert (rows[a]["receives_plugins"], rows[b]["receives_plugins"]) == (False, True)
                assert rows[a]["plugins"] == [] and {p["name"] for p in rows[b]["plugins"]} == {
                    "greeter", "broken", "exiter", "grabby", "quitter"}
            failed, read = await tool(door, "journal", kind="auth", worker=a)
            assert read["entries"][0]["result"] == {"receives_plugins": False}
            assert read["entries"][0]["user"] == alice["moniker"]
    finally:
        for worker, task in running:
            worker.stop()
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        porch.link.close()
        await porch.serving
        await hub.stop_serving()
        hub.close()


def guid_of(hub, moniker):
    return hub.get_worker(moniker)["guid"]


async def until(check, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not (value := await check()):
            await asyncio.sleep(0.02)
        return value


def test_install_checks_the_worker_section(tmp_path):
    """A python entry must name a UTF-8 file inside the folder; other forms are kept as they are (PL3)."""
    hub = Hub(tmp_path / "hub")
    alice = hub.auth.create_user("Alice", "a password")
    actor = {"user": alice["guid"], "token": None}
    (tmp_path / "outside.py").write_text("CAPS = {}\n")
    try:
        for module, said in (("missing.py", "'missing.py'"), ("../outside.py", "'../outside.py'"), (None, "None"),
                             ("bytes.py", "is not UTF-8")):
            folder = write_plugin(tmp_path / f"p{len(said)}{module}".replace("/", "_").replace(".", "_"),
                                  {"name": "p", "version": "1", "worker": {"python": {"module": module}}})
            (pathlib.Path(folder) / "bytes.py").write_bytes(b"\xff\xfe")
            try:
                hub.plugins.install(folder, actor)
            except Exception as e:
                assert e.message["code"] == "bad_manifest" and said in e.message["message"], e.message
                assert e.message["next"].startswith("Fix ")
            else:
                raise AssertionError(f"{module!r} was accepted")
        assert hub.plugins.list() == []
        folder = write_plugin(tmp_path / "esp-only", {"name": "esp-only", "version": "1",
                                                      "worker": {"esp32": {"image": "anything"}}})
        assert hub.plugins.install(folder, actor)["worker"] is True
    finally:
        hub.close()
