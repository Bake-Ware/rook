"""Secrets, journal scrubbing and the band-change event (X1 to X6, L4, L5, J2)."""

import asyncio
import json
import os

import pytest
from nacl.signing import SigningKey

from helpers import Clock, eventually, link_fields
from memory_link import link_pair
from rook_hub import Hub, bands, caps
from rook_hub.errors import HubError
from rook_protocol import decode, encode
from test_door import mcp_client, tool

HOME_GW, CAFE_GW, ROAM_GW = "02:aa:00:00:00:01", "02:bb:00:00:00:02", "02:cc:00:00:00:03"
V_HUB, V_BAND, V_X = "hub-value-0001-aaaa", "band-value-0002-bbbb", "x-value-0003-cccc"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n-----END OPENSSH PRIVATE KEY-----"
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PLAIN = "plain-value-not-a-secret-4242"


class Box:
    """An approved worker driven by hand: it heartbeats a given gateway and answers calls.
    `demo.echo` returns its arguments; `demo.leak` returns text holding secret-shaped values."""

    CAPS = [{"name": n, "description": d, "schema": {"type": "object"}, "returns": "result"}
            for n, d in (("demo.echo", "Echo."), ("demo.leak", "Leak."))]

    def __init__(self, hub, name, owner, gateway):
        self.name, self.gateway, self.seen = name, gateway, []
        signing_key = SigningKey.generate()
        self.key = signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        record = hub.approve(self.key, user=owner)
        self.guid, self.moniker = record["guid"], record["moniker"]

    async def connect(self, hub):
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(hub.serve_link(hub_end))
        self.answering = asyncio.create_task(self._answer())
        await self.heartbeat(self.gateway)

    async def heartbeat(self, gateway):
        self.gateway, self._beat = gateway, asyncio.get_running_loop().create_future()
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python",
                      "version": "0.1.0", "macs": []},
            "caps": self.CAPS, "band": {"gateway_mac": gateway}, "health": {}}))
        await self._beat

    async def _answer(self):
        while True:
            try:
                message = decode(await self.link.recv())
            except EOFError:
                return
            if message["type"] == "heartbeat_reply":
                self._beat.set_result(True)
            elif message["type"] == "call":
                self.seen.append(message["args"])
                result = message["args"] if message["cap"] == "demo.echo" else \
                    {"log": f"key:\n{PRIVATE_KEY}\ntoken {GITHUB_TOKEN} done"}
                await self.link.send(encode({"type": "result", "id": message["id"], "result": result}))

    async def stop(self):
        self.link.close()
        await asyncio.gather(self.serving, self.answering)


def journal_text(hub):
    return "\n".join(json.dumps(dict(row)) for row in hub._db.execute("SELECT * FROM journal"))


def entries(hub, **where):
    rows = [dict(r) for r in hub._db.execute("SELECT * FROM journal ORDER BY id")]
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def test_secrets(tmp_path, monkeypatch):
    received = []
    monkeypatch.setitem(caps.CAPS, "test.capture", (lambda hub, args, actor: received.append(args) or {"ok": True},
                                                     {"description": "Record the arguments.", "schema": {}}))
    asyncio.run(scenario(tmp_path, received))


