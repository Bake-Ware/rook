"""The link exchange's protocol part (docs/link-design.md 2, 9, 11; W1, U8, R5, W7): the
signed texts, the derivation `session_psk`, HKDF-SHA256, and the `link` section of the
shared vectors. No hub and no worker: what both sides compute alike."""

import asyncio
import hashlib
import json
import pathlib

import aiohttp
import pytest
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from rook_hub import Hub
from rook_protocol import (ProtocolError, decode, encode, hkdf_sha256, session_psk, sign_link_accept,
                           sign_link_key, sign_link_open, verify_link_accept, verify_link_key, verify_link_open)

VECTORS = json.loads((pathlib.Path(__file__).parent.parent / "protocol" / "vectors.json").read_text("utf-8"))
LINK = VECTORS["link"]
V = LINK["valid"][0]


def secret(hex_):
    return PrivateKey(bytes.fromhex(hex_))


def flip(hex_, at=0):
    """The same hex with one byte changed."""
    raw = bytearray(bytes.fromhex(hex_))
    raw[at] ^= 0x01
    return raw.hex()


# ---------------------------------------------------------------- the vectors


def test_derivation_vector_from_either_side():
    worker = session_psk("worker", V["worker_key"], V["worker_ephemeral"], V["hub_ephemeral"],
                         secret(V["worker_ephemeral_secret"]), secret(V["worker_link_secret"]), V["hub_link_key"])
    hub = session_psk("hub", V["worker_key"], V["worker_ephemeral"], V["hub_ephemeral"],
                      secret(V["hub_ephemeral_secret"]), secret(V["hub_link_secret"]), V["worker_link_key"])
    assert worker == hub == V["psk"]
    assert hashlib.sha256(V["psk"].encode("ascii")).digest()[:16].hex() == V["band_id"]


def test_derivation_vector_step_by_step():
    # The intermediate values, so an implementation in another language can find where it differs.
    assert secret(V["worker_link_secret"]).public_key.encode().hex() == V["worker_link_key"]
    assert secret(V["hub_ephemeral_secret"]).public_key.encode().hex() == V["hub_ephemeral"]
    assert SigningKey(bytes.fromhex(V["worker_seed"])).verify_key.encode().hex() == V["worker_key"]
    assert SigningKey(bytes.fromhex(V["hub_seed"])).verify_key.encode().hex() == V["hub_key"]
    assert V["info"] == V["worker_key"] + V["worker_ephemeral"] + V["hub_ephemeral"]
    okm = hkdf_sha256(b"rook-link-v1", bytes.fromhex(V["ee"] + V["ss"]), bytes.fromhex(V["info"]), 32)
    assert okm.hex() == V["okm"] == V["psk"]


def test_signature_vectors():
    worker = SigningKey(bytes.fromhex(V["worker_seed"]))
    hub = SigningKey(bytes.fromhex(V["hub_seed"]))
    # Ed25519 is deterministic: the same key and text always give the same signature.
    assert sign_link_key(worker, V["worker_link_key"]) == V["link_signature"]
    assert sign_link_open(worker, V["hub_key"], V["worker_ephemeral"]) == V["link_open"]["signature"]
    assert sign_link_accept(hub, V["worker_key"], V["worker_ephemeral"], V["hub_ephemeral"],
                            V["hub_link_key"]) == V["link_accept"]["signature"]
    assert verify_link_open(V["hub_key"], V["link_open"])
    assert verify_link_accept(V["hub_key"], V["worker_key"], V["worker_ephemeral"], V["link_accept"])
    knock = {"key": V["worker_key"], "link_key": V["worker_link_key"], "link_signature": V["link_signature"]}
    assert verify_link_key(knock)


def test_vector_messages_are_the_derivation_vectors():
    # The link_open and link_accept message vectors are the very messages of the derivation vector.
    messages = {v["name"]: v["message"] for v in VECTORS["valid"]}
    assert messages["link_open"] == V["link_open"] and messages["link_accept"] == V["link_accept"]
    assert decode(encode(V["link_open"])) == V["link_open"]


@pytest.mark.parametrize("vector", LINK["refused"], ids=lambda v: v["name"])
def test_refused_vector(vector):
    check = vector["check"]
    if check in ("ee", "ss"):
        with pytest.raises(ProtocolError, match=f"^{check} is all zero"):
            session_psk(vector["side"], vector["key"], vector["worker_ephemeral"], vector["hub_ephemeral"],
                        secret(vector["ephemeral_secret"]), secret(vector["link_secret"]), vector["peer_link_key"])
    elif check == "link_open signature":
        assert not verify_link_open(vector["hub_key"], vector["link_open"])
    elif check == "link_accept signature":
        assert not verify_link_accept(vector["hub_key"], vector["key"], vector["worker_ephemeral"], vector["link_accept"])
    else:
        assert check == "link_signature"
        assert not verify_link_key(vector["knock"])


