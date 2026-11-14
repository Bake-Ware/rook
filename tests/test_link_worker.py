"""The worker's side of the link exchange (docs/link-design.md 2.5, 3.1 to 3.3, 4.3, 6.1, 9, 10 row L3;
R5, W7, W2, S2, U8): the `/link` client and its checks against the real hub over real HTTP,
`Worker.run` as a loop with its backoff, stopping it at any point, stream output split at
65,536 bytes, results over the size limit, and the decoder's answer to JSON nested too deep.
The links are in-memory: the Telesthete carrier is L5. Backoff waits are recorded, never slept."""

import asyncio
import base64
import socket
import threading
import time

import aiohttp
import pytest
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from helpers import eventually
from memory_link import connector, link_pair
from rook_hub import Hub
from rook_protocol import (MAX_MESSAGE, MAX_STREAM_DATA, ProtocolError, decode, encode, sign_link_accept,
                           sign_link_key)
from rook_worker import Cap, Refused, Session, Worker
from test_hub import heartbeat
from test_link_exchange import Keys

SMALL_ORDER = "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"
DEEP = b"[" * 100_000 + b"]" * 100_000


async def echo(args):
    return args


async def huge(args):
    return "x" * MAX_MESSAGE                     # its result message is larger than any message may be


async def chatty(args, stream):
    async def run():
        await stream.send(bytes(i % 251 for i in range(200_000)))
    asyncio.create_task(run())
    return {"title": "chatty", "description": "Sends 200,000 bytes at once."}


CAPS = {"demo.echo": Cap("Echo.", {"type": "object"}, echo), "demo.huge": Cap("Too big.", {"type": "object"}, huge),
        "demo.chatty": Cap("A stream.", {"type": "object"}, chatty, opens_stream=True)}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recording:
    """A worker's connect: the sessions it was handed, and an in-memory link to the hub for each."""

    def __init__(self, hub):
        self.sessions, self.connect_to = [], connector(hub)

    async def __call__(self, session):
        self.sessions.append(session)
        return await self.connect_to(session)


async def approved_worker(hub, tmp_path, base, alice, **options):
    """A real worker, knocked and approved over HTTP, linked and online."""
    connect = Recording(hub)
    worker = Worker(tmp_path / "box", base, connect, name="box", caps=dict(CAPS), macs=[], knock_every=0.02,
                    **options)
    asyncio_task = asyncio.create_task(worker.run())
    await eventually(hub.list_pending)
    guid = hub.approve(worker.key, user=alice)["guid"]
    await eventually(lambda: hub._links.get(guid))
    return worker, asyncio_task, connect, guid


# ---------------------------------------------------------------- against the real hub


