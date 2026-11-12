"""Hub edges the lifecycle scenario does not reach."""

import asyncio
import json
import urllib.error
import urllib.request

import pytest
from nacl.signing import SigningKey

from helpers import Clock
from memory_link import link_pair
from rook_hub import Hub, HubError
from rook_protocol import decode, encode

ECHO = {"name": "demo.echo", "description": "Echo.", "schema": {"type": "object"}, "returns": "result"}
SHELL = {"name": "shell.open", "description": "Terminal.", "schema": {"type": "object"}, "returns": "stream"}


def new_key():
    return SigningKey.generate().verify_key.encode().hex()


def knock(key, name="box", macs=(), caps=(ECHO,)):
    return {"type": "knock", "protocol": 1, "name": name, "key": key, "macs": list(macs), "caps": list(caps)}


def heartbeat(key, name="box", caps=(ECHO,), hostname="box", **extra):
    return dict({"type": "heartbeat", "protocol": 1, "identity": {"name": name, "key": key},
                 "facts": {"os": "linux", "arch": "x86_64", "hostname": hostname, "form": "test",
                           "version": "0", "macs": []},
                 "caps": list(caps), "band": {"gateway_mac": None}, "health": {}}, **extra)


@pytest.fixture
def hub(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    hub.bake = hub.auth.create_user("Bake", "a password")["guid"]
    yield hub
    hub.close()


def approved(hub, name="box", macs=()):
    key = new_key()
    hub.knock(knock(key, name, macs))
    return key, hub.approve(key, user=hub.bake)


async def connected(hub, key, **beat):
    """A hand-driven worker end of a link that has sent one heartbeat."""
    hub_end, worker_end = link_pair(key)
    asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
    await worker_end.send(encode(heartbeat(key, **beat)))
    return worker_end, decode(await worker_end.recv())


def test_repeated_knock_keeps_one_pending_entry(hub):
    key = new_key()
    assert hub.knock(knock(key))["status"] == "pending"
    hub.clock.advance(5)
    assert hub.knock(knock(key, caps=(ECHO, SHELL)))["status"] == "pending"
    [entry] = hub.list_pending()
    assert [c["name"] for c in entry["caps"]] == ["demo.echo", "shell.open"]
    assert entry["last_at"] == entry["first_at"] + 5


def test_likely_rekey_by_mac_hostname_or_moniker(hub):
    _, record = approved(hub, "kaiju", macs=["AA:BB:CC:00:00:01"])
    hub.knock(knock(new_key(), "renamed", macs=["aa:bb:cc:00:00:01"]))
    hub.knock(knock(new_key(), record["moniker"]))
    hub.knock(knock(new_key(), "stranger", macs=["aa:bb:cc:00:00:99"]))
    flags = {p["name"]: p["likely_rekey_of"] for p in hub.list_pending()}
    assert flags["renamed"] == [{"guid": record["guid"], "moniker": record["moniker"],
                                 "matched": ["mac aa:bb:cc:00:00:01"]}]
    assert flags[record["moniker"]][0]["matched"] == [f"moniker {record['moniker']}"]
    assert flags["stranger"] == []


def test_likely_rekey_by_reported_hostname(hub):
    key, record = approved(hub, "display-name")

    async def go():
        await connected(hub, key, name="display-name", hostname="old-host")
    asyncio.run(go())
    assert hub.seen(record["guid"])["hostnames"] == ["old-host"]
    hub.knock(knock(new_key(), "old-host"))
    assert hub.list_pending()[0]["likely_rekey_of"][0]["matched"] == ["hostname old-host"]


def test_approve_and_attach_need_a_pending_key(hub):
    with pytest.raises(HubError) as e:
        hub.approve(new_key(), user=hub.bake)
    assert e.value.message["code"] == "unknown_key" and e.value.message["next"]
    key = new_key()
    hub.knock(knock(key))
    with pytest.raises(HubError) as e:
        hub.attach(key, "no-such-worker", user=hub.bake)
    assert e.value.message["code"] == "unknown_worker"
    assert len(hub.list_pending()) == 1


def test_monikers_are_unique_three_words(hub):
    adjectives, nouns = set(hub._adjectives), set(hub._nouns)
    monikers = [approved(hub)[1]["moniker"] for _ in range(300)]
    assert len(set(monikers)) == 300
    for moniker in monikers:
        first, second, noun = moniker.split("-")
        assert first in adjectives and second in adjectives and first != second and noun in nouns


def test_hub_restart_keeps_workers_keys_and_tokens(tmp_path):
    hub = Hub(tmp_path / "hub")
    hub.bake = hub.auth.create_user("Bake", "a password")["guid"]
    key, record = approved(hub)
    agent = hub.auth.mint("agent", user=hub.bake)
    public_key = hub.public_key
    hub.close()
    hub = Hub(tmp_path / "hub")
    assert hub.public_key == public_key
    [bake] = hub.auth.list_users()
    assert hub.get_worker(record["moniker"])["owner"] == bake["guid"]
    assert hub.knock(knock(key))["guid"] == record["guid"]
    assert hub.auth.verify(agent)["purpose"] == "agent"
    hub.close()


def test_link_refuses_unknown_and_mismatched_keys(hub):
    async def go():
        stranger = new_key()
        link, reply = await connected(hub, stranger)
        assert reply["code"] == "unknown_key" and reply["next"]
        with pytest.raises(EOFError):
            await link.recv()

        key, _ = approved(hub)
        hub_end, worker_end = link_pair(key)
        asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
        await worker_end.send(encode(heartbeat(new_key())))
        assert decode(await worker_end.recv())["code"] == "bad_message"
    asyncio.run(go())


def test_bad_message_is_answered_and_the_link_stays(hub):
    async def go():
        key, record = approved(hub)
        link, reply = await connected(hub, key)
        assert reply["type"] == "heartbeat_reply"
        await link.send(b"{not json")
        assert decode(await link.recv())["code"] == "bad_message"
        await link.send(encode(heartbeat(key)))
        assert decode(await link.recv())["type"] == "heartbeat_reply"
    asyncio.run(go())


def test_newer_worker_fields_are_accepted_and_kept_raw(hub):
    # U8: the hub records the worker's version and ignores fields it does not know.
    async def go():
        key, record = approved(hub)
        link, reply = await connected(hub, key, protocol=2, later_field={"x": 1})
        assert reply == {"type": "heartbeat_reply", "protocol": 1, "interval": 15}
        assert hub.get_worker(record["guid"])["protocol"] == 2
        [body] = hub._db.execute("SELECT body FROM heartbeats").fetchone()
        assert json.loads(body)["later_field"] == {"x": 1}
    asyncio.run(go())


def test_call_fails_cleanly_when_the_link_drops(hub):
    async def go():
        key, record = approved(hub)
        link, _ = await connected(hub, key)
        call = asyncio.create_task(hub.call(record["guid"], "demo.echo", {}, "drop mid-call", user=hub.bake))
        assert decode(await link.recv())["type"] == "call"
        link.close()
        reply = await call
        assert reply["code"] == "worker_disconnected" and reply["next"]
    asyncio.run(go())


def test_stream_cap_call_returns_the_stream_handle(hub):
    # S1: opening a stream is a normal call; its reply is the handle.
    async def go():
        key, record = approved(hub)
        link, _ = await connected(hub, key, caps=(SHELL,))
        call = asyncio.create_task(hub.call(record["guid"], "shell.open", {}, "open a shell", user=hub.bake))
        request = decode(await link.recv())
        await link.send(encode({"type": "stream_open", "id": request["id"], "stream": "s1",
                                "title": "shell", "description": "a test shell"}))
        reply = await call
        assert reply["type"] == "stream_open" and reply["stream"] == "s1" and reply["title"] == "shell"
    asyncio.run(go())


def test_bad_call_arguments_are_an_error_not_an_exception(hub):
    async def go():
        key, record = approved(hub)
        await connected(hub, key)
        reply = await hub.call(record["guid"], "demo.echo", ["not", "an", "object"], "bad args", user=hub.bake)
        assert reply["code"] == "bad_message" and reply["next"]
    asyncio.run(go())


def test_knock_endpoint_rejects_bad_requests(hub):
    asyncio.run(knock_endpoint(hub))


async def knock_endpoint(hub):
    host, port = await hub.serve("127.0.0.1", 0)

    def send(body):
        request = urllib.request.Request(f"http://{host}:{port}/knock", data=body, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, decode(response.read())
        except urllib.error.HTTPError as e:
            return e.code, decode(e.read())

    async def post(body):
        return await asyncio.to_thread(send, body)

    try:
        status, reply = await post(b"{}")
        assert status == 400 and reply["code"] == "bad_message"
        status, reply = await post(encode(heartbeat(new_key())))
        assert status == 400 and "expected a knock" in reply["message"]
        status, reply = await post(encode(knock(new_key())))
        assert status == 200 and reply["status"] == "pending"
    finally:
        await hub.stop_serving()
