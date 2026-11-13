"""Devices and delegates (D1 to D8): the brief's eleven-step scenario, through the MCP door.

Real rook_worker workers X and Y are on band Home and Z is roaming, all on in-memory
links. The device is a small HTTP lamp on localhost; the sample plugin `lamp` is its
driver. Each worker here tags its HTTP primitive's requests with its name, so the
lamp can tell which worker's request reached it."""

import asyncio
import http.server
import json
import os
import pathlib
import socketserver
import threading

from helpers import Clock, eventually
from memory_link import connector
from rook_hub import Hub, bands, devices
from rook_worker import Cap, Worker
from test_door import mcp_client, tool

LAMP = pathlib.Path(__file__).parent / "plugins" / "lamp"
HOME_GW, CAFE_GW = "02:aa:00:00:00:01", "02:cc:00:00:00:03"
KEYS = {"device": "desk-lamp-key-123", "band": "band-lamp-key-456", "hub": "hub-lamp-key-789"}


class Box(Worker):
    """A real rook_worker worker that reports the gateway it is given, and whose
    net.http primitive adds a header naming it (test instrumentation only)."""

    def __init__(self, *args, gateway, **kwargs):
        super().__init__(*args, **kwargs)
        self.gateway = gateway
        real = self.caps["net.http"]

        async def tagged(args):
            return await real.handler(dict(args, headers={**args.get("headers", {}), "x-came-through": self.name}))
        self.caps["net.http"] = Cap(real.description, real.schema, tagged)

    def heartbeat(self):
        return dict(super().heartbeat(), band={"gateway_mac": self.gateway})


