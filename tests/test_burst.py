"""A burst of calls waits for the link instead of being lost (brief 49; docs/stress.md, fault 1).

The hub runs on one asyncio loop. Requests that arrive together used to run back to back in one turn
of it, and the hub read nothing from the workers' links meanwhile: about 10,000 calls at once held it
for 5 s, the worker's Telesthete Channel heard no acknowledgement for ten retransmits and gave up on
the link, and the calls were answered worker_disconnected. Now each request takes its turn
(web.Turns) and the links are read between any two.

The worker here is the real one, over the real carrier, on a thread and an event loop of its own, as
a worker on another machine is: its Channel counts the hub's silence in real time. Each call's work on
the hub's loop is slowed to 20 ms, so that 400 calls at once hold the loop as long as about 10,000 do
here at full speed (8 s, past the Channel's 5.5 s), quickly and on any machine. The worker keeps
sending all the while (a stream writing every 10 ms), so it always has a frame waiting to be
acknowledged, as a worker answering calls does."""

import asyncio
import json
import threading
import time

import aiohttp

from rook_hub import Hub
from rook_worker import Cap, Worker

CALLS = 400
SLOW = 0.02     # seconds of work on the hub's loop for each call: 400 of them are 8 s


async def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def echo(args):
    return args


async def chatter(args, stream):
    async def run():
        while stream.open:
            await stream.send(b".")
            await asyncio.sleep(0.01)
    asyncio.create_task(run())
    return {"title": "chatter", "description": "Writes a dot every 10 ms until it is closed."}


CAPS = {"demo.echo": Cap("Echo.", {"type": "object"}, echo),
        "demo.chatter": Cap("Keeps writing.", {"type": "object"}, chatter, opens_stream=True)}


class Elsewhere:
    """A real worker on a thread with its own event loop: busy as the hub's loop gets, this one is not."""

    def __init__(self, state_dir, hub_url, token):
        self.state_dir, self.hub_url, self.token = state_dir, hub_url, token
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            self.worker = Worker(self.state_dir, self.hub_url, name="box", caps=CAPS, macs=[], knock_every=0.02,
                                 token=self.token)
            self.started.set()
            await self.worker.run()
        asyncio.run(main())

    def start(self):
        self.thread.start()
        self.started.wait()
        return self.worker

    async def stop(self):
        """Stop the worker and wait for its thread, without holding the hub's loop meanwhile."""
        self.loop.call_soon_threadsafe(self.worker.stop)
        await asyncio.to_thread(self.thread.join)


def test_a_burst_of_calls_waits_for_the_link(tmp_path, monkeypatch):
    asyncio.run(burst(tmp_path, monkeypatch))


async def burst(tmp_path, monkeypatch):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    elsewhere = Elsewhere(tmp_path / "box", base, hub.auth.mint("join", user=alice))
    worker = elsewhere.start()
    try:
        await until(lambda: worker.identity is not None and hub._links.get(worker.identity["guid"]) is not None
                    and worker._replied)
        guid = worker.identity["guid"]
        session = hub.sessions.live(worker.key)
        opened = await hub.call(guid, "demo.chatter", {}, "Keep the worker sending.", user=alice)
        assert opened["type"] == "stream_open"

        journal = hub._journal

        def slowly(*args, **kwargs):        # each call's work on the hub's loop takes SLOW seconds
            time.sleep(SLOW)
            return journal(*args, **kwargs)
        monkeypatch.setattr(hub, "_journal", slowly)

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0),
                                         timeout=aiohttp.ClientTimeout(total=None)) as http:
            async def call(n):
                body = {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": "call", "arguments": {
                    "worker": guid, "cap": "demo.echo", "args": {"n": n}, "why": "One of a burst."}}}
                async with http.post(base + "/mcp", json=body, headers={"Authorization": f"Bearer {agent}"}) as r:
                    result = (await r.json())["result"]
                return result["isError"], result["content"][0]["text"]
            replies = await asyncio.gather(*(call(n) for n in range(CALLS)))

        # Every call answered by the worker, none lost, and the link never reset: the same session.
        failed = [text for is_error, text in replies if is_error]
        assert failed == [], f"{len(failed)} of {CALLS} failed, the first: {failed[0]}"
        assert [json.loads(text) for _, text in replies] == [{"n": n} for n in range(CALLS)]
        assert hub.sessions.live(worker.key) == session
    finally:
        await elsewhere.stop()
        await hub.stop_serving()
        hub.close()


def test_turns_take_requests_in_order_one_per_turn_of_the_loop():
    asyncio.run(in_turn())


async def in_turn():
    from rook_hub.web import Turns
    turns, went, ticks = Turns(), [], [0]

    def tick():                     # counts the loop's turns: one call_soon each
        ticks[0] += 1
        asyncio.get_running_loop().call_soon(tick)
    asyncio.get_running_loop().call_soon(tick)

    async def request(n):
        await turns.take()
        went.append((n, ticks[0]))

    # Five at once: each in the order it came, and no two in the same turn of the loop.
    await asyncio.gather(*(request(n) for n in range(5)))
    assert [n for n, _ in went] == [0, 1, 2, 3, 4]
    assert len({tick for _, tick in went}) == 5

    # One that stops waiting (its client went away) passes its turn on: the one after it still goes.
    went.clear()
    first, second, third = (asyncio.create_task(request(n)) for n in range(3))
    await asyncio.sleep(0)
    second.cancel()
    await asyncio.gather(first, third, return_exceptions=True)
    assert [n for n, _ in went] == [0, 2] and second.cancelled()

    # With none waiting, a request goes at once.
    went.clear()
    for _ in range(3):
        await asyncio.sleep(0)
    now = ticks[0]
    await request(9)
    assert went == [(9, now)]
