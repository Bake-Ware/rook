"""Worker config (CF1, CF2, CF3, K5; X2, X3, L4, K1, K2): a config record per worker,
read and set through the hub caps config.get and config.set, shown on the Data page,
sent to the worker when it links and when it changes, applied there and acknowledged
in its heartbeat, with secret:<name> put in on the way and nowhere else. A real hub,
served, and a real rook_worker over its real carrier (Telesthete over loopback WebSocket)."""

import asyncio
import json
import os

from helpers import eventually
from rook_hub import Hub, config, journal, records
from rook_worker import Worker
from test_site import ask, browser, login

SECRET = "sk-live-4b1d9e77c0ffee-only-in-the-vault"     # a secret's value: never stored as config, never shown
WHY = "a test of worker config"


class Rig:
    """A served hub with its vault open, Alice, and one real worker it approved."""

    def __init__(self, tmp_path, key):
        self.tmp_path, self.key = tmp_path, key

    async def start_hub(self):
        self.hub = Hub(self.tmp_path / "hub", vault_key=self.key)
        host, port = await self.hub.serve("127.0.0.1", 0)
        self.base = f"http://{host}:{port}"

    async def stop_hub(self):
        await self.hub.stop_serving()
        self.hub.close()

    def start_worker(self):
        """The worker as a new process would be: the same state directory, nothing in memory."""
        self.worker = Worker(self.tmp_path / "box", self.base, name="box", macs=[], knock_every=0.02)
        self.task = asyncio.create_task(self.worker.run())

    async def stop_worker(self):
        self.worker.stop()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)

    async def cap(self, cap, **args):
        reply = await self.hub.call("hub", cap, args, WHY, user=self.alice)
        assert reply["type"] == "result", reply
        return reply["result"]

    def applied(self):
        """The version the hub last heard the worker applied."""
        return self.hub.get_worker(self.guid)["config_version"]