async def scenario(tmp_path, received):
    state = tmp_path / "hub"
    key = os.urandom(32)

    # 1. Started with no vault key: secret.create says the vault is locked; the rest works.
    hub = Hub(state, clock=Clock())
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    try:
        async with mcp_client(f"http://{host}:{port}/mcp", agent) as door:
            failed, text = await tool(door, "call", worker="hub", cap="secret.create", why="try the vault",
                                      args={"scope": "hub", "name": "api-key", "value": V_HUB})
            assert failed and text.startswith("vault_locked:") and "Next: Start the hub with its vault key" in text
            failed, listing = await tool(door, "workers")
            assert not failed
            failed, minted = await tool(door, "call", worker="hub", cap="worker.join-token", why="still works")
            assert not failed and minted["token"]
    finally:
        await hub.stop_serving()
        hub.close()

    # 2. With a key: three api-key secrets in three scopes; listed without values; journal clean.
    hub = Hub(state, clock=Clock(), vault_key=key)
    host, port = await hub.serve("127.0.0.1", 0)
    url = f"http://{host}:{port}/mcp"
    home = bands.create(hub, "Home", HOME_GW, user=alice)
    x, y, z = (Box(hub, n, alice, gw) for n, gw in (("box-x", HOME_GW), ("box-y", HOME_GW), ("box-z", ROAM_GW)))
    for box in (x, y, z):
        await box.connect(hub)
    try:
        async with mcp_client(url, agent) as door:
            async def call(worker, cap, args, why="a call"):
                return await tool(door, "call", worker=worker, cap=cap, args=args, why=why)

            for scope, value in (("hub", V_HUB), (f"band:{home['moniker']}", V_BAND), (f"worker:{x.moniker}", V_X)):
                failed, made = await call("hub", "secret.create", {"scope": scope, "name": "api-key", "value": value},
                                          f"keep the api key for {scope}")
                assert not failed and made["created"]["reference"] == "secret:api-key"
                assert value not in json.dumps(made)
            failed, listed = await call("hub", "secret.list", {})
            assert sorted(s["scope"] for s in listed["secrets"]) == sorted(
                ["hub", f"band:{home['moniker']}", f"worker:{x.moniker}"])
            assert not any(v in json.dumps(listed) for v in (V_HUB, V_BAND, V_X))
            assert not any(v in journal_text(hub) for v in (V_HUB, V_BAND, V_X))
            failed, again = await call("hub", "secret.create", {"scope": "hub", "name": "api-key", "value": "other-value"})
            assert failed and text_has(again, "secret_exists")

            # 3. Resolution walks worker, band, hub (X1), at any depth (X2).
            nested = {"outer": {"list": ["plain", "secret:api-key", {"inner": "secret:api-key"}]}}
            for box, value in ((x, V_X), (y, V_BAND), (z, V_HUB)):
                failed, reply = await call(box.moniker, "demo.echo", nested)
                assert not failed and box.seen[-1] == {"outer": {"list": ["plain", value, {"inner": value}]}}
            failed, reply = await call("hub", "test.capture", nested)
            assert received[-1] == {"outer": {"list": ["plain", V_HUB, {"inner": V_HUB}]}}

            # 4. A name found nowhere is refused, naming the scopes searched; the cap never ran.
            calls_before = len(x.seen)
            failed, text = await call(x.moniker, "demo.echo", {"k": "secret:nothing"})
            assert failed and text.startswith("unknown_secret:")
            assert f"worker:{x.moniker}, band:{home['moniker']}, hub" in text and len(x.seen) == calls_before

            # 5. The reply to the agent holds the value; the journal entry holds the marker.
            failed, reply = await call(x.moniker, "demo.echo", {"k": "secret:api-key"}, "echo the key back")
            assert reply == {"k": V_X}
            [entry] = [e for e in entries(hub, kind="call") if e["why"] == "echo the key back"]
            assert "(secret:api-key)" in entry["result"] and V_X not in entry["result"]

            # 6. Known secret shapes are scrubbed from the journal too.
            failed, reply = await call(x.moniker, "demo.leak", {}, "read the log")
            assert PRIVATE_KEY in reply["log"] and GITHUB_TOKEN in reply["log"]
            [entry] = [e for e in entries(hub, kind="call") if e["why"] == "read the log"]
            assert "(secret shape:private-key)" in entry["result"] and "(secret shape:github-token)" in entry["result"]
            assert PRIVATE_KEY not in journal_text(hub) and GITHUB_TOKEN not in journal_text(hub)

            # 7. A job's call carries the reference; the worker gets the value; neither the
            #    stored job nor the journal holds it.
            failed, made = await call("hub", "job.create", {
                "title": "deploy", "match": {"event": "deploy"},
                "call": {"worker": x.moniker, "cap": "demo.echo", "args": {"token": "secret:api-key"}}})
            assert not failed
            await call("hub", "event.emit", {"name": "deploy"})
            await eventually(lambda: x.seen[-1] == {"token": V_X})
            stored = hub._db.execute("SELECT args FROM jobs WHERE guid = ?", (made["job"]["guid"],)).fetchone()[0]
            assert json.loads(stored) == {"token": "secret:api-key"}
            [run] = await eventually(lambda: entries(hub, kind="job"))
            assert "(secret:api-key)" in run["result"] and V_X not in journal_text(hub)

            # 8. Only a person views a value; the reveal is journaled without it (X3).
            secret = next(s for s in listed["secrets"] if s["scope"] == f"worker:{x.moniker}")
            assert hub.vault.reveal(secret["moniker"], user=alice) == V_X
            [revealed] = [e for e in entries(hub, kind="auth") if "revealed" in e["result"]]
            assert revealed["user"] == alice and json.loads(revealed["result"])["revealed"] == secret["guid"]
            assert V_X not in journal_text(hub)
            with pytest.raises(HubError) as e:
                hub.vault.reveal(secret["moniker"], token=agent)
            assert e.value.message["code"] == "reveal_needs_a_person" and e.value.message["next"]

            # 9. A value in neither the vault nor the shapes, in an old entry: redact takes it out (L5).
            await call(x.moniker, "demo.echo", {"note": PLAIN}, "an old call")
            assert PLAIN in journal_text(hub)
            failed, redacted = await call("hub", "journal.redact", {"value": PLAIN}, "take the value out")
            assert not failed and redacted["redacted"] == 1
            assert PLAIN not in journal_text(hub)
            [own] = [e for e in entries(hub, kind="call") if e["why"] == "take the value out"]
            assert PLAIN not in json.dumps(own)

            # 11. Band change: Home -> roaming -> a newly named band; renaming is no change (J2).
            failed, made = await call("hub", "job.create", {
                "title": "moved", "match": {"event": "band.change", "payload": {"worker_moniker": y.moniker}},
                "call": {"worker": "hub", "cap": "event.emit", "args": {"name": "y.moved"}}})
            await y.heartbeat(CAFE_GW)
            await eventually(lambda: band_changes(hub))
            assert band_changes(hub)[-1]["from"]["guid"] == home["guid"] and band_changes(hub)[-1]["to"] is None
            cafe = bands.create(hub, "Cafe", CAFE_GW, user=alice)
            await eventually(lambda: len(band_changes(hub)) == 2)
            assert band_changes(hub)[-1]["from"] is None and band_changes(hub)[-1]["to"] == {
                "guid": cafe["guid"], "moniker": cafe["moniker"], "name": "Cafe"}
            bands.rename(hub, cafe["moniker"], "Corner Cafe", user=alice)
            await y.heartbeat(CAFE_GW)
            await eventually(lambda: len(entries(hub, kind="job", job=made["job"]["guid"])) == 2)
            await asyncio.sleep(0.1)
            assert len(band_changes(hub)) == 2 and all(c["worker_moniker"] == y.moniker for c in band_changes(hub))
    finally:
        for box in (x, y, z):
            await box.stop()
        await hub.stop_serving()
        hub.close()

    # The key was never written into the hub's data directory (X4).
    for path in state.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert key not in data and key.hex().encode() not in data

    # 10. Reopened with the same key, references still resolve and nothing changed band;
    #     with a wrong key the vault says so and hands out nothing.
    hub = Hub(state, clock=Clock(), vault_key=key)
    await hub.serve("127.0.0.1", 0)
    try:
        for box in (x, y):
            await box.connect(hub)
        reply = await hub.call(x.moniker, "demo.echo", {"k": "secret:api-key"}, "after a restart", user=alice)
        assert reply["result"] == {"k": V_X}
        await asyncio.sleep(0.1)
        assert band_changes(hub)[2:] == []                  # restarting emitted no band change
    finally:
        for box in (x, y):
            await box.stop()
        await hub.stop_serving()
        hub.close()
    hub = Hub(state, clock=Clock(), vault_key=os.urandom(32))
    try:
        assert hub.vault.state == "wrong_key"
        for attempt in (lambda: hub.vault.list({}, {"user": alice, "token": None}),
                        lambda: hub.vault.reveal(secret["moniker"], user=alice),
                        lambda: hub.vault.substitute({"k": "secret:api-key"}, "hub")):
            with pytest.raises(HubError) as e:
                attempt()
            assert e.value.message["code"] == "vault_key_wrong" and e.value.message["next"]
    finally:
        hub.close()


