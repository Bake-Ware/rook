"""The hub's side of the link exchange (docs/link-design.md 2, 4.3, 9, 10 row L2; R5, W7, X4, F3, U8):
`POST /link` through real HTTP, the session table, the knock's link key, `hub_link.key` with
backups, and the size limit over memory links. The test plays the worker with the protocol
library's own functions. Nothing carries a session yet (L4), so the test marks a session used
itself, as the carrier will."""

import asyncio
import os
import sqlite3
import stat

import aiohttp
import pytest
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from helpers import eventually
from memory_link import link_pair
from rook_hub import Hub, backup
from rook_hub.errors import HubError
from rook_protocol import (MAX_MESSAGE, decode, encode, session_psk, sign_link_key, sign_link_open,
                           verify_link_accept)
from test_hub import heartbeat

SMALL_ORDER = "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"


class Keys:
    """Where the session table puts keys, recorded: what L4's Endpoint will be given."""

    def __init__(self):
        self.added, self.removed = [], []

    def add_key(self, psk):
        self.added.append(psk)

    def remove_key(self, psk):
        self.removed.append(psk)

    def held(self):
        return [psk for psk in self.added if psk not in self.removed]


class Box:
    """A worker played by hand: its identity key, link secret and what the hub told it at approval."""

    def __init__(self):
        self.identity, self.link = SigningKey.generate(), PrivateKey.generate()
        self.key = self.identity.verify_key.encode().hex()
        self.hub_key = None

    def knock(self, **change):
        link_key = self.link.public_key.encode().hex()
        return dict({"type": "knock", "protocol": 1, "name": "box", "key": self.key, "macs": [], "caps": [],
                     "link_key": link_key, "link_signature": sign_link_key(self.identity, link_key)}, **change)

    def link_open(self, hub_key=None, ephemeral=None):
        """(the worker's ephemeral secret, the link_open message)."""
        secret = ephemeral or PrivateKey.generate()
        public = secret.public_key.encode().hex()
        return secret, {"type": "link_open", "protocol": 1, "key": self.key, "ephemeral": public,
                        "signature": sign_link_open(self.identity, hub_key or self.hub_key, public)}

    def psk(self, secret, link_open, link_accept):
        return session_psk("worker", self.key, link_open["ephemeral"], link_accept["ephemeral"], secret,
                           self.link, link_accept["hub_link_key"])


async def post(client, url, body):
    async with client.post(url, data=body if isinstance(body, bytes) else encode(body)) as response:
        return response.status, decode(await response.read())


def test_link_exchange_through_real_http(tmp_path):
    asyncio.run(exchange(tmp_path))