def test_every_refused_check_has_a_vector():
    assert {v["check"] for v in LINK["refused"]} == {"ee", "ss", "link_open signature", "link_accept signature",
                                                      "link_signature"}


# ---------------------------------------------------------------- session_psk


class Sides:
    """Fresh keys for one worker and its hub, and the PSK each side computes."""

    def __init__(self):
        self.key = SigningKey.generate().verify_key.encode().hex()
        self.worker_link, self.hub_link = PrivateKey.generate(), PrivateKey.generate()
        self.worker_ephemeral, self.hub_ephemeral = PrivateKey.generate(), PrivateKey.generate()

    def worker(self, **change):
        args = dict(key=self.key, worker_ephemeral=pub(self.worker_ephemeral), hub_ephemeral=pub(self.hub_ephemeral),
                    ephemeral_secret=self.worker_ephemeral, link_secret=self.worker_link,
                    peer_link_key=pub(self.hub_link))
        args.update(change)
        return session_psk("worker", **args)

    def hub(self, **change):
        args = dict(key=self.key, worker_ephemeral=pub(self.worker_ephemeral), hub_ephemeral=pub(self.hub_ephemeral),
                    ephemeral_secret=self.hub_ephemeral, link_secret=self.hub_link,
                    peer_link_key=pub(self.worker_link))
        args.update(change)
        return session_psk("hub", **args)


def pub(private):
    return private.public_key.encode().hex()


def test_both_sides_agree_and_the_psk_is_64_hex():
    sides = Sides()
    psk = sides.worker()
    assert psk == sides.hub()
    assert len(psk) == 64 and psk == psk.lower() and bytes.fromhex(psk)


def test_any_one_input_changed_changes_the_psk():
    sides = Sides()
    psk = sides.worker()
    other = PrivateKey.generate()
    # The worker's view of the hub's ephemeral, or of the hub's link key, is not the hub's.
    assert sides.worker(hub_ephemeral=pub(other)) != psk
    assert sides.worker(peer_link_key=pub(other)) != psk
    # The hub's view of the worker's ephemeral, or of the worker's link key, is not the worker's.
    assert sides.hub(worker_ephemeral=pub(other)) != psk
    assert sides.hub(peer_link_key=pub(other)) != psk
    # Another identity key: the same curve results, but the info differs.
    assert sides.worker(key=SigningKey.generate().verify_key.encode().hex()) != psk
    # Each side's own secrets too.
    assert sides.worker(ephemeral_secret=other) != psk
    assert sides.worker(link_secret=other) != psk


def test_two_exchanges_never_share_a_key():
    sides = Sides()
    first = sides.worker()
    sides.worker_ephemeral, sides.hub_ephemeral = PrivateKey.generate(), PrivateKey.generate()
    assert sides.worker() == sides.hub() != first


SMALL_ORDER = "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"   # a point of order 8 (RFC 7748)


@pytest.mark.parametrize("point", [SMALL_ORDER, "00" * 32, "01" + "00" * 31])
def test_small_order_points_are_refused(point):
    sides = Sides()
    with pytest.raises(ProtocolError, match="^ee is all zero"):
        sides.worker(hub_ephemeral=point)
    with pytest.raises(ProtocolError, match="^ss is all zero"):
        sides.hub(peer_link_key=point)


def test_a_refusal_names_the_check_and_no_secret():
    sides = Sides()
    with pytest.raises(ProtocolError) as refused:
        sides.worker(hub_ephemeral=SMALL_ORDER)
    text = str(refused.value)
    for secret_ in (sides.worker_ephemeral, sides.worker_link):
        assert secret_.encode().hex() not in text
    assert SMALL_ORDER not in text


def test_side_must_be_named():
    sides = Sides()
    with pytest.raises(ValueError):
        session_psk("relay", sides.key, pub(sides.worker_ephemeral), pub(sides.hub_ephemeral),
                    sides.worker_ephemeral, sides.worker_link, pub(sides.hub_link))


# ---------------------------------------------------------------- the signed texts


def test_link_key_signature_fails_when_anything_changes():
    identity = SigningKey.generate()
    knock = {"key": identity.verify_key.encode().hex(), "link_key": pub(PrivateKey.generate())}
    knock["link_signature"] = sign_link_key(identity, knock["link_key"])
    assert verify_link_key(knock)
    assert not verify_link_key({**knock, "link_key": flip(knock["link_key"])})
    assert not verify_link_key({**knock, "key": SigningKey.generate().verify_key.encode().hex()})
    assert not verify_link_key({**knock, "link_signature": flip(knock["link_signature"], 5)})
    assert not verify_link_key({**knock, "link_signature": flip(knock["link_signature"], 63)})