def test_worker_config(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    rig = Rig(tmp_path, os.urandom(32))
    await rig.start_hub()
    hub = rig.hub
    rig.alice = hub.auth.create_user("Alice", "a password")["guid"]
    rig.start_worker()
    await eventually(lambda: any(p["key"] == rig.worker.key for p in hub.list_pending()))
    rig.guid = hub.approve(rig.worker.key, user=rig.alice)["guid"]
    moniker = hub.get_worker(rig.guid)["moniker"]

    # CF1, K1: approval makes the worker's config record, owned by its owner, with nothing set.
    [made] = records.search(hub, type=config.TYPE)
    assert made["body"] == {"worker": rig.guid, "values": {}}
    assert made["owner"] == made["creator"] == rig.alice and made["title"] == f"Config of {moniker}"
    assert {"type": "config", "plugin": "core"} in records.list_types(hub)     # declared and owned (K2)

    # Linked with nothing set: the worker holds no config and should have none, so nothing is sent.
    empty = config.version({})
    await eventually(lambda: rig.hub._links.get(rig.guid) is not None and rig.hub.get_worker(rig.guid)["online"])
    await asyncio.sleep(0.2)
    assert rig.worker.config == {} and rig.worker.config_version is None and rig.applied() is None

    # The record's owner follows the worker's when it is reassigned (W6); its creator stays.
    bob = hub.auth.create_user("Bob", "another password")["guid"]
    hub.reassign(moniker, bob, user=rig.alice)
    [moved] = records.search(hub, type=config.TYPE)
    assert moved["owner"] == bob and moved["creator"] == rig.alice
    hub.reassign(moniker, rig.alice, user=rig.alice)

    # CF2 on change, CF3: a value set as secret:<name> reaches the worker as the secret's value.
    await rig.cap("secret.create", scope=f"worker:{moniker}", name="api-key", value=SECRET)
    answer = await rig.cap("config.set", worker=moniker, set={"greeting": "hello", "api_key": "secret:api-key",
                                                              "paths": ["/srv"], "retries": 3})
    written = {"greeting": "hello", "api_key": "secret:api-key", "paths": ["/srv"], "retries": 3}
    assert answer["sent"] is True and answer["config"]["values"] == written
    assert answer["config"]["record"] == made["moniker"] and answer["config"]["worker"] == moniker
    await eventually(lambda: rig.applied() == answer["config"]["version"] != empty)
    assert rig.worker.config == dict(written, api_key=SECRET)
    assert rig.worker.config_version == answer["config"]["version"]

    # The record keeps the reference (X2), and config.get reads it back with what was applied.
    [stored] = records.search(hub, type=config.TYPE)
    assert stored["guid"] == made["guid"] and stored["body"]["values"] == written
    got = (await rig.cap("config.get", worker=rig.guid))["config"]
    assert got == dict(answer["config"], applied=answer["config"]["version"])

    # Unset takes a value out; the worker gets the whole config again.
    answer = await rig.cap("config.set", worker=moniker, unset=["greeting", "not-set-anyway"])
    await eventually(lambda: rig.worker.config == {"api_key": SECRET, "paths": ["/srv"], "retries": 3})
    await eventually(lambda: rig.applied() == answer["config"]["version"])

    # The Data page shows the record like any other, with the reference and never the value (CF1, L4).
    async with browser() as client:
        assert (await login(client, rig.base, "Alice", "a password"))[0] == 200
        status, types = await ask(client, "GET", f"{rig.base}/api/record-types")
        assert status == 200 and {"type": "config", "plugin": "core", "count": 1} in types["types"]
        status, listed = await ask(client, "GET", f"{rig.base}/api/records?type=config")
        assert status == 200 and [r["moniker"] for r in listed["records"]] == [made["moniker"]]
        assert listed["records"][0]["body"]["values"]["api_key"] == "secret:api-key"
        status, one = await ask(client, "GET", f"{rig.base}/api/records/{made['moniker']}")
        assert status == 200 and one["record"]["body"]["values"]["api_key"] == "secret:api-key"
        status, entries = await ask(client, "GET", f"{rig.base}/api/journal?cap=config.set")
        assert status == 200 and len(entries["entries"]) == 2
        for reply in (types, listed, one, entries):
            assert SECRET not in json.dumps(reply)

    # The journal has the reference, and nowhere does the hub keep the value: not the journal,
    # the records, the heartbeats it stores, nor anything else in its database (X2, L4).
    sets = journal.read(hub, cap="config.set")
    assert "secret:api-key" in json.dumps(sets)
    with hub._lock:
        for table in ("journal", "records", "record_text", "heartbeats", "workers"):
            for row in hub._db.execute(f"SELECT * FROM {table}"):
                assert SECRET not in json.dumps([str(v) for v in tuple(row)])
    for name in ("hub.db", "hub.db-wal"):
        if (tmp_path / "hub" / name).exists():
            assert SECRET.encode() not in (tmp_path / "hub" / name).read_bytes()

    # Set while the worker is offline: kept, and sent when it links. A new worker process,
    # holding nothing, then a hub restart: the config is read back from the record (K5).
    await rig.stop_worker()
    await eventually(lambda: rig.hub._links.get(rig.guid) is None)
    answer = await rig.cap("config.set", worker=moniker, set={"greeting": "while away"})
    assert answer["sent"] is False and answer["not_sent"]["code"] == "worker_offline"
    assert answer["not_sent"]["next"]
    latest = answer["config"]
    await rig.stop_hub()

    await rig.start_hub()
    hub = rig.hub
    got = (await rig.cap("config.get", worker=moniker))["config"]
    assert got["values"] == dict(latest["values"]) and got["version"] == latest["version"]
    assert got["values"]["api_key"] == "secret:api-key" and got["values"]["greeting"] == "while away"
    rig.start_worker()
    assert rig.worker.config == {} and rig.worker.config_version is None
    await eventually(lambda: rig.worker.config_version == latest["version"] and rig.applied() == latest["version"])
    assert rig.worker.config == {"api_key": SECRET, "paths": ["/srv"], "retries": 3, "greeting": "while away"}

    # A reference to a secret that does not exist: the record keeps it, nothing is sent,
    # the worker keeps what it has, and the reply says why and what to do (C3).
    before = dict(rig.worker.config)
    answer = await rig.cap("config.set", worker=moniker, set={"db": "secret:nowhere"})
    assert answer["sent"] is False and answer["not_sent"]["code"] == "unknown_secret"
    assert "secret.create" in answer["not_sent"]["next"]
    assert answer["config"]["values"]["db"] == "secret:nowhere"
    await asyncio.sleep(0.3)
    assert rig.worker.config == before and rig.applied() == latest["version"]

    # When it links with that config, the hub journals why it sent nothing (kind config).
    await rig.stop_worker()
    rig.start_worker()
    found = await eventually(lambda: journal.read(hub, kind="config", worker=rig.guid))
    assert found[0]["result"]["not_sent"]["code"] == "unknown_secret"

    # Creating the secret, then setting again, sends the whole config.
    await rig.cap("secret.create", scope="hub", name="nowhere", value="db-password-in-the-hub-scope")
    answer = await rig.cap("config.set", worker=moniker, set={"greeting": "again"})
    assert answer["sent"] is True
    await eventually(lambda: rig.worker.config.get("db") == "db-password-in-the-hub-scope"
                     and rig.applied() == answer["config"]["version"])

    await rig.stop_worker()
    await rig.stop_hub()


def test_a_worker_approved_before_config_records_gets_one_at_start(tmp_path):
    hub = Hub(tmp_path / "hub")
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    from helpers import link_fields
    from nacl.signing import SigningKey
    signing = SigningKey.generate()
    key = signing.verify_key.encode().hex()
    hub.knock({"type": "knock", "protocol": 1, "name": "old", "key": key, "macs": [], "caps": [],
               **link_fields(signing)})
    guid = hub.approve(key, user=alice)["guid"]
    with hub._lock, hub._db:      # as a data directory from before this slice: no config record
        hub._db.execute("DELETE FROM records WHERE type = 'config'")
    hub.close()

    again = Hub(tmp_path / "hub")
    [made] = records.search(again, type=config.TYPE)
    assert made["body"] == {"worker": guid, "values": {}} and made["owner"] == made["creator"] == alice
    again.close()
    third = Hub(tmp_path / "hub")       # made once, not at every start
    assert len(records.search(third, type=config.TYPE)) == 1
    third.close()


def test_config_set_refuses_arguments_it_does_not_take(tmp_path):
    asyncio.run(refuses(tmp_path))


async def refuses(tmp_path):
    hub = Hub(tmp_path / "hub")
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    for args in ({"worker": "no-such-worker", "set": {}}, {"set": {"a": 1}},
                 {"worker": "x", "set": []}, {"worker": "x", "unset": "a"}):
        reply = await hub.call("hub", "config.set", args, WHY, user=alice)
        assert reply["type"] == "error" and reply["message"] and reply["next"], reply
    hub.close()