async def exchange(tmp_path):
    hub = Hub(tmp_path / "hub")
    keys = hub.sessions.keys = Keys()
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    box = Box()
    try:
        async with aiohttp.ClientSession() as client:
            # A knock whose link signature does not verify is refused, and nothing is kept.
            other = SigningKey.generate()
            forged = box.knock(link_signature=sign_link_key(other, box.knock()["link_key"]))
            status, reply = await post(client, f"{base}/knock", forged)
            assert status == 403 and reply["code"] == "bad_signature" and reply["next"]
            assert hub.list_pending() == []

            # A key never seen, and a key still pending, are refused with what to do.
            secret, opening = box.link_open(hub_key=hub.public_key)
            status, reply = await post(client, f"{base}/link", opening)
            assert status == 403 and reply["code"] == "unknown_key" and "never approved" in reply["message"]
            status, reply = await post(client, f"{base}/knock", box.knock())
            assert status == 200 and reply["status"] == "pending"
            status, reply = await post(client, f"{base}/link", opening)
            assert status == 403 and reply["code"] == "unknown_key" and "waiting to be approved" in reply["message"]
            assert reply["next"] and keys.added == []

            # Approval carries the knock's link key to the key (link design 2.3).
            hub.approve(box.key, user=alice)
            with hub._lock:
                stored = hub._db.execute("SELECT link_key FROM keys WHERE key = ?", (box.key,)).fetchone()[0]
            assert stored == box.link.public_key.encode().hex()
            status, approval = await post(client, f"{base}/knock", box.knock())
            box.hub_key = approval["hub_key"]                   # what the worker stores (R5, U9)

            # Bad signatures: a changed byte, and a link_open signed for another hub. Nothing is kept.
            _, opening = box.link_open()
            tampered = dict(opening, signature=opening["signature"][:-2] + ("00" if opening["signature"][-2:] != "00" else "01"))
            status, reply = await post(client, f"{base}/link", tampered)
            assert status == 403 and reply["code"] == "bad_signature" and reply["next"]
            _, elsewhere = box.link_open(hub_key=SigningKey.generate().verify_key.encode().hex())
            status, reply = await post(client, f"{base}/link", elsewhere)
            assert status == 403 and reply["code"] == "bad_signature"
            # Not a link_open at all, and a small-order ephemeral: bad_message, nothing kept.
            status, reply = await post(client, f"{base}/link", box.knock())
            assert status == 400 and reply["code"] == "bad_message"
            status, reply = await post(client, f"{base}/link", b"not json")
            assert status == 400 and reply["code"] == "bad_message"
            small = {"type": "link_open", "protocol": 1, "key": box.key, "ephemeral": SMALL_ORDER,
                     "signature": sign_link_open(box.identity, box.hub_key, SMALL_ORDER)}
            status, reply = await post(client, f"{base}/link", small)
            assert status == 400 and reply["code"] == "bad_message" and "ee is all zero" in reply["message"]
            assert keys.added == [] and hub.sessions.pending(box.key) == []

            # A good link_open: a link_accept that verifies under the stored hub key, and the same PSK on both sides.
            first_secret, first = box.link_open()
            status, accept = await post(client, f"{base}/link", first)
            assert status == 200 and accept["type"] == "link_accept"
            assert verify_link_accept(box.hub_key, box.key, first["ephemeral"], accept)
            assert accept["hub_link_key"] == hub.link_key
            first_psk = box.psk(first_secret, first, accept)
            assert keys.added == [first_psk] and hub.sessions.pending(box.key) == [first["ephemeral"]]

            # A second link_open adds a pending session beside the first; the first used clears the others.
            second_secret, second = box.link_open()
            status, accept2 = await post(client, f"{base}/link", second)
            second_psk = box.psk(second_secret, second, accept2)
            assert second_psk != first_psk and sorted(keys.held()) == sorted([first_psk, second_psk])
            assert hub.sessions.pending(box.key) == sorted([first["ephemeral"], second["ephemeral"]])
            assert hub.sessions.used(first_psk) == box.key           # what L4 calls when Channel 0 opens
            assert hub.sessions.live(box.key) == first_psk and hub.sessions.pending(box.key) == []
            assert keys.held() == [first_psk] and second_psk in keys.removed
            assert hub.sessions.used(second_psk) is None             # gone: never usable now

            # A replayed link_open leaves the live session in place; the replay's own key is useless to it.
            status, replayed = await post(client, f"{base}/link", first)
            assert status == 200 and replayed["ephemeral"] != accept["ephemeral"]   # a fresh hub ephemeral
            assert hub.sessions.live(box.key) == first_psk and hub.sessions.pending(box.key) == [first["ephemeral"]]
            assert box.psk(first_secret, first, replayed) not in (first_psk,)       # nothing old comes back

            # A recording replayed in a loop holds one pending entry, and never removes a fresh one (D10).
            fresh_secret, fresh = box.link_open()
            status, fresh_accept = await post(client, f"{base}/link", fresh)
            fresh_psk = box.psk(fresh_secret, fresh, fresh_accept)
            for _ in range(10):
                status, _ = await post(client, f"{base}/link", second)
                assert status == 200
            assert hub.sessions.pending(box.key) == sorted([first["ephemeral"], second["ephemeral"], fresh["ephemeral"]])
            assert fresh_psk in keys.held() and len(keys.held()) == 4      # live, and three pending: one per ephemeral
            assert hub.sessions.used(fresh_psk) == box.key
            assert keys.held() == [fresh_psk] and hub.sessions.pending(box.key) == []

            # Revoke: attaching a new key to the worker removes every session of the old one.
            _, pending_again = box.link_open()
            await post(client, f"{base}/link", pending_again)
            rekeyed = Box()
            await post(client, f"{base}/knock", rekeyed.knock())
            hub.attach(rekeyed.key, approval["moniker"], user=alice)
            assert keys.held() == [] and hub.sessions.live(box.key) is None and hub.sessions.pending(box.key) == []
            status, reply = await post(client, f"{base}/link", box.link_open()[1])
            assert status == 403 and reply["code"] == "key_revoked" and reply["next"]
            rekeyed.hub_key = box.hub_key
            status, reply = await post(client, f"{base}/link", rekeyed.link_open()[1])
            assert status == 200 and reply["type"] == "link_accept"
    finally:
        await hub.stop_serving()
        hub.close()

    # A restarted hub holds no session, keeps its link key, and the file is the owner's only.
    again = Hub(tmp_path / "hub")
    try:
        assert again.link_key == hub.link_key and again.sessions.live(rekeyed.key) is None
        assert stat.S_IMODE(os.stat(tmp_path / "hub" / "hub_link.key").st_mode) == 0o600
        assert not [p for p in (tmp_path / "hub").iterdir() if p.name not in (
            "hub.db", "hub.key", "hub_link.key", "token.secret", "hub.lock") and not p.name.startswith("hub.db-")]
    finally:
        again.close()