class Lamp:
    """A lamp with an HTTP API: POST /on, POST /off, GET /state. It keeps every request it got."""

    def __init__(self):
        self.on, self.requests = False, []
        lamp = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                lamp.requests.append({"path": self.path, "via": self.headers.get("x-came-through"),
                                      "key": self.headers.get("x-lamp-key")})
                if self.path in ("/on", "/off"):
                    lamp.on = self.path == "/on"
                body = json.dumps({"on": lamp.on}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def via(self):
        return self.requests[-1]["via"]


class EchoTCP(socketserver.BaseRequestHandler):
    received = []

    def handle(self):
        data = self.request.recv(1024)
        EchoTCP.received.append(data)
        try:
            self.request.sendall(b"echo:" + data)
        except OSError:          # a sender that does not wait for the answer has gone already
            pass


class EchoUDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(b"echo:" + data, self.client_address)


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_devices(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock, vault_key=os.urandom(32))
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["guid"])
    lamp = Lamp()
    tcp = serve(socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoTCP))
    udp = serve(socketserver.ThreadingUDPServer(("127.0.0.1", 0), EchoUDP))
    running = {}

    async def start(name, gateway):
        box = Box(tmp_path / name, f"http://{host}:{port}", connector(hub), name=name, macs=[], knock_every=0.02,
                  gateway=gateway)
        running[name] = (box, asyncio.create_task(box.run()))
        if box.identity is None:
            await eventually(lambda: any(k["key"] == box.key for k in hub.list_pending()))
            hub.approve(box.key, user=alice["guid"])
        await eventually(lambda: box.identity and box.identity["guid"] in hub._links)
        await eventually(lambda: hub.get_worker(box.identity["guid"])["online"])
        return box.identity

    async def stop(name):
        box, task = running.pop(name)
        box.stop()
        await task
        await eventually(lambda: box.identity["guid"] not in hub._links)

    try:
        x, y, z = await start("x", HOME_GW), await start("y", HOME_GW), await start("z", CAFE_GW)
        home = bands.create(hub, "Home", HOME_GW, user=alice["guid"])

        async with mcp_client(f"http://{host}:{port}/mcp", agent) as door:
            async def call(worker, cap, why, **args):
                return await tool(door, "call", worker=worker, cap=cap, args=args, why=why)

            async def listed(moniker):
                return next(w for w in (await tool(door, "workers"))[1]["workers"] if w["moniker"] == moniker)

            # 11. The primitives on their own: the agent calls them on X directly (D3).
            failed, answer = await call(x["moniker"], "net.http", "fetch the lamp's state", url=lamp.url + "/state")
            assert not failed and answer["status"] == 200 and json.loads(answer["body"]) == {"on": False}
            failed, answer = await call(x["moniker"], "net.tcp", "talk to the echo server",
                                        host="127.0.0.1", port=tcp.server_address[1], data="hello", wait=2)
            assert not failed and answer == {"data": "echo:hello"}
            failed, answer = await call(x["moniker"], "net.udp", "send a datagram", host="127.0.0.1",
                                        port=udp.server_address[1], data_base64="/w==", wait=2)
            assert not failed and answer == {"data_base64": "ZWNobzr/"}     # bytes that are not text come back as base64
            #     With wait 0, TCP sends and returns without reading.
            failed, answer = await call(x["moniker"], "net.tcp", "send without waiting",
                                        host="127.0.0.1", port=tcp.server_address[1], data="fire", wait=0)
            assert not failed and answer == {"data": None}
            await eventually(lambda: b"fire" in EchoTCP.received)
            #     net.http makes HTTP requests only: a file URL is refused, and nothing is read.
            secret_file = tmp_path / "not-for-agents.txt"
            secret_file.write_text("the worker's own disk")
            failed, text = await call(x["moniker"], "net.http", "try to read a file", url=secret_file.as_uri())
            assert failed and "http:// or https://" in text and "own disk" not in text
            lamp.requests.clear()

            # 1. The agent installs lamp and adds a device on band Home with a floating route. The
            #    workers tool shows it as a device in Home with its route; caps lists its caps (D1, D2, D4).
            failed, reply = await call("hub", "plugin.install", "install the lamp driver", path=str(LAMP))
            assert not failed and reply["installed"]["drivers"] == ["lamp"]
            failed, reply = await call("hub", "device.add", "add the desk lamp", name="Desk lamp", driver="lamp",
                                       band=home["moniker"], route="floating", interval=30,
                                       config={"url": lamp.url, "key": "secret:lamp-key"})
            assert not failed, reply
            desk = reply["added"]["moniker"]
            shown = await listed(desk)
            assert shown["device"] is True and shown["band"]["name"] == "Home" and shown["route"] == {"kind": "floating"}
            assert shown["caps"] == ["lamp.off", "lamp.on"] and shown["reachable"] is None
            failed, listing = await tool(door, "caps", worker=desk)
            assert [c["name"] for c in listing["caps"][0]["caps"]] == ["lamp.off", "lamp.on"]

            # 10 (set up). The key lives in the vault: the desk lamp's own scope (X1).
            failed, made = await call("hub", "secret.create", "keep the desk lamp's key", scope=f"worker:{desk}",
                                      name="lamp-key", value=KEYS["device"])
            assert not failed, made

            # 2. lamp.on through X or Y, never Z and never the hub; the device call and the primitive
            #    call are both journaled (D5, L1).
            failed, answer = await call(desk, "lamp.on", "turn on the desk lamp")
            assert not failed and answer == {"on": True} and lamp.on
            first = lamp.via()
            assert first in ("x", "y")
            failed, read = await tool(door, "journal", cap="lamp.on")
            assert [(e["worker"], e["why"], e["user"]) for e in read["entries"]] == [
                (desk, "turn on the desk lamp", alice["moniker"])]
            failed, read = await tool(door, "journal", cap="net.http", limit=1)
            [primitive] = read["entries"]
            assert primitive["worker"] == {"x": x, "y": y}[first]["moniker"] and desk in primitive["why"]

            # 3. The next call goes through the same worker: the last that succeeded (D5).
            await call(desk, "lamp.off", "turn it off again")
            assert lamp.via() == first and not lamp.on

            # 4. That worker goes offline; the next call succeeds through the other one (D5).
            other = "y" if first == "x" else "x"
            await stop(first)
            failed, answer = await call(desk, "lamp.on", "on again, with one worker gone")
            assert not failed and lamp.via() == other

            # 5. Both X and Y offline: the error names them and says what to do; Z was never tried.
            await stop(other)
            count = len(lamp.requests)
            failed, text = await call(desk, "lamp.on", "on with no jump box")
            assert failed and text.startswith("device_unreachable:") and "Next:" in text
            assert x["moniker"] in text and y["moniker"] in text and z["moniker"] not in text
            assert len(lamp.requests) == count

            # 6. Y is back but opts out of serving devices (D8, W4): it is not used. Opted back in, it is.
            await start("y", HOME_GW)
            devices.set_serves_devices(hub, y["moniker"], False)
            assert (await listed(y["moniker"]))["serves_devices"] is False
            failed, text = await call(desk, "lamp.on", "on with the only worker opted out")
            assert failed and f"{y['moniker']} does not serve devices" in text and len(lamp.requests) == count
            devices.set_serves_devices(hub, y["moniker"], True)
            failed, answer = await call(desk, "lamp.on", "on through Y")
            assert not failed and lamp.via() == "y"

            # 6b. A job calls a device like any worker (D1, J1): on an event, it turns the lamp on.
            await call(desk, "lamp.off", "off before the job")
            failed, made = await call("hub", "job.create", "light up when the door opens", title="door light",
                                      match={"event": "door.opened"}, call={"worker": desk, "cap": "lamp.on"})
            assert not failed and made["job"]["call"]["worker"] == desk
            await call("hub", "event.emit", "someone came in", name="door.opened")
            await eventually(lambda: lamp.on)
            failed, read = await tool(door, "journal", kind="job", job=made["job"]["moniker"])
            assert read["entries"][0]["worker"] == desk and read["entries"][0]["cap"] == "lamp.on"

            # 7. A device pinned to X uses only X; with X offline it fails naming X, though Y is online (D4).
            failed, reply = await call("hub", "device.add", "add a lamp pinned to X", name="Shelf lamp", driver="lamp",
                                       route="pinned", via=x["moniker"], band=home["moniker"],
                                       config={"url": lamp.url, "key": "secret:lamp-key"})
            shelf = reply["added"]["moniker"]
            assert reply["added"]["route"] == {"kind": "pinned", "via": x["moniker"]}
            await call("hub", "secret.create", "a key for the whole band", scope=f"band:{home['moniker']}",
                       name="lamp-key", value=KEYS["band"])
            await start("x", HOME_GW)
            failed, answer = await call(shelf, "lamp.on", "the shelf lamp, through X")
            assert not failed and lamp.via() == "x" and lamp.requests[-1]["key"] == KEYS["band"]   # band scope
            await stop("x")
            count = len(lamp.requests)
            failed, text = await call(shelf, "lamp.on", "the shelf lamp, X gone")
            assert failed and text.startswith("device_unreachable:") and x["moniker"] in text
            assert len(lamp.requests) == count

            # 8. A device with a direct route is reached by the hub itself, no worker involved (D4).
            await call("hub", "secret.create", "a key for the hub", scope="hub", name="lamp-key", value=KEYS["hub"])
            failed, reply = await call("hub", "device.add", "add a lamp the hub reaches", name="Hall lamp",
                                       driver="lamp", route="direct", config={"url": lamp.url, "key": "secret:lamp-key"})
            hall = reply["added"]["moniker"]
            failed, read = await tool(door, "journal", cap="net.http")
            primitives_before = len(read["entries"])
            failed, answer = await call(hall, "lamp.off", "the hall lamp, from the hub")
            assert not failed and lamp.via() is None and lamp.requests[-1]["key"] == KEYS["hub"]   # hub scope
            failed, read = await tool(door, "journal", cap="net.http")
            assert len(read["entries"]) == primitives_before

            # 9. Status: once the interval passes the hub polls the lamp through its route, and the
            #    workers tool shows the answer; with no route it shows the device unreachable (D6).
            await call(desk, "lamp.on", "on before the poll")
            hub.events.tick()
            assert (await listed(desk))["reachable"] is None                      # not due yet
            clock.advance(30)
            hub.events.tick()
            await eventually(lambda: devices.find(hub, desk)["reachable"] is not None)
            shown = await listed(desk)
            assert shown["reachable"] is True and shown["status"] == {"on": True} and lamp.via() == "y"
            await stop("y")
            clock.advance(30)
            hub.events.tick()
            await eventually(lambda: devices.find(hub, desk)["reachable"] == 0)
            shown = await listed(desk)
            assert shown["reachable"] is False and shown["status"] == {"on": True}  # the latest answer is kept

            # 10. The lamp saw the desk lamp's own key (device scope); the stored device keeps the
            #     reference and the journal holds no key (D7, X2, L4).
            assert {r["key"] for r in lamp.requests if r["via"] in ("x", "y") and r["path"] != "/state"} \
                >= {KEYS["device"]}
            assert json.loads(devices.find(hub, desk)["config"]) == {"url": lamp.url, "key": "secret:lamp-key"}
            journal = json.dumps([dict(r) for r in hub._db.execute("SELECT * FROM journal")])
            assert not any(key in journal for key in KEYS.values())
    finally:
        for name in list(running):
            await stop(name)
        await hub.stop_serving()
        hub.close()
        for server in (lamp.server, tcp, udp):
            server.shutdown()
            server.server_close()