def test_a_session_through_the_real_hub_and_a_new_one_after_each_loss(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        keys = hub.sessions.keys = Keys()
        host, port = await hub.serve("127.0.0.1", 0)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        worker, running, connect, guid = await approved_worker(hub, tmp_path, f"http://{host}:{port}", alice)
        try:
            # Both sides hold the same key; the carrier is told where the hub serves links (2.3).
            [first] = connect.sessions
            assert keys.added == [first.psk] and first.key == worker.key
            assert first.band_url == f"ws://{host}:{port}/band"
            assert first.psk not in repr(first)                                 # never printed
            assert first.psk not in (tmp_path / "box").joinpath("identity.json").read_text()
            assert sorted(p.name for p in (tmp_path / "box").iterdir()) == ["identity.json", "key", "link_key"]

            # A stream stays open across a lost link: the worker did not restart (S2, D5).
            reply = await hub.call(guid, "demo.chatty", {}, "a stream", user=alice)
            stream = reply["stream"]
            await eventually(lambda: worker.streams)

            # The link is lost: a new session, never the old key, and a new link; calls work again.
            for n in (2, 3):
                hub._links[guid].close()
                await eventually(lambda: len(connect.sessions) == n and hub._links.get(guid) is not None)
                psks = [s.psk for s in connect.sessions]
                assert len(set(psks)) == n and all(psk in keys.added for psk in psks)
                reply = await hub.call(guid, "demo.echo", {"n": n}, "after a loss", user=alice)
                assert reply["result"] == {"n": n}
            assert hub.streams.get(stream)["open"]                          # listed in open_streams, kept
            assert worker.heartbeat()["open_streams"] == sorted(worker.streams) and len(worker.streams) == 1
        finally:
            worker.stop()
            await asyncio.wait_for(running, 2)
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


def test_refusals_stop_the_worker_without_a_retry(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        worker, running, connect, guid = await approved_worker(hub, tmp_path, f"http://{host}:{port}", alice)
        posts, post = [], worker._post

        def counted(body, path="/knock"):
            posts.append(path)
            return post(body, path)
        worker._post = counted
        try:
            # key_revoked: a rekey attaches a new key to this worker; the old one's next link_open is refused.
            other, link = SigningKey.generate(), PrivateKey.generate().public_key.encode().hex()
            hub.knock({"type": "knock", "protocol": 1, "name": "box", "key": other.verify_key.encode().hex(),
                       "macs": [], "caps": [], "link_key": link, "link_signature": sign_link_key(other, link)})
            hub.attach(other.verify_key.encode().hex(), guid, user=alice)       # closes the old key's link
            with pytest.raises(Refused) as refused:
                await asyncio.wait_for(running, 5)
            assert refused.value.message["code"] == "key_revoked" and refused.value.message["next"]
            assert posts == ["/link"]                                           # once: no retry

            # unknown_key: a hub that never approved this key.
            stranger = Worker(tmp_path / "stranger", f"http://{host}:{port}", connector(hub), macs=[])
            stranger.identity = {"guid": "g", "moniker": "m", "signature": "s", "hub_key": hub.public_key}
            tries = []
            stranger._sleep = lambda seconds: tries.append(seconds) or asyncio.sleep(0)
            with pytest.raises(Refused) as refused:
                await asyncio.wait_for(stranger.run(), 5)
            assert refused.value.message["code"] == "unknown_key" and refused.value.message["next"] and tries == []
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


def test_a_hub_that_is_down_is_retried_on_the_designs_schedule(tmp_path):
    async def go():
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        hub = Hub(tmp_path / "hub")
        await hub.serve("127.0.0.1", port)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        worker, running, connect, guid = await approved_worker(hub, tmp_path, base, alice)
        waits, sent, post = [], [], worker._post

        async def recorded_sleep(seconds):
            waits.append(seconds)
            await asyncio.sleep(0)

        def recorded_post(body, path="/knock"):
            sent.append(decode(body))
            return post(body, path)
        worker._sleep, worker._post = recorded_sleep, recorded_post
        try:
            await hub.stop_serving()                     # the hub goes down; the worker's link is lost
            hub.close()
            await eventually(lambda: len(waits) >= 9, timeout=20)
            hub = Hub(tmp_path / "hub")                  # and comes back on the same address
            worker.connect = connector(hub)
            await hub.serve("127.0.0.1", port)
            await eventually(lambda: hub._links.get(guid))
            assert hub.get_worker(guid)["online"]
            # 0.5 s doubling to 30 s, each within 20 % either way, and not all exactly on the base.
            bases = [0.5, 1, 2, 4, 8, 16, 30, 30, 30]
            for wait, base_wait in zip(waits, bases):
                assert base_wait * 0.8 <= wait <= base_wait * 1.2, (wait, base_wait)
            assert any(wait != base_wait for wait, base_wait in zip(waits, bases))
            # Every attempt was a new link_open with a new ephemeral, failed ones included.
            ephemerals = [m["ephemeral"] for m in sent if m["type"] == "link_open"]
            assert len(ephemerals) >= len(waits) + 1 and len(set(ephemerals)) == len(ephemerals)
        finally:
            worker.stop()
            await asyncio.wait_for(running, 2)
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


# ---------------------------------------------------------------- the checks on link_accept (2.5)


def answering(hub_signer, how):
    """A worker's _post for /link that answers each link_open with a link_accept spoiled as `how` says."""
    sent = []

    def post(body, path="/knock"):
        assert path == "/link"
        opening = decode(body)
        sent.append(opening)
        ephemeral = SMALL_ORDER if how == "small order" else PrivateKey.generate().public_key.encode().hex()
        link_key = PrivateKey.generate().public_key.encode().hex()
        answered = PrivateKey.generate().public_key.encode().hex() if how == "another ephemeral" else opening["ephemeral"]
        signer = SigningKey.generate() if how == "another key" else hub_signer
        return {"type": "link_accept", "protocol": 1, "ephemeral": ephemeral, "hub_link_key": link_key,
                "signature": sign_link_accept(signer, opening["key"], answered, ephemeral, link_key)}
    return post, sent


@pytest.mark.parametrize("how", ["another ephemeral", "another key", "small order", "good"])
def test_a_link_accept_that_fails_a_check_is_never_used(tmp_path, how):
    async def go():
        hub_signer = SigningKey.generate()
        used = []

        async def connect(session):
            used.append(session)
            worker.stop()                    # a good answer is used once; that is all this test needs
            return link_pair(session.key)[1]

        worker = Worker(tmp_path, "http://unused", connect, macs=[])
        worker.identity = {"guid": "g", "moniker": "m", "signature": "s",
                           "hub_key": hub_signer.verify_key.encode().hex()}
        worker._post, sent = answering(hub_signer, how)
        waits = []

        async def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3 or used:
                worker.stop()
        worker._sleep = wait
        await asyncio.wait_for(worker.run(), 5)
        if how == "good":
            assert len(used) == 1 and len(used[0].psk) == 64 and waits == []
        else:
            assert used == [] and len(sent) == 3 and len(waits) == 3       # each attempt failed, and was retried
            assert len({m["ephemeral"] for m in sent}) == 3
    asyncio.run(go())


# ---------------------------------------------------------------- stop() at any point


def test_stop_ends_run_promptly_wherever_it_is(tmp_path):
    async def go():
        # During the exchange: the hub has not answered the link_open yet.
        release = threading.Event()

        def hanging(body, path="/knock"):
            release.wait(10)
            raise OSError("released")

        async def stop_later(worker, check):
            await eventually(check)
            started = time.monotonic()
            worker.stop()
            return started

        for where in ("exchange", "backoff"):
            worker = Worker(tmp_path / where, "http://unused", connector(None), macs=[])
            worker.identity = {"guid": "g", "moniker": "m", "signature": "s",
                               "hub_key": SigningKey.generate().verify_key.encode().hex()}
            waiting = []
            if where == "exchange":
                calls = []
                worker._post = lambda body, path="/knock": calls.append(path) or hanging(body, path)
                check = lambda: calls
            else:
                def refused(body, path="/knock"):
                    raise OSError("connection refused")
                worker._post = refused

                async def long_wait(seconds):
                    waiting.append(seconds)
                    await asyncio.sleep(3600)
                worker._sleep = long_wait
                check = lambda: waiting
            running = asyncio.create_task(worker.run())
            started = await stop_later(worker, check)
            await asyncio.wait_for(running, 1)
            assert time.monotonic() - started < 0.5, where
        release.set()

        # During a session: shown with a real hub.
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        worker, running, connect, guid = await approved_worker(hub, tmp_path, f"http://{host}:{port}", alice)
        started = time.monotonic()
        worker.stop()
        await asyncio.wait_for(running, 1)
        assert time.monotonic() - started < 0.5 and len(connect.sessions) == 1
        await hub.stop_serving()
        hub.close()
    asyncio.run(go())


# ---------------------------------------------------------------- sizes (docs/protocol.md "Sizes")


def test_stream_output_is_split_and_a_result_over_the_limit_fails(tmp_path):
    async def go():
        hub_end, worker_end = link_pair("k")

        async def connect(session):
            return worker_end

        async def one_session():
            return Session(worker.key, "0" * 64, "ws://unused/band")

        worker = Worker(tmp_path, "http://unused", connect, caps=dict(CAPS), macs=[])
        worker.identity = {"guid": "g", "moniker": "m", "signature": "s", "hub_key": "0" * 64}
        worker._open_session = one_session
        running = asyncio.create_task(worker.run())
        assert decode(await hub_end.recv())["type"] == "heartbeat"

        async def next_of(kind):
            while (message := decode(await hub_end.recv()))["type"] != kind:
                pass
            return message

        # 200,000 bytes of output: messages of at most 65,536 bytes of data, in order and complete.
        await hub_end.send(encode({"type": "call", "id": "c1", "cap": "demo.chatty", "args": {}}))
        assert (await next_of("stream_open"))["id"] == "c1"
        pieces = []
        while sum(map(len, pieces)) < 200_000:
            pieces.append(base64.b64decode((await next_of("stream_data"))["data"]))
        assert [len(p) for p in pieces] == [MAX_STREAM_DATA] * 3 + [200_000 - 3 * MAX_STREAM_DATA]
        assert b"".join(pieces) == bytes(i % 251 for i in range(200_000))

        # A result over 16,000,000 bytes encoded: cap_failed, and the link carries on.
        await hub_end.send(encode({"type": "call", "id": "c2", "cap": "demo.huge", "args": {}}))
        failed = await next_of("error")
        assert failed["id"] == "c2" and failed["code"] == "cap_failed" and str(MAX_MESSAGE) in failed["message"]
        await hub_end.send(encode({"type": "call", "id": "c3", "cap": "demo.echo", "args": {"after": True}}))
        assert await next_of("result") == {"type": "result", "id": "c3", "result": {"after": True}}
        worker.stop()
        await asyncio.wait_for(running, 1)
    asyncio.run(go())


# ---------------------------------------------------------------- JSON nested too deep (review of 414)


def test_json_nested_too_deep_is_a_bad_message(tmp_path):
    with pytest.raises(ProtocolError, match="nested too deep"):
        decode(DEEP)

    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as client:
                for path in ("/knock", "/link"):
                    async with client.post(f"http://{host}:{port}{path}", data=DEEP) as response:
                        reply = decode(await response.read())
                    assert response.status == 400 and reply["code"] == "bad_message", path
            # Over a link: the hub answers bad_message and goes on serving it.
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            identity, link = SigningKey.generate(), PrivateKey.generate().public_key.encode().hex()
            key = identity.verify_key.encode().hex()
            hub.knock({"type": "knock", "protocol": 1, "name": "box", "key": key, "macs": [], "caps": [],
                       "link_key": link, "link_signature": sign_link_key(identity, link)})
            hub.approve(key, user=alice)
            hub_end, worker_end = link_pair(key)
            serving = asyncio.create_task(hub.serve_link(hub_end))
            await worker_end.send(DEEP)
            assert decode(await worker_end.recv())["code"] == "bad_message"
            await worker_end.send(encode(heartbeat(key)))
            assert decode(await worker_end.recv())["type"] == "heartbeat_reply"
            assert not serving.done()
            worker_end.close()
            await asyncio.wait_for(serving, 1)
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


# ---------------------------------------------------------------- review of 416: back off unless a session worked


def test_a_link_lost_before_any_heartbeat_reply_is_a_failed_attempt(tmp_path):
    """A session has worked only once its link delivered a heartbeat_reply (link design 3.2, as
    Telesthete's WebSocket client backs off). Sessions here are scripted: "dies" is a link already
    closed; "works" answers one heartbeat, then is lost."""
    async def go():
        script = ["dies", "dies", "dies", "works", "dies", "works", "works", "dies", "dies"]
        exchanges, waits, events = [], [], []

        async def open_session():
            exchanges.append(len(exchanges))
            return Session(worker.key, "0" * 64, "ws://unused/band")

        async def connect(session):
            how = script[len(exchanges) - 1]
            events.append(how)
            hub_end, worker_end = link_pair(session.key)
            if how == "dies":
                hub_end.close()
            else:
                async def answer_once():
                    assert decode(await hub_end.recv())["type"] == "heartbeat"
                    await hub_end.send(encode({"type": "heartbeat_reply", "protocol": 1, "interval": 15}))
                    await asyncio.sleep(0.01)
                    hub_end.close()
                asyncio.create_task(answer_once())
            return worker_end

        async def wait(seconds):
            waits.append((len(exchanges), seconds))
            if len(exchanges) == len(script):
                worker.stop()

        worker = Worker(tmp_path, "http://unused", connect, macs=[])
        worker.identity = {"guid": "g", "moniker": "m", "signature": "s", "hub_key": "0" * 64}
        worker._open_session, worker._sleep = open_session, wait
        await asyncio.wait_for(worker.run(), 5)

        # A wait after each session that died, growing; none after one that worked, and the wait
        # starts again from 0.5 s after a session that worked.
        assert events == script
        after = [n for n, _ in waits]
        assert after == [1, 2, 3, 5, 8, 9]
        for (_, seconds), base in zip(waits, [0.5, 1, 2, 0.5, 0.5, 1]):
            assert base * 0.8 <= seconds <= base * 1.2, (seconds, base)
    asyncio.run(go())


def test_a_link_that_always_dies_at_once_makes_few_exchanges(tmp_path):
    """The hot loop the review found: a carrier whose link is closed at once. Against the real hub, the
    worker now backs off instead of making a session every few milliseconds."""
    async def go():
        hub = Hub(tmp_path / "hub")
        keys = hub.sessions.keys = Keys()
        host, port = await hub.serve("127.0.0.1", 0)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        worker, running, connect, guid = await approved_worker(hub, tmp_path, f"http://{host}:{port}", alice)
        waits = []

        async def short_wait(seconds):
            waits.append(seconds)
            await asyncio.sleep(0.05)          # a fixed short wait, so the test runs fast but not hot

        async def dead(session):
            hub_end, worker_end = link_pair(session.key)
            worker_end.close()
            return worker_end
        try:
            worker._sleep, worker.connect = short_wait, dead
            before = len(keys.added)
            hub._links[guid].close()           # the working session is lost: one exchange at once, then backoff
            await asyncio.sleep(0.5)
            made = len(keys.added) - before
            assert 3 <= made <= 12, made       # about one per 50 ms wait, not one every 2 ms
            assert len(waits) >= made - 1 and waits[:3] == sorted(waits[:3])
            assert waits[0] <= 0.6 and waits[2] >= 1.6
        finally:
            worker.stop()
            await asyncio.wait_for(running, 2)
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


# ---------------------------------------------------------------- review of 416: HTTP_TIMEOUT


def test_a_hub_that_never_answers_times_out(tmp_path, monkeypatch):
    import rook_worker
    monkeypatch.setattr(rook_worker, "HTTP_TIMEOUT", 0.2)

    async def go():
        held = []

        async def accept_and_say_nothing(reader, writer):
            held.append(writer)                # keep the connection open, answer nothing

        server = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            # /link: a timeout is a failed attempt, like an unreachable hub, and is retried after a wait.
            worker = Worker(tmp_path / "linking", f"http://127.0.0.1:{port}", connector(None), macs=[])
            worker.identity = {"guid": "g", "moniker": "m", "signature": "s",
                               "hub_key": SigningKey.generate().verify_key.encode().hex()}
            waits = []

            async def wait(seconds):
                waits.append(seconds)
                if len(waits) == 2:
                    worker.stop()
            worker._sleep = wait
            started = time.monotonic()
            await asyncio.wait_for(worker.run(), 5)
            assert len(waits) == 2 and 0.4 <= time.monotonic() - started < 2

            # The knock: as an unreachable hub does today, the timeout ends run with the error.
            knocking = Worker(tmp_path / "knocking", f"http://127.0.0.1:{port}", connector(None), macs=[])
            started = time.monotonic()
            with pytest.raises(OSError):
                await asyncio.wait_for(knocking.run(), 5)
            assert time.monotonic() - started < 2
        finally:
            for writer in held:
                writer.close()
            server.close()
            await server.wait_closed()
    asyncio.run(go())