def test_backup_and_restore_keep_hub_link_key(tmp_path):
    async def go():
        data, key_file = tmp_path / "hub", tmp_path / "vault.key"
        hub = Hub(data, vault_key_file=key_file)
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        box = Box()
        hub.knock(box.knock())
        hub.approve(box.key, user=alice)
        box.hub_key = hub.public_key
        link_key = hub.link_key
        backup.backup(data, key_file, tmp_path / "hub.backup")
        hub.close()
        backup.restore(tmp_path / "hub.backup", tmp_path / "restored", key_file)
        assert (tmp_path / "restored" / "hub_link.key").read_bytes() == (data / "hub_link.key").read_bytes()
        restored = Hub(tmp_path / "restored", vault_key_file=key_file)
        host, port = await restored.serve("127.0.0.1", 0)
        try:
            assert restored.link_key == link_key
            secret, opening = box.link_open()
            async with aiohttp.ClientSession() as client:
                status, accept = await post(client, f"http://{host}:{port}/link", opening)
            assert status == 200 and accept["hub_link_key"] == link_key
            assert verify_link_accept(box.hub_key, box.key, opening["ephemeral"], accept)
        finally:
            await restored.stop_serving()
            restored.close()
    asyncio.run(go())


def test_a_data_directory_from_before_the_link_keys_gains_the_columns(tmp_path):
    # The keys and knocks tables as core made them before this change: no link_key column.
    data = tmp_path / "hub"
    hub = Hub(data)
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    old, waiting = Box(), Box()
    hub.knock(old.knock())
    moniker = hub.approve(old.key, user=alice)["moniker"]
    hub.knock(waiting.knock(name="waiting"))
    hub.close()
    db = sqlite3.connect(data / "hub.db")
    db.execute("ALTER TABLE keys DROP COLUMN link_key")
    db.execute("ALTER TABLE knocks DROP COLUMN link_key")
    db.commit()
    db.close()

    async def go():
        hub = Hub(data)
        host, port = await hub.serve("127.0.0.1", 0)
        try:
            for table in ("keys", "knocks"):
                assert "link_key" in {row["name"] for row in hub._db.execute(f"PRAGMA table_info({table})")}
            assert hub.get_worker(moniker)["owner"] == alice                   # nothing lost
            assert [p["name"] for p in hub.list_pending()] == ["waiting"]
            # The key approved before link keys has none: refused, with how to get one (a rekey, W7).
            old.hub_key = hub.public_key
            async with aiohttp.ClientSession() as client:
                status, reply = await post(client, f"http://{host}:{port}/link", old.link_open()[1])
            assert status == 403 and reply["code"] == "unknown_key" and "before workers had link keys" in reply["message"]
            assert moniker in reply["next"]
            # The pending knock gets its link key when it knocks again, and approval carries it.
            hub.knock(waiting.knock(name="waiting"))
            hub.approve(waiting.key, user=alice)
            with hub._lock:
                assert hub._db.execute("SELECT link_key FROM keys WHERE key = ?", (waiting.key,)).fetchone()[0] \
                    == waiting.link.public_key.encode().hex()
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