def test_device_errors(tmp_path):
    # C3: each refusal says what to try next. A driver handler that raises names its plugin;
    # a driver whose handler is not async is refused at install (D2).
    from test_plugins import manifest, write_plugin

    async def go():
        hub = Hub(tmp_path / "hub")
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            hub.plugins.install(str(LAMP), {"user": alice, "token": None})
            hub.plugins.install(write_plugin(tmp_path / "broken-bulb", manifest("broken-bulb"),
                                             "def setup(plugin):\n"
                                             "    bulb = plugin.driver('bulb')\n"
                                             "    @bulb.cap('bulb.on', 'Fail.', {'type': 'object'})\n"
                                             "    async def on(device, args):\n"
                                             "        raise KeyError('url')\n"), {"user": alice, "token": None})

            async def add(**args):
                return await hub.call("hub", "device.add", args, "add a device", user=alice)
            for args, code, words in (
                    ({"name": "a", "driver": "toaster", "route": "direct"}, "unknown_driver", "Drivers installed"),
                    ({"name": "a", "driver": "lamp", "route": "floating"}, "bad_message", "band"),
                    ({"name": "a", "driver": "lamp", "route": "pinned"}, "bad_message", "via"),
                    ({"name": "a", "driver": "lamp", "route": "teleport"}, "bad_message", "floating"),
                    ({"name": "a", "driver": "lamp", "route": "direct", "interval": 0}, "bad_message", "seconds")):
                reply = await add(**args)
                assert reply["code"] == code and words in reply["message"] + reply["next"], reply
            assert devices.list_devices(hub) == []

            bulb = (await add(name="Bulb", driver="bulb", route="direct"))["result"]["added"]["moniker"]
            reply = await hub.call(bulb, "bulb.on", {}, "try the broken driver", user=alice)
            assert reply["code"] == "plugin_failed" and "Plugin broken-bulb failed in bulb.on" in reply["message"]
            reply = await hub.call(bulb, "lamp.on", {}, "a cap this device lacks", user=alice)
            assert reply["code"] == "unknown_cap" and reply["next"]

            try:
                hub.plugins.install(write_plugin(tmp_path / "sync-bulb", manifest("sync-bulb"),
                                                 "def setup(plugin):\n"
                                                 "    d = plugin.driver('sync-bulb')\n"
                                                 "    @d.cap('sync-bulb.on', 'Not async.', {'type': 'object'})\n"
                                                 "    def on(device, args):\n        return 1\n"),
                                   {"user": alice, "token": None})
                raise AssertionError("a driver with a plain handler was installed")
            except Exception as e:
                assert getattr(e, "message", {}).get("code") == "bad_plugin" and "async" in str(e)
            assert "sync-bulb" not in hub.plugins.drivers
        finally:
            hub.close()
    asyncio.run(go())


def test_primitives_make_http_requests_only():
    # D3: net.http is an HTTP request. Other schemes urllib knows are refused before anything
    # is opened, on a worker and on the hub's direct route alike (both run rook_net).
    import pytest
    import rook_net
    for url in ("file:///etc/hostname", "ftp://127.0.0.1/x", "data:,hello", "/etc/hostname"):
        with pytest.raises(ValueError, match="http:// or https://"):
            rook_net.http({"url": url})
