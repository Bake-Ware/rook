"""Users, tokens and unattended joins (T1, T2, T4, T5, T6, R3, W6, L1)."""

import asyncio
import base64
import json

import pytest
from nacl.signing import SigningKey

from helpers import Clock, eventually
from memory_link import connector
from rook_hub import Hub, HubError
from rook_worker import Cap, Refused, Worker


async def echo(args):
    return args


CAPS = {"demo.echo": Cap("Return the arguments unchanged.", {"type": "object"}, echo)}


def journal(hub):
    return [dict(row) for row in hub._db.execute("SELECT * FROM journal ORDER BY id")]


def claims(jwt):
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def refusals(hub):
    """The auth entries for refused tokens: (user, token, reason)."""
    return [(e["user"], e["token"], json.loads(e["result"])["refused"]) for e in journal(hub)
            if e["kind"] == "auth" and "refused" in json.loads(e["result"])]


def refused(hub, jwt, scope=None):
    with pytest.raises(HubError) as e:
        hub.auth.verify(jwt, scope)
    assert e.value.message["next"]
    return e.value.message["code"]


def test_users_tokens_and_unattended_joins(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock)
    host, port = hub.serve_knocks("127.0.0.1", 0)
    connect = connector(hub)

    def worker(state, token=None, name="box"):
        return Worker(tmp_path / state, f"http://{host}:{port}", connect, name=name,
                      caps=CAPS, macs=[], knock_every=0.02, token=token)

    try:
        # 1. A user mints an agent token. It verifies and lists with its scope,
        #    owner and no last-used time (T1, T2, T5, T6).
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        agent = hub.auth.mint("agent", user=alice)
        record = hub.auth.verify(agent)
        assert (record["purpose"], record["scopes"], record["owner"], record["last_used"]) == (
            "agent", ["client"], alice, None)
        assert claims(agent)["purpose"] == "agent" and claims(agent)["scope"] == "client"
        assert "exp" not in claims(agent)                  # agent tokens: long-lived by default
        [listed] = hub.auth.list_tokens()
        assert (listed["scopes"], listed["owner"], listed["owner_moniker"], listed["last_used"]) == (
            ["client"], alice, hub.auth.get_user(alice)["moniker"], None)

        # 2. The agent token mints a join token. Its owner chains to the same
        #    user; its lifetime is the hub's default, not the caller's (T2, T5, R3).
        minted_at = clock.now
        join = hub.auth.mint("join", token=agent)
        record = hub.auth.verify(join)
        assert record["owner"] == alice
        assert record["minted_by"] == hub.auth.verify(agent)["guid"]
        lifetime = hub._db.execute("SELECT lifetime FROM token_defaults WHERE purpose = 'join'").fetchone()[0]
        assert lifetime == 600 and record["expires_at"] == minted_at + lifetime
        assert claims(join)["exp"] == int(minted_at + lifetime)
        assert hub.auth.verify(agent)["last_used"] == minted_at   # minting through it is a use

        # 3. A worker knocks with the join token and is approved at once; its
        #    owner is that user; nothing is pending (R3, W6). The journal holds
        #    the mint and the join as auth entries naming token and user (L1).
        first = worker("w1", token=join)
        running = asyncio.create_task(first.run())
        await eventually(lambda: first.identity)
        assert hub.list_pending() == []
        [w] = hub.list_workers()
        assert w["owner"] == alice and first.identity["guid"] == w["guid"]
        agent_id, join_id = hub.auth.verify(agent)["guid"], hub.auth.list_tokens()[1]["guid"]
        auth = [(e["user"], e["token"], e["worker"], json.loads(e["result"])) for e in journal(hub)
                if e["kind"] == "auth"]
        assert (alice, agent_id, None, {"minted": join_id, "purpose": "join"}) in auth
        assert (alice, join_id, w["guid"], {"approved": first.key}) in auth
        await eventually(lambda: hub.get_worker(w["guid"])["online"])

        # 4. A second worker knocks with the same join token: refused, with a
        #    next step, and not approved (R3, C3).
        second = worker("w2", token=join)
        with pytest.raises(Refused) as e:
            await second.run()
        assert e.value.message["code"] == "token_spent" and e.value.message["next"]
        assert second.identity is None and hub.list_pending() == [] and len(hub.list_workers()) == 1
        assert refusals(hub) == [(alice, join_id, "token_spent")]    # journaled under its owner (L1)

        # 5. A join token past its lifetime is refused the same way (T2).
        late = hub.auth.mint("join", user=alice)
        clock.advance(600)
        third = worker("w3", token=late)
        with pytest.raises(Refused) as e:
            await third.run()
        assert e.value.message["code"] == "token_expired" and e.value.message["next"]
        assert hub.list_pending() == [] and len(hub.list_workers()) == 1
        assert refusals(hub)[-1] == (alice, hub.auth.list_tokens()[-1]["guid"], "token_expired")

        # 6. A call made with the agent token is journaled with token and user,
        #    and the token's last-used time moves (L1, T6).
        first.changed()                                    # heartbeat at the new time
        await eventually(lambda: hub.get_worker(w["guid"])["online"])
        before = hub.auth.verify(agent)["last_used"]
        reply = await hub.call(w["guid"], "demo.echo", {"x": 1}, "echo through the agent token", token=agent)
        assert reply["result"] == {"x": 1}
        call = journal(hub)[-1]
        assert (call["kind"], call["user"], call["token"], call["cap"]) == ("call", alice, agent_id, "demo.echo")
        assert hub.auth.verify(agent)["last_used"] == clock.now > before

        # 7. A revoked token stops verifying. Revoking a user revokes every token
        #    that chains back to them, and no one else's (T5).
        alice_spare = hub.auth.mint("agent", user=alice)
        hub.auth.revoke_token(agent_id, user=alice)
        assert refused(hub, agent) == "token_revoked"
        with pytest.raises(HubError) as e:                 # acting with it is refused and journaled
            hub.auth.mint("join", token=agent)
        assert e.value.message["code"] == "token_revoked"
        assert refusals(hub)[-1] == (alice, agent_id, "token_revoked")
        bob = hub.auth.create_user("Bob", "a password")["guid"]
        bob_agent = hub.auth.mint("agent", user=bob)
        bob_join = hub.auth.mint("join", token=bob_agent)
        hub.auth.revoke_user(bob, token=alice_spare)
        assert refused(hub, bob_agent) == "token_revoked" and refused(hub, bob_join) == "token_revoked"
        bob_join_id = hub.auth.list_tokens()[-1]["guid"]
        revoked_user_entry = journal(hub)[-1]
        assert hub.knock(knock(bob_join))["code"] == "token_revoked"   # the knock path journals too
        assert refusals(hub)[-1] == (bob, bob_join_id, "token_revoked")
        assert hub.auth.verify(alice_spare)["owner"] == alice
        assert [t["revoked_at"] is None for t in hub.auth.list_tokens() if t["owner"] == alice] == [
            False, True, True, True]                       # only the agent token revoked by hand
        entry = revoked_user_entry
        assert json.loads(entry["result"]) == {"revoked_user": bob, "revoked_tokens": [
            hub.auth.list_tokens()[-2]["guid"], hub.auth.list_tokens()[-1]["guid"]]}

        # 8. A worker with no token still knocks, sits pending and is approved by hand (R2).
        plain = worker("w4", name="plain")
        running_plain = asyncio.create_task(plain.run())
        pending = await eventually(hub.list_pending)
        assert pending[0]["key"] == plain.key and plain.identity is None
        hub.approve(plain.key, user=alice)
        await eventually(lambda: plain.identity)
        assert hub.get_worker(plain.identity["guid"])["owner"] == alice

        for w_ in (first, plain):
            w_.stop()
        await asyncio.gather(running, running_plain)
    finally:
        hub.close()