def test_link_open_signature_fails_when_anything_changes():
    identity, hub_key = SigningKey.generate(), SigningKey.generate().verify_key.encode().hex()
    message = {"type": "link_open", "protocol": 1, "key": identity.verify_key.encode().hex(),
               "ephemeral": pub(PrivateKey.generate())}
    message["signature"] = sign_link_open(identity, hub_key, message["ephemeral"])
    assert verify_link_open(hub_key, message)
    assert not verify_link_open(flip(hub_key), message)                                   # one byte of the hub key
    assert not verify_link_open(SigningKey.generate().verify_key.encode().hex(), message)  # signed for another hub
    assert not verify_link_open(hub_key, {**message, "ephemeral": flip(message["ephemeral"], 31)})
    assert not verify_link_open(hub_key, {**message, "key": SigningKey.generate().verify_key.encode().hex()})
    assert not verify_link_open(hub_key, {**message, "signature": flip(message["signature"], 40)})


def test_link_accept_signature_fails_when_anything_changes():
    hub = SigningKey.generate()
    hub_key = hub.verify_key.encode().hex()
    key, worker_ephemeral = SigningKey.generate().verify_key.encode().hex(), pub(PrivateKey.generate())
    message = {"type": "link_accept", "protocol": 1, "ephemeral": pub(PrivateKey.generate()),
               "hub_link_key": pub(PrivateKey.generate())}
    message["signature"] = sign_link_accept(hub, key, worker_ephemeral, message["ephemeral"], message["hub_link_key"])
    assert verify_link_accept(hub_key, key, worker_ephemeral, message)
    assert not verify_link_accept(hub_key, key, flip(worker_ephemeral), message)        # an answer to another request
    assert not verify_link_accept(hub_key, flip(key, 10), worker_ephemeral, message)    # to another worker
    assert not verify_link_accept(hub_key, key, worker_ephemeral, {**message, "ephemeral": flip(message["ephemeral"])})
    assert not verify_link_accept(hub_key, key, worker_ephemeral,
                                  {**message, "hub_link_key": flip(message["hub_link_key"], 20)})
    assert not verify_link_accept(SigningKey.generate().verify_key.encode().hex(), key, worker_ephemeral, message)
    assert not verify_link_accept(hub_key, key, worker_ephemeral, {**message, "signature": flip(message["signature"])})


def test_the_signed_texts_cannot_stand_in_for_each_other():
    # Each text starts with its own label, so a signature over one never verifies as another.
    identity = SigningKey.generate()
    key = identity.verify_key.encode().hex()
    link_key = pub(PrivateKey.generate())
    signature = sign_link_key(identity, link_key)
    assert not verify_link_open(link_key, {"key": key, "ephemeral": link_key, "signature": signature})
    old = identity.sign(b"rook-worker-key:" + bytes.fromhex(link_key)).signature.hex()   # the approval's text
    assert not verify_link_key({"key": key, "link_key": link_key, "link_signature": old})


# ---------------------------------------------------------------- HKDF (RFC 5869, appendix A)


RFC5869 = [   # (IKM, salt, info, L, OKM), test cases 1 to 3: SHA-256
    ("0b" * 22, "000102030405060708090a0b0c", "f0f1f2f3f4f5f6f7f8f9", 42,
     "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"),
    ("".join(f"{i:02x}" for i in range(0x00, 0x50)), "".join(f"{i:02x}" for i in range(0x60, 0xb0)),
     "".join(f"{i:02x}" for i in range(0xb0, 0x100)), 82,
     "b11e398dc80327a1c8e7f78c596a49344f012eda2d4efad8a050cc4c19afa97c59045a99cac7827271cb41c65e590e09"
     "da3275600c2f09b8367793a9aca3db71cc30c58179ec3e87c14c01d5c1f3434f1d87"),
    ("0b" * 22, "", "", 42,
     "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"),
]


@pytest.mark.parametrize("ikm, salt, info, length, okm", RFC5869, ids=["case 1", "case 2", "case 3"])
def test_hkdf_sha256_rfc5869(ikm, salt, info, length, okm):
    assert hkdf_sha256(bytes.fromhex(salt), bytes.fromhex(ikm), bytes.fromhex(info), length).hex() == okm


# ---------------------------------------------------------------- the knock through real HTTP


def test_the_hub_refuses_a_knock_without_its_link_fields(tmp_path):
    # Until L2 the hub checks the two fields' shape only; a knock without them is not a knock.
    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        identity = SigningKey.generate()
        key = identity.verify_key.encode().hex()
        knock = {"type": "knock", "protocol": 1, "name": "box", "key": key, "macs": [], "caps": []}
        try:
            async with aiohttp.ClientSession() as client:
                for body in (json.dumps(knock).encode(),
                             json.dumps({**knock, "link_key": pub(PrivateKey.generate())}).encode()):
                    async with client.post(f"http://{host}:{port}/knock", data=body) as response:
                        reply = await response.json()
                    assert response.status == 400 and reply["code"] == "bad_message", reply
                link_key = pub(PrivateKey.generate())
                good = encode({**knock, "link_key": link_key, "link_signature": sign_link_key(identity, link_key)})
                async with client.post(f"http://{host}:{port}/knock", data=good) as response:
                    assert response.status == 200 and (await response.json())["status"] == "pending"
            assert [p["key"] for p in hub.list_pending()] == [key]
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())
