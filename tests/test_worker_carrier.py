"""The worker's Telesthete carrier (docs/link-design.md sections 3, 4, 6.1 and 9; build plan L5), with a
real `rook_worker` (its real exchange, its real loop, `rook_link`) and the real hub, over loopback
WebSocket through a proxy that can record, stall, cut and inject. Two tests wait for Telesthete's real
timers (the hub restart and the 20 s stall); everything else is quick."""

import asyncio
import dataclasses
import logging
import time

import aiohttp
from aiohttp import WSMsgType, web

import rook_link
from rook_hub import Hub
from rook_protocol import MAX_MESSAGE, ProtocolError, encode
from rook_worker import Cap, Refused, Worker
from test_link_carrier import udp_sockets


async def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


class Proxy:
    """Between the worker's WebSocket client and the hub's /band. It passes binary messages both ways;
    while `stalled` it drops them (a partition); it records what the hub sends while `recording`; it can
    cut every connection, and inject frames into the open ones as if the hub had sent them."""

    def __init__(self, upstream):
        self.upstream, self.recorded, self.recording, self.stalled = upstream, [], False, False
        self._open = []

    async def start(self):
        app = web.Application()
        app.router.add_get("/band", self._route)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        host, port = self._runner.addresses[0][:2]
        self.url = f"ws://{host}:{port}/band"
        return self

    async def _route(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            async with aiohttp.ClientSession() as client, client.ws_connect(self.upstream) as up:
                pair = (ws, up)
                self._open.append(pair)

                async def down():
                    async for message in up:
                        if message.type == WSMsgType.BINARY:
                            if self.recording:
                                self.recorded.append(message.data)
                            if not self.stalled:
                                await ws.send_bytes(message.data)
                    await ws.close()
                to_worker = asyncio.create_task(down())
                try:
                    async for message in ws:
                        if message.type == WSMsgType.BINARY and not self.stalled:
                            await up.send_bytes(message.data)
                finally:
                    to_worker.cancel()
                    await asyncio.gather(to_worker, return_exceptions=True)
                    self._open.remove(pair)
        except aiohttp.ClientError:
            pass                                    # the hub is down: the worker's client tries again
        await ws.close()
        return ws

    async def cut(self):
        for ws, up in list(self._open):
            await up.close()
            await ws.close()

    async def inject(self, frames):
        for ws, _ in list(self._open):
            for frame in frames:
                await ws.send_bytes(frame)

    async def stop(self):
        await self.cut()
        await self._runner.cleanup()


ran = {"count": 0}


async def echo(args):
    return args


async def size(args):
    return len(args["text"])


async def count(args):
    ran["count"] += 1
    return ran["count"]


async def slow(args):
    await asyncio.sleep(3600)


async def term(args, stream):
    async def run():
        while (data := await stream.read()) is not None:
            await stream.send(b"heard: " + data)
    asyncio.create_task(run())
    return {"title": "term", "description": "Echoes input."}


opened = []


async def held(args, stream):
    opened.append(stream)
    return {"title": "held", "description": "Stays open until the test closes it."}


CAPS = {"demo.echo": Cap("Echo.", {"type": "object"}, echo), "demo.size": Cap("Size.", {"type": "object"}, size),
        "demo.count": Cap("Count runs.", {"type": "object"}, count),
        "demo.slow": Cap("Never answers.", {"type": "object"}, slow),
        "demo.term": Cap("A terminal.", {"type": "object"}, term, opens_stream=True),
        "demo.held": Cap("A stream.", {"type": "object"}, held, opens_stream=True)}


class Rig:
    """A served hub, a proxy in front of its /band, and real workers whose carrier goes through it."""

    def __init__(self, tmp_path):
        self.tmp_path, self.links, self.running = tmp_path, [], []

    async def __aenter__(self):
        await self.serve()
        self.alice = self.hub.auth.create_user("Alice", "a password")["guid"]
        self.proxy = await Proxy(f"ws://{self.host}:{self.port}/band").start()
        return self

    async def serve(self, port=0):
        self.hub = Hub(self.tmp_path / "hub")
        self.host, self.port = await self.hub.serve("127.0.0.1", port)
        self.base = f"http://{self.host}:{self.port}"

    async def via_proxy(self, session):
        """The worker's own carrier, pointed at the proxy instead of the hub's /band."""
        link = await rook_link.connect(dataclasses.replace(session, band_url=self.proxy.url))
        self.links.append(link)
        return link

    def worker(self, state="box"):
        worker = Worker(self.tmp_path / state, self.base, self.via_proxy, name="box", caps=dict(CAPS), macs=[],
                        knock_every=0.02)
        worker.opens = []
        post = worker._post

        def counted(body, path="/knock"):
            if path == "/link":
                worker.opens.append(time.monotonic())
            return post(body, path)
        worker._post = counted
        return worker

    def run(self, worker):
        task = asyncio.create_task(worker.run())
        self.running.append((worker, task))
        return task

    async def approved(self, worker):
        task = self.run(worker)
        await until(lambda: any(p["key"] == worker.key for p in self.hub.list_pending()))
        self.guid = self.hub.approve(worker.key, user=self.alice)["guid"]
        await until(lambda: self.online())
        return task

    def online(self):
        return self.hub._links.get(self.guid) is not None and self.hub.get_worker(self.guid)["online"]

    async def call(self, cap, args=None, **options):
        return await self.hub.call(self.guid, cap, args or {}, "a test call", user=self.alice, **options)

    async def __aexit__(self, *exc):
        for worker, task in self.running:
            worker.stop()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await self.proxy.stop()
        await self.hub.stop_serving()
        self.hub.close()


def carrier_tasks():
    """Tasks of a worker's carrier still alive: its link, its WebSocket client, the websockets library."""
    names = ("TelestheteLink", "WebSocketTransport", "rook_link")
    alive = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        where = f"{getattr(coro, '__qualname__', '')} {getattr(getattr(coro, 'cr_code', None), 'co_filename', '')}"
        if any(name in where for name in names) or "/websockets/" in where:
            alive.append(where)
    return alive


def test_the_real_worker_over_websocket(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)                 # every logger, so a session key in any log is found
    asyncio.run(over_websocket(tmp_path, caplog))


async def over_websocket(tmp_path, caplog):
    before = udp_sockets()
    async with Rig(tmp_path) as rig:
        worker = rig.worker()
        running = await rig.approved(worker)
        assert len(worker.opens) == 1 and udp_sockets() == before           # no UDP socket in the worker (D3)
        psk = rig.hub.sessions.live(worker.key)
        assert psk is not None

        # A call and a stream round trip.
        assert (await rig.call("demo.echo", {"a": 1}))["result"] == {"a": 1}
        stream = (await rig.call("demo.term"))["stream"]
        await rig.hub.streams.write(stream, "ping", user=rig.alice)
        entries = []
        while len(entries) < 2:
            entries += (await rig.hub.streams.read(stream, len(entries), wait=5))["entries"]
        assert [e.get("in") or e.get("out") for e in entries] == ["ping", "heard: ping"]

        # The proxy cuts the WebSocket: the client reconnects under the same key, and the session and
        # Channel 0 carry on with no new link_open (section 3.3).
        await rig.proxy.cut()
        assert (await rig.call("demo.echo", {"after": "cut"}))["result"] == {"after": "cut"}
        assert len(worker.opens) == 1 and rig.hub.sessions.live(worker.key) == psk

        # A stall of 3 s: frames are lost both ways, Telesthete retransmits, the session carries on.
        rig.proxy.stalled = True
        await asyncio.sleep(3)
        rig.proxy.stalled = False
        assert (await rig.call("demo.echo", {"after": "stall"}))["result"] == {"after": "stall"}
        assert len(worker.opens) == 1 and rig.hub.sessions.live(worker.key) == psk

        # Nothing logged the session key, and the worker kept no copy of it.
        assert psk not in caplog.text
        assert all(psk not in path.read_text() for path in (tmp_path / "box").iterdir())

        # stop() ends everything of the carrier promptly.
        started = time.monotonic()
        worker.stop()
        await asyncio.wait_for(running, 2)
        assert time.monotonic() - started < 1
        assert carrier_tasks() == []
        assert all(link.closed for link in rig.links)


def test_sizes_and_order_over_the_real_carrier(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            worker = rig.worker()
            await rig.approved(worker)
            # The largest message passes; one byte more is refused before anything is sent (section 4.3).
            empty = len(encode({"type": "call", "id": "big", "cap": "demo.size", "args": {"text": ""}}))
            text = "x" * (MAX_MESSAGE - empty)
            assert (await rig.call("demo.size", {"text": text}, call_id="big"))["result"] == len(text)
            refused = await rig.call("demo.size", {"text": text + "x"}, call_id="big")
            assert refused["code"] == "message_too_large"
            # The carrier itself never hands the Channel a message over the limit.
            try:
                await rig.links[-1].send(b"x" * (MAX_MESSAGE + 1))
                raise AssertionError("sent")
            except ProtocolError:
                pass

            # Order on the link (D5): streams opened and closed while heartbeats race them. The hub never
            # closes a stream the worker still has, and closes every one the worker closed.
            opened.clear()
            storm = asyncio.Event()

            async def beat_storm():
                while not storm.is_set():
                    worker.changed()
                    await asyncio.sleep(0.005)
            storming = asyncio.create_task(beat_storm())
            names = []
            for n in range(9):
                names.append((await rig.call("demo.held"))["stream"])
                if n % 3 == 1:
                    await opened[n - 1].close()
            await asyncio.sleep(0.5)
            storm.set()
            await storming
            await until(lambda: rig.hub.get_worker(rig.guid)["streams"] == len(worker.streams))
            for name, stream in zip(names, opened):
                record = rig.hub.streams.get(name)
                assert record["open"] == stream.open, (name, record, stream.open)
                if not stream.open:
                    assert record["closed_by"] == "worker"

    asyncio.run(go())


def test_a_hub_that_never_answers_hello_is_a_failed_attempt(tmp_path, monkeypatch):
    """The exchange worked, but the Band's HELLO is never answered (here: every frame is lost). After
    PEER_TIMEOUT the link has ended, the attempt counts as failed, and the worker waits before the next."""
    monkeypatch.setattr(rook_link, "PEER_TIMEOUT", 0.5)

    async def go():
        async with Rig(tmp_path) as rig:
            worker = rig.worker()
            await rig.approved(worker)
            waits = []

            async def wait(seconds):
                waits.append(seconds)
                if len(waits) == 2:
                    worker.stop()
            worker._sleep = wait
            rig.proxy.stalled = True
            rig.links[-1].close()                       # the session is lost; every new one goes unanswered
            await asyncio.wait_for(rig.running[0][1], 10)
            assert len(waits) == 2 and len(worker.opens) == 3      # one per failed attempt, waits between
            assert all(link.closed for link in rig.links) and carrier_tasks() == []
            rig.running.clear()
    asyncio.run(go())


def test_a_worker_restart_takes_over_and_fails_the_call_in_flight(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            first = rig.worker()
            running = await rig.approved(first)
            in_flight = asyncio.create_task(rig.call("demo.slow"))
            await asyncio.sleep(0.3)
            first.stop()                                # the process ends: its Band says GOODBYE
            await asyncio.wait_for(running, 2)
            failed = await asyncio.wait_for(in_flight, 5)
            assert failed["code"] == "worker_disconnected"

            again = rig.worker()                        # the same state: the same identity (R5, W7)
            rig.run(again)
            await until(rig.online)
            assert len(again.opens) == 1 and again.key == first.key
            assert (await rig.call("demo.echo", {"back": True}))["result"] == {"back": True}
    asyncio.run(go())


def test_revoke_stops_the_worker_and_a_rekey_through_attach_takes_its_place(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            old = rig.worker("old")
            running = await rig.approved(old)
            moniker = rig.hub.get_worker(rig.guid)["moniker"]
            new = rig.worker("new")                     # a new key, as after a lost state directory
            rig.run(new)
            await until(lambda: any(p["key"] == new.key for p in rig.hub.list_pending()))
            rig.hub.attach(new.key, moniker, user=rig.alice)
            # The hub drops the old session's key without a word (section 3.3); the old worker learns when
            # its next message goes unacknowledged. Its next heartbeat is made now, not in 15 s.
            old.changed()
            try:
                await asyncio.wait_for(running, 10)
                raise AssertionError("the revoked worker kept running")
            except Refused as e:
                assert e.message["code"] == "key_revoked" and e.message["next"]
            await until(rig.online)
            assert rig.hub._links[rig.guid].key == new.key
            assert (await rig.call("demo.echo", {"rekeyed": True}))["result"] == {"rekeyed": True}
            rig.running = [(w, t) for w, t in rig.running if w is not old]
    asyncio.run(go())


def test_replay_against_a_restarted_worker(tmp_path):
    """Replay test 1 (section 9): what the hub sent during one session, re-sent to the worker's next
    session after a restart, runs nothing."""
    async def go():
        ran["count"] = 0
        async with Rig(tmp_path) as rig:
            first = rig.worker()
            running = await rig.approved(first)
            rig.proxy.recording = True
            assert (await rig.call("demo.count"))["result"] == 1
            rig.proxy.recording = False
            assert rig.proxy.recorded
            first.stop()
            await asyncio.wait_for(running, 2)
            await until(lambda: not rig.online())

            again = rig.worker()
            rig.run(again)
            await until(rig.online)
            await rig.proxy.inject(rig.proxy.recorded)
            await asyncio.sleep(1)
            assert ran["count"] == 1                    # the recorded call ran exactly once
            assert rig.online() and len(again.opens) == 1
            assert (await rig.call("demo.echo", {"undisturbed": True}))["result"] == {"undisturbed": True}
            assert ran["count"] == 1
    asyncio.run(go())


# ---------------------------------------------------------------- the two that wait for real timers


def test_the_hub_restarts_and_the_worker_is_back_within_20_s(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            worker = rig.worker()
            await rig.approved(worker)
            stopped = time.monotonic()
            # A hub process that stops closes its connections at once; an in-process stop_serving waits for
            # open /band connections to end (about 20 s here), so the proxy cuts them as the process would.
            # No GOODBYE reaches the worker: the hub removes session keys before it stops its Endpoint.
            await asyncio.gather(rig.hub.stop_serving(), rig.proxy.cut())
            rig.hub.close()
            await rig.serve(rig.port)                  # the same data directory and address
            await until(rig.online, timeout=25)
            back = time.monotonic() - stopped
            print(f"worker back {back:.1f} s after the hub stopped")
            assert back < 20 and len(worker.opens) >= 2
            assert (await rig.call("demo.echo", {"back": True}))["result"] == {"back": True}
    asyncio.run(go())


def test_a_stall_of_20_s_makes_a_new_session(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            worker = rig.worker()
            await rig.approved(worker)
            first = rig.hub.sessions.live(worker.key)
            rig.proxy.stalled = True
            await asyncio.sleep(20)
            rig.proxy.stalled = False
            await until(lambda: rig.hub.sessions.live(worker.key) not in (None, first) and rig.online(), timeout=15)
            assert len(worker.opens) == 2                  # one new link_open: the session was lost (3.2)
            assert (await rig.call("demo.echo", {"after": "20 s"}))["result"] == {"after": "20 s"}
    asyncio.run(go())