# ------------------------------------------------------------------- edges

@pytest.fixture
def hub(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock())
    yield hub
    hub.close()


def knock(token):
    key = SigningKey.generate().verify_key.encode().hex()
    return {"type": "knock", "protocol": 1, "name": "box", "key": key, "macs": [], "caps": [], "token": token}


def test_scopes_keep_tokens_to_their_mechanism(hub):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    join = hub.auth.mint("join", user=alice)
    with pytest.raises(HubError) as e:
        hub.auth.mint("join", token=join)          # a join token cannot act as an agent
    assert e.value.message["code"] == "token_scope"
    reply = asyncio.run(hub.call("anything", "demo.echo", {}, "join token as caller", token=join))
    assert reply["code"] == "token_scope" and reply["next"]
    reply = hub.knock(knock(agent))                # an agent token cannot join
    assert reply["code"] == "token_scope"
    assert hub.list_pending() == [] and hub.list_workers() == []
    agent_id, join_id = (t["guid"] for t in hub.auth.list_tokens())
    assert refusals(hub) == [(alice, join_id, "token_scope"), (alice, join_id, "token_scope"),
                             (alice, agent_id, "token_scope")]
    assert hub.knock(knock(join))["status"] == "approved"


def test_forged_or_foreign_tokens_are_invalid(hub, tmp_path):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    header, payload, signature = agent.split(".")
    tampered = base64.urlsafe_b64encode(json.dumps(dict(claims(agent), sub="someone-else")).encode())
    assert refused(hub, f"{header}.{tampered.decode().rstrip('=')}.{signature}") == "token_invalid"
    assert refused(hub, "not a token") == "token_invalid"
    other = Hub(tmp_path / "other")
    foreign = other.auth.mint("agent", user=other.auth.create_user("Eve", "a password")["guid"])
    other.close()
    assert refused(hub, foreign) == "token_invalid"
    assert hub.knock(knock("not a token"))["code"] == "token_invalid"
    assert hub.knock(knock(foreign))["code"] == "token_invalid"
    with pytest.raises(HubError):
        hub.auth.mint("agent", token=foreign)
    assert hub.list_pending() == []
    assert refusals(hub) == []                     # unrecognised tokens are not journaled


