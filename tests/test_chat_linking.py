"""Access v2 item 5: Telegram/Discord accounts linked per person by a
one-time code, the hub's bot by default and a person's own bot as an
override, shared spaces granted like machines (rook/hub/access.py,
rook/hub/integrations.py, the users.* caps, notify.send user=).

No real network and no real people: the fake platform servers from
test_integrations stand in for Telegram and Discord.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from rook.hub import access as A
from rook.hub.authz import build_authorizer, current_principal, principal_for_token, \
    principal_for_user
from rook.hub.integrations import Inbound
from rook.hub.node import HubNode
from rook.hub.plugins.discord import Discord
from rook.hub.plugins.notify import Notify
from rook.hub.plugins.telegram import Telegram
from tests.test_integrations import CHAT, CHANNEL, DC_TOKEN, TG_TOKEN, FakeClient, FakeDiscord, \
    FakeTelegram, WORKERS, chat, wait_for, wire  # noqa: F401 (chat: a fixture)

OWN_TOKEN = "987654321:BBOwnTelegramBotTokenForTestsOnly_zyxwvut"
GROUP = "-100555"
DM_BAKE = "4001"     # Telegram: a private chat's id is the user's id
DM_KID = "4002"


def member(uid="u_kid"):
    return principal_for_user({"id": uid, "name": uid}, False)


def owner(uid="u_bake"):
    return principal_for_user({"id": uid, "name": uid}, True)


async def as_(principal, coro_fn):
    tok = current_principal.set(principal)
    try:
        return await coro_fn()
    finally:
        current_principal.reset(tok)


class Vault:
    def __init__(self, values):
        self.values = values
        self.reads = []

    def get(self, name, actor, via="get", task=None):
        self.reads.append((name, actor, via))
        return self.values[name]


class AskClient(FakeClient):
    """Records calls; the house agent answers home.ask."""

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None,
                   principal=None, _decision=None):
        await super().call(cap, args, target, timeout, identity, principal, _decision)
        self.calls[-1]["role"] = getattr(principal, "role", None)
        if cap == "home.ask":
            return {"ok": True, "result": {"answer": f"house: {args['question']}", "path": "answer"}}
        return {"ok": True, "result": {"ran": cap}}


def people_node(tmp_path, vault=None):
    """A hub node whose authorizer has an access store: Bake (owner) and the
    kid (member, no grants)."""
    az = build_authorizer(str(tmp_path), signer=False)
    az.signer = None
    az.access.set_role("u_bake", "owner", actor="t", words="seed", label="Bake")
    az.access.set_role("u_kid", "member", actor="t", words="seed", label="Kid")
    node = SimpleNamespace(worker_id="hub-id", _state_dir=str(tmp_path), _vault=vault,
                           entry=lambda: {"name": "rook", "roles": ["is_hub"]},
                           client=AskClient(az, dict(WORKERS)))
    node.plugins = {}

    class _Host:
        @property
        def plugins(self):
            return list(node.plugins.values())
    node.host = _Host()
    return node, az.access


def push(tg, text, user_id, chat, kind, username="someone", reply_from=None):
    tg.update_id += 1
    msg = {"message_id": 1000 + tg.update_id, "chat": {"id": int(chat), "type": kind},
           "from": {"id": int(user_id), "is_bot": False, "username": username}, "text": text}
    if reply_from is not None:
        msg["reply_to_message"] = {"message_id": 1, "from": {"id": reply_from}}
    tg.updates.append({"update_id": tg.update_id, "message": msg})


def dm(text, user=DM_BAKE, **kw):
    return Inbound(chat_id=user, user_id=user, username="bake", text=text, private=True,
                   message_id="7", **kw)


# -- the store -------------------------------------------------------------------

def test_link_codes_are_one_time_short_lived_and_hashed(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    c = s.link_code("u_bake", "telegram", actor="human:u_bake")
    assert len(c["code"]) == 9 and c["code"][4] == "-"
    with sqlite3.connect(tmp_path / "access.db") as db:
        stored = [r[0] for r in db.execute("SELECT code_hash FROM link_codes")]
    assert stored and c["code"] not in stored[0]                       # only its hash

    assert s.redeem("discord", c["code"], "1") is None                  # wrong platform
    assert s.redeem("telegram", c["code"].lower().replace("-", " "), "4001", "bake") == "u_bake"
    assert s.redeem("telegram", c["code"], "4001") is None              # used up
    assert s.linked_user("telegram", "4001") == "u_bake"
    assert s.linked_user("telegram", "9999") is None

    old = s.link_code("u_bake", "telegram", actor="x")["code"]
    new = s.link_code("u_bake", "telegram", actor="x")["code"]
    assert s.redeem("telegram", old, "4001") is None                    # replaced by the new one
    with sqlite3.connect(tmp_path / "access.db") as db:
        db.execute("UPDATE link_codes SET expires=?", (time.time() - 1,))
    assert s.redeem("telegram", new, "4001") is None                    # expired

    with pytest.raises(A.AccessError, match="platform"):
        s.link_code("u_bake", "irc", actor="x")


def test_one_account_per_person_per_platform_and_own_bot_codes(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    s.redeem("telegram", s.link_code("u_bake", "telegram", actor="x")["code"], "4001")
    s.redeem("telegram", s.link_code("u_bake", "telegram", actor="x")["code"], "4003")
    assert [l["platform_user"] for l in s.links("u_bake")] == ["4003"]    # relinked
    # the same Telegram account linked by someone else moves to them
    s.redeem("telegram", s.link_code("u_kid", "telegram", actor="x")["code"], "4003")
    assert s.links("u_bake") == [] and s.linked_user("telegram", "4003") == "u_kid"
    assert s.history("u_kid")[0]["doc"]["was"] == "u_bake"
    # on someone's own bot only their codes work, and a refused code survives
    code = s.link_code("u_kid", "discord", actor="x")["code"]
    assert s.redeem("discord", code, "77", bot_owner="u_bake") is None
    assert s.redeem("discord", code, "77", bot_owner="u_kid") == "u_kid"
    assert s.unlink("u_kid", "discord", actor="x", words="kid: forget my discord") is True
    assert s.unlink("u_kid", "discord", actor="x", words="again") is False
    assert [h["action"] for h in s.history("u_kid")][:2] == ["unlink", "link"]


def test_own_bot_takes_a_vault_name_never_a_token(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    with pytest.raises(A.AccessError, match="never the token"):
        s.set_bot("u_kid", "telegram", OWN_TOKEN, actor="x", words="w")
    with pytest.raises(A.AccessError, match="user.u_kid."):
        s.set_bot("u_kid", "telegram", "plugin.telegram.token", actor="x", words="w")
    got = s.set_bot("u_kid", "telegram", "{{secret:user.u_kid.tg}}", actor="x", words="w")
    assert got["secret"] == "user.u_kid.tg" and s.bots("telegram")[0]["user_id"] == "u_kid"
    assert s.set_bot("u_bake", "telegram", "bake-bot", actor="x", words="w", owner=True)["bot"] \
        == "own"                                                         # owners name any
    assert s.set_bot("u_kid", "telegram", "", actor="x", words="back to the hub bot")["bot"] \
        == "hub"
    assert [b["user_id"] for b in s.bots("telegram")] == ["u_bake"]


def test_spaces_are_hub_resources_granted_like_machines(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    s.set_role("u_bake", "owner", actor="t", words="seed")
    with pytest.raises(A.AccessError, match="users.space"):
        s.grant("u_kid", "space", "discord:123", actor="o", words="w")   # not a space yet
    with pytest.raises(A.AccessError, match="server id"):
        s.add_space("discord", "general", "x", actor="o", words="w")
    s.add_space("discord", "123", "Household", actor="o", words="Bake: our family server")
    g = s.grant("u_kid", "space", "Discord:123", actor="o", words="Bake: the kid can use it")
    assert g["ref"] == "discord:123"
    assert s.may_use_space("u_kid", "discord", "123") and s.may_use_space("u_bake", "discord", "9")
    assert not s.may_use_space("u_kid", "discord", "9")
    # a space grant never reaches a machine or the hub
    from rook.hub.policy import Target
    assert s.evaluate("u_kid", "file.read", "read", Target(id="w", name="w"))[0] is None
    assert s.evaluate("u_kid", "hub.info", "read", Target(id="h", is_rook=True))[0] is None
    out = s.remove_space("discord", "123", actor="o", words="Bake: we left that server")
    assert out["revoked"] == [g["id"]] and not s.may_use_space("u_kid", "discord", "123")


def test_an_older_grants_table_is_rebuilt_with_the_new_kind(tmp_path):
    db = sqlite3.connect(tmp_path / "access.db")
    db.executescript("""
        CREATE TABLE grants (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('band','worker','hub','knowledge','any')),
            ref TEXT NOT NULL, caps TEXT NOT NULL DEFAULT '["*"]',
            tier TEXT NOT NULL DEFAULT 'exec', created REAL NOT NULL, actor TEXT NOT NULL,
            words TEXT NOT NULL DEFAULT '', revoked REAL, revoked_by TEXT, revoke_words TEXT);
        INSERT INTO grants(id,user_id,kind,ref,created,actor,words)
            VALUES('g_old','u_kid','worker','kids-pc',1,'o','his desktop');
    """)
    db.commit()
    db.close()
    s = A.AccessStore.for_dir(str(tmp_path))
    assert [g["id"] for g in s.grants("u_kid")] == ["g_old"]
    s.add_space("telegram", GROUP, "Family", actor="o", words="w")
    assert s.grant("u_kid", "space", f"telegram:{GROUP}", actor="o", words="w")["kind"] == "space"
    A.AccessStore.for_dir(str(tmp_path))                               # reopening is a no-op
    assert len(s.grants("u_kid")) == 2


# -- the integration with people ---------------------------------------------------

@pytest.mark.asyncio
async def test_strangers_are_ignored_and_told_once_how_to_link(tmp_path):
    node, store = people_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        p.bind_host(node)
        await p.connect()
        try:
            hub_chat = Inbound(chat_id=CHAT, user_id="42", username="alice", text="hello")
            assert await p.handle_inbound(hub_chat) == "ignored:unknown"
            assert tg.sent == []                                          # silent in groups
            assert await p.handle_inbound(dm("hi", user="42")) == "ignored:unknown"
            assert await p.handle_inbound(dm("hi again", user="42")) == "ignored:unknown"
            assert len(tg.sent) == 1 and "/link CODE" in tg.sent[0]["text"]
            assert await p.handle_inbound(dm("/call worker-a shell.exec", user="42")) \
                == "ignored:unknown"
            assert node.client.calls == [] and p.status()["unknown"] == 4
        finally:
            await p.disconnect()


@pytest.mark.asyncio
async def test_link_then_direct_messages_reach_the_house_agent_as_that_person(tmp_path):
    node, store = people_node(tmp_path)
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        p.LONG_POLL_SECS = 1
        p.bind_host(node)
        await p.start()
        try:
            await wait_for(lambda: p.state["connected"])
            code = store.link_code("u_kid", "telegram", actor="human:u_kid")["code"]
            push(tg, f"/start {code}", DM_KID, DM_KID, "private", "kiddo")   # t.me deep link
            await wait_for(lambda: tg.sent)
            assert tg.sent[0]["text"] == "Linked: messages from this account now act as Kid."
            assert tg.sent[0]["chat_id"] == DM_KID
            assert store.linked_user("telegram", DM_KID) == "u_kid"

            push(tg, "what's for dinner?", DM_KID, DM_KID, "private", "kiddo")
            await wait_for(lambda: len(tg.sent) == 2)
            assert tg.sent[1]["text"] == "house: what's for dinner?"
            call = node.client.calls[-1]
            assert call["cap"] == "home.ask" and call["principal"] == "human:u_kid"
            assert call["role"] == "member" and call["identity"] == "human:u_kid/telegram"
            assert call["args"] == {"question": "what's for dinner?", "source": "telegram"}
        finally:
            await p.stop()


@pytest.mark.asyncio
async def test_link_guessing_is_throttled(tmp_path):
    node, store = people_node(tmp_path)
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rate_in=100)
    p.bind_host(node)
    sent = []

    async def send_text(chat, text, ids=None, reply_to="", bot=None):
        sent.append(text)
        return "1"
    p.send_text = send_text
    for _ in range(5):
        assert await p.handle_inbound(dm("/link AAAA-AAAA", user="66")) == "ignored:bad-code"
    code = store.link_code("u_kid", "telegram", actor="x")["code"]
    assert await p.handle_inbound(dm(f"/link {code}", user="66")) == "dropped:link-rate"
    assert store.linked_user("telegram", "66") is None
    assert await p.handle_inbound(dm(f"/link {code}", user="67")) == "linked"   # per account


@pytest.mark.asyncio
async def test_commands_and_bridged_messages_act_as_the_linked_person(tmp_path, chat):
    node, store = people_node(tmp_path)
    room = chat.start("ops", "agent:claude", [])["room"]
    store.redeem("telegram", store.link_code("u_kid", "telegram", actor="x")["code"], "42", "kid")
    store.redeem("telegram", store.link_code("u_bake", "telegram", actor="x")["code"], "43", "b")
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, rooms=[room], api_base=tg.base,
                 commands=["help", "call"], allowed_caps=["hub.echo", "shell.exec"])
        p.bind_host(node)
        p.use_chat_store(chat)
        p._watermarks = {room: chat.last_seq(room)}
        await p.connect()
        try:
            async def say(text, user):
                before = len(tg.sent)
                res = await p.handle_inbound(Inbound(chat_id=CHAT, user_id=user, username="u",
                                                     text=text, message_id="5"))
                return res, (tg.sent[before]["text"] if len(tg.sent) > before else None)

            assert (await say("/help", "42"))[1].startswith("Rook commands (run as Kid):")
            res, reply = await say('/call worker-c hub.echo {"x": 1}', "42")
            assert reply.startswith("denied: Kid (telegram) may not call hub.echo")
            assert node.client.calls == []                       # the kid has no grants
            store.grant("u_kid", "worker", "worker-c", caps=["hub.*"], actor="o",
                        words="Bake: the kid may echo on worker-c")
            res, reply = await say('/call worker-c hub.echo {"x": 1}', "42")
            assert json.loads(reply)["ran"] == "hub.echo"
            assert node.client.calls[-1]["principal"] == "human:u_kid"
            res, reply = await say('/call worker-a shell.exec {"cmd": "id"}', "43")
            assert node.client.calls[-1]["principal"] == "human:u_bake"     # the owner

            assert (await say("deploy looks good", "43"))[0] == "bridged"
            last = chat.read(room, None)["messages"][-1]
            assert last["sender"] == "user:bake" and last["text"] == "deploy looks good"
            before = len(tg.sent)
            assert await p.bridge_once() == 0                    # never relayed back
            assert len(tg.sent) == before
            chat.send(room, "agent:claude", "thanks", [], False)
            assert await p.bridge_once() == 1
        finally:
            await p.disconnect()


@pytest.mark.asyncio
async def test_hub_chat_without_a_bridge_goes_to_the_house_agent(tmp_path):
    node, store = people_node(tmp_path)
    store.redeem("telegram", store.link_code("u_bake", "telegram", actor="x")["code"], "43")
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT)
    p.bind_host(node)
    sent = []

    async def send_text(chat, text, ids=None, reply_to="", bot=None):
        sent.append((chat, text, reply_to))
        return "1"
    p.send_text = send_text
    assert await p.handle_inbound(Inbound(chat_id=CHAT, user_id="43", username="b",
                                          text="is kaiju up?", message_id="9")) == "asked"
    await wait_for(lambda: sent)
    assert sent == [(CHAT, "house: is kaiju up?", "9")]


@pytest.mark.asyncio
async def test_shared_spaces_hear_only_granted_people_addressing_the_bot(tmp_path):
    node, store = people_node(tmp_path)
    store.add_space("telegram", GROUP, "Family", actor="o", words="Bake: the family group")
    store.redeem("telegram", store.link_code("u_kid", "telegram", actor="x")["code"], "42")
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT)
    p.bind_host(node)
    p.bot_name = "rook_test_bot"

    async def send_text(*a, **kw):
        return "1"
    p.send_text = send_text
    msg = lambda text, **kw: Inbound(chat_id=GROUP, user_id="42", username="kid",  # noqa: E731
                                     text=text, space=GROUP, **kw)
    assert await p.handle_inbound(msg("@rook_test_bot hi", to_bot=True)) == "ignored:space"
    store.grant("u_kid", "space", f"telegram:{GROUP}", actor="o", words="Bake: kid can use it")
    assert await p.handle_inbound(msg("just chatting")) == "ignored:chatter"
    assert await p.handle_inbound(msg("@rook_test_bot hi", to_bot=True)) == "asked"
    other = Inbound(chat_id="-100666", user_id="42", username="kid", text="hi", space="-100666",
                    to_bot=True)
    assert await p.handle_inbound(other) == "ignored:chat"      # not a shared space


def test_telegram_parses_private_chats_groups_and_replies_to_the_bot():
    t = Telegram()
    m = t.parse({"message_id": 1, "chat": {"id": 5, "type": "private"},
                 "from": {"id": 5, "username": "a"}, "text": "x"})
    assert m.private and m.space == ""
    msg = {"message_id": 2, "chat": {"id": -100555, "type": "supergroup"},
           "from": {"id": 5}, "text": "@Rook_Test_Bot status",
           "reply_to_message": {"message_id": 1, "from": {"id": 999}}}
    m = t.parse(msg)
    assert not m.private and m.space == "-100555"
    from rook.hub.integrations import Bot
    assert Telegram._to_bot(msg, Bot(bot_id="999", bot_name="rook_test_bot"))
    assert Telegram._to_bot({**msg, "reply_to_message": None, "text": "hi"},
                            Bot(bot_id="999")) is False


class TwoBots(FakeTelegram):
    """One fake Bot API serving the hub's bot and a person's own bot, each
    with its own updates and sent messages."""

    def __init__(self):
        super().__init__()
        self.own = SimpleNamespace(updates=[], sent=[], update_id=0)

    async def handle(self, request):
        if request.match_info["token"] != OWN_TOKEN:
            return await super().handle(request)
        method = request.match_info["method"]
        body = await request.json() if request.can_read_body else {}
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"id": 555, "username": "kids_bot"}})
        if method == "getUpdates":
            for _ in range(10):
                if self.own.updates:
                    break
                await asyncio.sleep(0.02)
            out, self.own.updates = self.own.updates, []
            return web.json_response({"ok": True, "result": out})
        self.own.sent.append(body)
        return web.json_response({"ok": True, "result": {"message_id": len(self.own.sent)}})


@pytest.mark.asyncio
async def test_a_persons_own_bot_hears_only_them_and_carries_their_messages(tmp_path):
    vault = Vault({"user.u_kid.tg": OWN_TOKEN})
    node, store = people_node(tmp_path, vault)
    store.redeem("telegram", store.link_code("u_kid", "telegram", actor="x")["code"], DM_KID)
    store.redeem("telegram", store.link_code("u_bake", "telegram", actor="x")["code"], DM_BAKE)
    async with TwoBots() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        p.LONG_POLL_SECS = 1
        p.bind_host(node)
        node.plugins["telegram"] = p
        await p.start()
        try:
            await wait_for(lambda: p.state["connected"])
            store.set_bot("u_kid", "telegram", "user.u_kid.tg", actor="x", words="kid: my bot")
            p.reload_bots()
            await wait_for(lambda: p._user_bots.get("u_kid") and p._user_bots["u_kid"].connected)
            assert vault.reads[0][:2] == ("user.u_kid.tg", "integration:telegram")
            assert p.bot_label("u_kid") == "@kids_bot" and p.bot_label("u_bake") == "@rook_test_bot"
            assert p.status()["own_bots"] == 1

            push(tg.own, "hi from bake", DM_BAKE, DM_BAKE, "private")      # not the bot's owner
            push(tg.own, "hi from kid", DM_KID, DM_KID, "private")
            await wait_for(lambda: len(tg.own.sent) == 2)
            texts = sorted(s["text"] for s in tg.own.sent)
            assert texts[0] == "I only answer people linked to this Rook hub. Ask it for a " \
                               "link code, then send /link CODE here."
            assert texts[1] == "house: hi from kid"
            assert [c["principal"] for c in node.client.calls] == ["human:u_kid"]
            assert tg.sent == []                                           # the hub bot said nothing

            n = Notify()
            n.bind_host(node)
            res = await as_(member(), lambda: n.send("your build finished", user="u_kid"))
            assert res == {"ok": True, "sent": ["telegram"]}
            assert tg.own.sent[-1]["chat_id"] == DM_KID                    # from the own bot
            assert tg.own.sent[-1]["text"] == "your build finished"
            res = await as_(member(), lambda: n.send("psst", user="u_bake"))
            assert res["ok"] is False and "only u_bake or an owner" in res["error"]
            res = await as_(owner(), lambda: n.send("hi bake", user="u_bake"))
            assert res["ok"] and tg.sent[-1]["chat_id"] == DM_BAKE         # the hub bot
            assert OWN_TOKEN not in p.mask(f"x {OWN_TOKEN}")
            assert OWN_TOKEN not in json.dumps(p.status())

            store.set_bot("u_kid", "telegram", "", actor="x", words="kid: use the hub bot")
            p.reload_bots()
            await wait_for(lambda: "u_kid" not in p._user_bots)
        finally:
            await p.stop()


@pytest.mark.asyncio
async def test_sending_to_a_shared_space_needs_a_grant(tmp_path):
    node, store = people_node(tmp_path)
    store.add_space("telegram", GROUP, "Family", actor="o", words="w")
    async with FakeTelegram() as tg:
        p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, api_base=tg.base)
        p.bind_host(node)
        await p.connect()
        try:
            res = await as_(member(), lambda: p.send("hi family", chat=GROUP))
            assert res["ok"] is False and "no grant" in res["error"]
            assert (await as_(member(), lambda: p.send("x", chat="-100999")))["ok"] is False
            # An API key is its person (item 7): the kid's key reaches what the kid does.
            store.set_token_user("a1", "u_kid", actor="o", words="w")
            key = principal_for_token(SimpleNamespace(verified=True, kind="agent", agent_id="a1",
                                                      key_id="k", label="x", identity="agent:x"),
                                      store)
            assert (await as_(key, lambda: p.send("x", chat=GROUP)))["ok"] is False
            store.grant("u_kid", "space", f"telegram:{GROUP}", actor="o", words="w")
            assert (await as_(member(), lambda: p.send("hi family", chat=GROUP)))["ok"]
            assert (await as_(owner(), lambda: p.send("dinner", chat=GROUP)))["ok"]
            assert (await as_(key, lambda: p.send("from my key", chat=GROUP)))["ok"]
            assert [s["chat_id"] for s in tg.sent] == [GROUP, GROUP, GROUP]
        finally:
            await p.disconnect()


# -- Discord ----------------------------------------------------------------------

class FakeDiscordDM(FakeDiscord):
    def __init__(self, token=DC_TOKEN):
        super().__init__(token)
        self.server.app.router.add_post("/users/@me/channels", self.open_dm)
        self.server.app.router.add_get("/channels/{cid}", self.channel)

    async def open_dm(self, request):
        body = await request.json()
        return web.json_response({"id": "dm-" + body["recipient_id"]})

    async def channel(self, request):
        cid = request.match_info["cid"]
        return web.json_response({"id": cid, "guild_id": "777" if cid == "888" else None})


@pytest.mark.asyncio
async def test_discord_dms_links_and_servers(tmp_path):
    node, store = people_node(tmp_path)
    store.add_space("discord", "777", "Household", actor="o", words="Bake: our server")
    async with FakeDiscordDM() as dc:
        p = wire(Discord(), tmp_path, DC_TOKEN, chat_id=CHANNEL, api_base=dc.base)
        p.bind_host(node)
        node.plugins["discord"] = p
        await p.start()
        try:
            await wait_for(lambda: p.state["connected"] and p.bot_id == "999")
            m = p.parse({"id": "1", "channel_id": "dm1", "content": "x",
                         "author": {"id": "42", "username": "kid"}})
            assert m.private and m.space == ""
            m = p.parse({"id": "2", "channel_id": "888", "guild_id": "777", "content": "x",
                         "author": {"id": "42"}})
            assert not m.private and m.space == "777"

            code = store.link_code("u_kid", "discord", actor="x")["code"]
            assert await p.handle_inbound(Inbound(chat_id="dm1", user_id="42", username="kid",
                                                  text=f"!link {code}", private=True)) == "linked"
            assert dc.sent[-1]["_channel"] == "dm1" and "act as Kid" in dc.sent[-1]["content"]

            n = Notify()
            n.bind_host(node)
            res = await as_(owner(), lambda: n.send("dinner's ready", user="u_kid"))
            assert res == {"ok": True, "sent": ["discord"]}
            assert dc.sent[-1]["_channel"] == "dm-42"

            assert (await as_(member(), lambda: p.send("hi", chat="888")))["ok"] is False
            store.grant("u_kid", "space", "discord:777", actor="o", words="w")
            assert (await as_(member(), lambda: p.send("hi", chat="888")))["ok"]
            assert dc.sent[-1]["_channel"] == "888"
        finally:
            await p.stop()


# -- the users.* caps ----------------------------------------------------------------

class Client:
    def __init__(self, authz):
        self.authz = authz
        self.workers = {}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "missing.db"))
    az = build_authorizer(str(tmp_path), signer=False)
    az.signer = None
    az.access.set_role("u_bake", "owner", actor="t", words="seed", label="Bake")
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   client=Client(az)), az.access


@pytest.mark.asyncio
async def test_users_caps_for_chat_accounts(hub):
    n, store = hub
    caps = set(n.caps())
    assert {"users.link_code", "users.links", "users.unlink", "users.chat_bot", "users.space",
            "users.spaces"} <= caps
    tiers = n.host.tiers()
    assert tiers["users.space"] == "a" and tiers["users.spaces"] == "r"

    got = await as_(member(), lambda: n.invoke("users.link_code", {"platform": "telegram"},
                                               "human:u_kid"))
    assert got["send"] == f"/link {got['code']}" and "10 minutes" in got["plain"]
    assert "not running" in got["note"]
    assert store.redeem("telegram", got["code"], "42") == "u_kid"
    mine = await as_(member(), lambda: n.invoke("users.links", {}, "human:u_kid"))
    assert [l["platform_user"] for l in mine["links"]] == ["42"] and mine["bots"] == {}

    with pytest.raises(PermissionError):      # a member may not act for someone else
        await as_(member(), lambda: n.invoke("users.link_code", {
            "platform": "telegram", "user": "u_bake"}, "human:u_kid"))
    with pytest.raises(PermissionError):
        await as_(member(), lambda: n.invoke("users.links", {"user": "*"}, "human:u_kid"))
    for_kid = await as_(owner(), lambda: n.invoke("users.link_code", {
        "platform": "discord", "user": "u_kid"}, "human:u_bake"))
    assert for_kid["send"].startswith("!link ")
    agent = principal_for_token(SimpleNamespace(verified=True, kind="agent", agent_id="a1",
                                                key_id="k", label="x", identity="agent:x"))
    with pytest.raises(ValueError, match="signed-in person"):
        await as_(agent, lambda: n.invoke("users.link_code", {"platform": "telegram"}, "token:a1"))

    out = await as_(member(), lambda: n.invoke("users.chat_bot", {
        "platform": "telegram", "secret": "user.u_kid.mybot",
        "words": "kid: use my own bot"}, "human:u_kid"))
    assert out["bot"] == "own"
    out = await as_(member(), lambda: n.invoke("users.unlink", {
        "platform": "telegram", "words": "kid: unlink me"}, "human:u_kid"))
    assert out["unlinked"] is True

    with pytest.raises(PermissionError):
        await as_(member(), lambda: n.invoke("users.space", {
            "platform": "discord", "space": "777", "words": "w"}, "human:u_kid"))
    await as_(owner(), lambda: n.invoke("users.space", {
        "platform": "discord", "space": "777", "name": "Household",
        "words": "Bake: add our server"}, "human:u_bake"))
    await as_(owner(), lambda: n.invoke("users.grant", {
        "user": "u_kid", "kind": "space", "ref": "discord:777",
        "words": "Bake: the kid can talk to rook there"}, "human:u_bake"))
    sp = await as_(member(), lambda: n.invoke("users.spaces", {}, "human:u_kid"))
    assert sp["spaces"][0]["ref"] == "discord:777" and sp["spaces"][0]["granted"] == ["u_kid"]
    me = await as_(member(), lambda: n.invoke("users.me", {}, "human:u_kid"))
    assert "chat space discord:777" in me["plain"]