# ---------------------------------------------------------------- sizes (D4), over memory links


ECHO = {"name": "demo.echo", "description": "Echo.", "schema": {"type": "object"}, "returns": "result"}
TERM = {"name": "demo.term", "description": "A stream.", "schema": {"type": "object"}, "returns": "stream"}


def call_size(call_id, text):
    return len(encode({"type": "call", "id": call_id, "cap": "demo.echo", "args": {"text": text}}))


def test_max_message_limits_calls_and_input(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        box = Box()
        hub.knock(box.knock())
        guid = hub.approve(box.key, user=alice)["guid"]
        hub_end, worker = link_pair(box.key)
        asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
        limit = call_size("at-limit", "x" * 1000)

        async def beat(**extra):
            await worker.send(encode(heartbeat(box.key, caps=(ECHO, TERM), **extra)))
            assert decode(await worker.recv())["type"] == "heartbeat_reply"

        async def answer():
            request = decode(await worker.recv())
            await worker.send(encode({"type": "result", "id": request["id"], "result": len(encode(request))}))

        try:
            await beat(max_message=limit)
            assert hub.largest_message(guid) == limit
            # Exactly the limit passes; one byte more is refused before anything is sent.
            answering = asyncio.create_task(answer())
            reply = await hub.call(guid, "demo.echo", {"text": "x" * 1000}, "fits", user=alice, call_id="at-limit")
            await answering
            assert reply == {"type": "result", "id": "at-limit", "result": limit}
            reply = await hub.call(guid, "demo.echo", {"text": "x" * 1001}, "too big", user=alice, call_id="at-limit")
            assert reply["code"] == "message_too_large" and reply["id"] == "at-limit" and reply["next"]
            assert str(limit) in reply["message"]
            assert worker._inbox.empty()                                 # nothing reached the worker

            # Input to a stream: the same limit.
            opening = asyncio.create_task(hub.call(guid, "demo.term", {}, "open", user=alice))
            request = decode(await worker.recv())
            await worker.send(encode({"type": "stream_open", "id": request["id"], "stream": "s1", "title": "t",
                                      "description": "d"}))
            stream = (await opening)["stream"]
            fits = len(encode({"type": "stream_data", "stream": "s1", "data": "eHh4" * 100}))   # 300 bytes of x
            await beat(max_message=fits)
            assert await hub.streams.write(stream, "x" * 300, user=alice) == {"written": 300}
            assert decode(await worker.recv())["type"] == "stream_data"
            with pytest.raises(HubError) as refused:
                await hub.streams.write(stream, "x" * 301, user=alice)
            assert refused.value.message["code"] == "message_too_large" and refused.value.message["next"]
            assert worker._inbox.empty()

            # Without the field, or above 16,000,000, the limit is 16,000,000: exactly that passes.
            await beat()
            assert hub.largest_message(guid) == MAX_MESSAGE
            await beat(max_message=MAX_MESSAGE + 5)
            assert hub.largest_message(guid) == MAX_MESSAGE
            big = "x" * (1000 + MAX_MESSAGE - limit)
            assert call_size("at-limit", big) == MAX_MESSAGE
            answering = asyncio.create_task(answer())
            reply = await hub.call(guid, "demo.echo", {"text": big}, "the largest", user=alice, call_id="at-limit")
            await answering
            assert reply["result"] == MAX_MESSAGE
            reply = await hub.call(guid, "demo.echo", {"text": big + "x"}, "too big", user=alice, call_id="at-limit")
            assert reply["code"] == "message_too_large"
        finally:
            worker.close()
            await eventually(lambda: not hub._links)
            hub.close()
    asyncio.run(go())