def test_checking_a_token_is_not_using_it(hub):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    hub.auth.verify(agent)
    hub.auth.verify(agent, "client")
    assert hub.auth.verify(agent)["last_used"] is None


def test_only_purposes_with_defaults_are_minted(hub):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    with pytest.raises(HubError) as e:
        hub.auth.mint("dongle", user=alice)
    assert e.value.message["code"] == "unknown_purpose" and "agent, join" in e.value.message["next"]


def test_a_revoked_user_cannot_act(hub):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    bob = hub.auth.create_user("Bob", "a password")
    hub.auth.revoke_user(bob["moniker"], user=alice)
    with pytest.raises(HubError) as e:
        hub.auth.mint("agent", user=bob["guid"])
    assert e.value.message["code"] == "user_revoked"
    assert hub.auth.get_user(bob["guid"])["revoked_at"] is not None


def test_acting_needs_exactly_one_of_user_or_token(hub):
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    agent = hub.auth.mint("agent", user=alice)
    for kwargs in ({}, {"user": alice, "token": agent}):
        with pytest.raises(HubError) as e:
            hub.auth.mint("agent", **kwargs)
        assert e.value.message["code"] == "no_actor"


def test_users_and_tokens_have_monikers_unique_hub_wide(hub):
    # G1: every hub object has a GUID and a moniker, unique across all of them.
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["moniker"])
    token = hub.auth.verify(agent)
    assert len(alice["moniker"].split("-")) == 3 and len(token["moniker"].split("-")) == 3
    hub.auth.revoke_token(token["moniker"], user=alice["guid"])   # either ID is accepted
    assert refused(hub, agent) == "token_revoked"
    rows = hub._db.execute("SELECT moniker, guid FROM monikers").fetchall()
    assert {r[1] for r in rows} == {alice["guid"], token["guid"]}