def band_changes(hub):
    return [json.loads(e["result"])["payload"] for e in entries(hub, kind="event")
            if json.loads(e["result"])["event"] == "band.change"]


def text_has(text, code):
    return isinstance(text, str) and text.startswith(code + ":")


# ------------------------------------------------------------------- edges

def test_scrubber_finds_values_inside_text_and_not_short_ones(tmp_path):
    hub = Hub(tmp_path / "hub", vault_key=os.urandom(32))
    try:
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        hub.vault.create({"scope": "hub", "name": "long", "value": "correct-horse-battery"}, {"user": alice, "token": None})
        hub.vault.create({"scope": "workshop", "name": "short", "value": "abc"}, {"user": alice, "token": None})
        with hub._lock, hub._db:
            hub._journal("call", (alice, None), None, {"out": "x correct-horse-battery y abc"},
                         why="say correct-horse-battery")
        [row] = entries(hub)
        assert row["why"] == "say (secret:long)"
        assert json.loads(row["result"]) == {"out": "x (secret:long) y abc"}   # shorter than 8: not scrubbed
        listed = hub.vault.list({}, {"user": alice, "token": None})["secrets"]
        assert {s["scope"] for s in listed} == {"hub", "workshop"}            # a plain-string scope
    finally:
        hub.close()


def test_scrubber_finds_values_at_every_position(tmp_path):
    # The scan looks at every third position only; a value must be found wherever it starts,
    # next to another, inside a longer one, and at exactly eight characters.
    hub = Hub(tmp_path / "hub", vault_key=os.urandom(32))
    try:
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        values = {"eight": "8chars!!", "long": "a-much-longer-secret-value", "inner": "longer-secret"}
        for name, value in values.items():
            hub.vault.create({"scope": "hub", "name": name, "value": value}, {"user": alice, "token": None})
        from rook_hub.scrub import scrub_text
        for pad in range(7):
            text = "x" * pad + values["eight"] + values["long"] + " " + values["inner"] + "y" * pad
            assert scrub_text(text, hub.vault) == \
                "x" * pad + "(secret:eight)(secret:long) (secret:inner)" + "y" * pad
    finally:
        hub.close()
