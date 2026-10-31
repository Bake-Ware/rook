"""The hub service plugins on a real 0.5 hub (rook.core2.hub.runtime, in
memory band) through the HTTP door: notify.send reaching people on the phone
(notification.post on their device via ctx.call_on), Telegram and Discord
(fake Bot API, REST and Gateway), link codes, inbound messages as tainted
events, per-person settings, the sends_as hold on posts, and llm.complete
routed to its chosen provider with the llm.* builtins."""

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("aiohttp")

from fake_services import FakeClaude, FakeDiscord, FakeOpenAI, FakeTelegram, serve  # noqa: E402
from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback, _key  # noqa: E402
from rook.core2.hub.vault import Vault  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the hub service plugins."
TG_TOKEN, DC_TOKEN = "123456:telegram-test-token", "discord-test-token"

# A device that takes notification.post, served by the hub node itself (a
# plugin named notification, so its cap is notification.post): the phone.
PHONE = """\
manifest: 1
plugin: notification
version: 1.0.0
doc: A fake phone.
runs_on: hub
caps:
  post:
    doc: Show a notification.
    risk: write
    scope: local
    on: hub
    run: scripts/phone.py:post
    args:
      text: { type: str, required: true }
      title: { type: str }
"""
PHONE_PY = """\
import json

def post(ctx, text, title=None):
    with open(ctx.data / "shown.jsonl", "a") as f:
        f.write(json.dumps({"text": text, "title": title, "person": ctx.person}) + "\\n")
    return {"shown": True}
"""


async def http(hub, path, body=None, token=None):
    def go():
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{hub.doors[0].port}{path}", data=data,
                                     method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    return await asyncio.to_thread(go)


async def call(hub, token, cap, **args):
    return await http(hub, "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY}, token)


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def library(tmp_path, *names):
    lib = tmp_path / "lib"
    (lib / "notification" / "scripts").mkdir(parents=True)
    (lib / "notification" / "plugin.yaml").write_text(PHONE)
    (lib / "notification" / "scripts" / "phone.py").write_text(PHONE_PY)
    for name in names:
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return lib


def shown(hub) -> list:
    path = hub.data / "plugins" / "notification" / "shown.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_hub(cfg, body, timeout=40):
    async def main():
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            owner = (hub.data / "owner.token").read_text().strip()
            await body(hub, owner)
        finally:
            await hub.stop()
    asyncio.run(asyncio.wait_for(main(), timeout))


def seed_vault(data, **values):
    """Secrets in the hub's vault before it starts (as ``secret set`` would)."""
    (data / "keys").mkdir(parents=True, exist_ok=True)
    _key(data / "keys" / "root.key")
    v = Vault(data).unlock()
    for name, value in values.items():
        v.set(name, value, by="test")


def users():
    return {"bake": {"preset": "owner"}, "ann": {"preset": "adult"},
            "kid": {"preset": "child", "created": time.time()}}


def test_notify_reaches_people_on_phone_telegram_and_discord(tmp_path):
    seed_vault(tmp_path / "data", **{"tg-bot": TG_TOKEN, "dc-bot": DC_TOKEN})
    tg, dc = FakeTelegram(TG_TOKEN), FakeDiscord(DC_TOKEN)

    async def body(hub, owner):
        ann = hub.add_token(Caller(person="person:ann"), label="ann")
        events = []
        hub.listeners.append(lambda event, payload, source: events.append((event, payload, source)))
        assert {"notify", "telegram", "discord", "notification"} <= set(hub.plugins.plugins)
        assert hub.plugins.caps()["notify.send"]["run"] == "scripts/notify.py:send"    # notify is the provider

        # Nobody has a channel yet: the call fails and says why per channel.
        out = await call(hub, owner, "notify.send", text="hello")
        assert not out["ok"] and "couldn't reach person:bake" in out["error"], out
        assert "phone: no phone set" in out["error"] and "hasn't linked Telegram" in out["error"], out

        # Per-person settings: your own; an adult can't set someone else's.
        assert (await call(hub, owner, "setting.set", plugin="notify", key="phone", value="rook"))["ok"]
        out = await call(hub, ann, "setting.set", plugin="notify", key="phone", value="rook", person="bake")
        assert not out["ok"] and "someone else's" in out["error"], out
        out = await call(hub, ann, "setting.list", plugin="notify", person="bake")
        assert not out["ok"] and "someone else's" in out["error"], out
        assert (await call(hub, owner, "setting.list", plugin="notify", person="ann"))["ok"]
        out = await call(hub, owner, "setting.set", plugin="notify", key="reach", value=["fax"])
        assert not out["ok"] and out["error"].startswith("bad value"), out
        listed = (await call(hub, owner, "setting.list", plugin="notify"))["result"]["settings"]
        assert {(s["key"], s.get("where", "hub"), str(s["value"])) for s in listed} >= {
            ("phone", "person", "rook"), ("channels", "hub", "['phone', 'telegram', 'discord']")}
        assert json.loads((hub.data / "person-settings.json").read_text()) == {
            "person:bake": {"notify": {"phone": "rook"}}}

        # Blank to: the owners; the phone is first in the hub's order.
        out = await call(hub, owner, "notify.send", text="Backup done.", title="Home")
        assert out["ok"] and out["result"] == {"sent": [{"to": "person:bake", "channel": "phone"}], "errors": {}}
        assert shown(hub) == [{"text": "Backup done.", "title": "Home", "person": "person:bake"}]
        post = hub.journal.query(cap="notification.post", limit=1)[0]
        assert (post["person"], post["ok"]) == ("person:bake", True)       # as the caller, journaled

        # Telegram: link with a one-time code sent to the bot.
        link = (await call(hub, owner, "telegram.link"))["result"]
        assert link["how"] == f"Send /link {link['code']} to @rook_bot in a direct message."
        tg.push({"message_id": 1, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "username": "bk"},
                 "text": "/link 00000000"})
        await tg.wait(lambda: any("wrong or has expired" in r["body"]["text"] for r in tg.sent("/sendMessage")))
        tg.push({"message_id": 2, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "username": "bk"},
                 "text": f"/link {link['code']}"})
        await tg.wait(lambda: any("Linked: you are bake" in r["body"]["text"] for r in tg.sent("/sendMessage")))
        assert not [e for e in events if e[0] == "telegram.message"]          # /link isn't an event

        out = await call(hub, owner, "notify.send", text="Deploy waits on you", to="bake", channel="telegram")
        assert out["result"]["sent"] == [{"to": "person:bake", "channel": "telegram"}], out
        assert tg.sent("/sendMessage")[-1]["body"]["chat_id"] == "42"
        assert tg.sent("/sendMessage")[-1]["body"]["text"] == "Deploy waits on you"

        # Inbound: a tainted event naming the linked person.
        tg.push({"message_id": 3, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "username": "bk"},
                 "text": "what's on today?"})
        ev = await until(lambda: next((e for e in events if e[0] == "telegram.message"), None))
        assert ev[1] == {"text": "what's on today?", "chat": "42", "private": True, "from": "bk", "from_id": "42",
                         "message": "3", "person": "person:bake"} and ev[2] == "telegram"
        assert hub.plugins.plugins["telegram"].manifest["emits"]["telegram.message"]["taints"] is True
        tg.push({"message_id": 4, "chat": {"id": -100, "type": "group"}, "from": {"id": 7, "username": "x"},
                 "text": "hi all"})
        ev = await until(lambda: next((e for e in events if e[1].get("from_id") == "7"), None))
        assert "person" not in ev[1] and ev[1]["private"] is False

        # Discord: !link in a DM over the Gateway, then a DM through notify.
        await dc.wait(lambda: dc.identified)
        assert dc.identified[0]["token"] == DC_TOKEN and dc.identified[0]["intents"] == (1 << 9) | (1 << 12) | (1 << 15)
        link = (await call(hub, owner, "discord.link"))["result"]
        dc.push({"id": "m1", "channel_id": "300", "author": {"id": "77", "username": "bakeb"},
                 "content": f"!link {link['code']}"})
        await dc.wait(lambda: any("Linked: you are bake" in r["body"]["content"] for r in dc.sent("/messages")))
        out = await call(hub, owner, "notify.send", text="ping", channel="discord", to="bake", title="Rook")
        assert out["result"]["sent"] == [{"to": "person:bake", "channel": "discord"}], out
        assert dc.sent("/messages")[-1]["path"] == "/api/channels/300/messages"
        assert dc.sent("/messages")[-1]["body"]["content"] == "**Rook**\nping"
        dc.push({"id": "m2", "channel_id": "900", "guild_id": "1", "author": {"id": "77", "username": "bakeb"},
                 "content": "status?"})
        ev = await until(lambda: next((e for e in events if e[0] == "discord.message"), None))
        assert ev[1]["person"] == "person:bake" and ev[1]["guild"] == "1" and ev[1]["private"] is False
        await dc.wait(lambda: dc.heartbeats)

        # channel all: every channel bake has; the person's own order.
        out = await call(hub, owner, "notify.send", text="all hands", channel="all")
        assert [s["channel"] for s in out["result"]["sent"]] == ["phone", "telegram", "discord"], out
        assert (await call(hub, owner, "setting.set", plugin="notify", key="reach", value=["discord", "phone"]))["ok"]
        out = await call(hub, owner, "notify.send", text="yours", to="bake")
        assert out["result"]["sent"] == [{"to": "person:bake", "channel": "discord"}], out

        # notify.channels: what reaches a person now.
        got = (await call(hub, owner, "notify.channels"))["result"]
        assert got["order"] == ["discord", "phone"]
        rows = {r["channel"]: r for r in got["channels"]}
        assert rows["phone"] == {"channel": "phone", "installed": True, "linked": True, "worker": "rook"}
        assert rows["telegram"]["linked"] and rows["telegram"]["connected"] and rows["telegram"]["bot"] == "rook_bot"
        assert rows["discord"]["linked"] and rows["discord"]["bot"] == "rook-bot"
        got = (await call(hub, owner, "notify.channels", to="ann"))["result"]
        assert not any(r["linked"] for r in got["channels"])

        # ann has no channel: one failing person fails the call.
        out = await call(hub, owner, "notify.send", text="hi ann", to="ann")
        assert not out["ok"] and "person:ann" in out["error"]

        # A post to a chat reaches people outside the band: sends_as holds it.
        out = await call(hub, owner, "telegram.post", chat="-100", text="hello group")
        assert out.get("held") and "sends_as" in out["error"], out
        out = await call(hub, owner, "discord.post", chat="900", text="hello server")
        assert out.get("held"), out

        # Unlink: yours; an adult can't unlink someone else.
        out = await call(hub, ann, "telegram.unlink", to="bake")
        assert not out["ok"] and "only an owner" in out["error"]
        assert (await call(hub, owner, "telegram.unlink"))["result"] == {"person": "person:bake", "unlinked": True}
        out = await call(hub, owner, "notify.send", text="x", channel="telegram")
        assert not out["ok"] and "hasn't linked Telegram" in out["error"]

    async def main():
        async with serve(tg.app) as tg_url, serve(dc.app) as dc_url:
            dc.base = dc_url
            cfg = Config(data=tmp_path / "data", band=BAND, library=library(tmp_path, "notify", "telegram", "discord"),
                         users=users(), doors={"http": {"bind": "127.0.0.1:0"}},
                         settings={"telegram": {"token": "tg-bot", "api": tg_url, "poll": 1},
                                   "discord": {"token": "dc-bot", "api": dc_url + "/api"}})
            hub = Hub(cfg, transport=Loopback())
            await hub.start()
            try:
                await body(hub, (hub.data / "owner.token").read_text().strip())
            finally:
                await hub.stop()
    asyncio.run(asyncio.wait_for(main(), 60))


def test_a_channel_that_fails_falls_through(tmp_path):
    # No token in the vault: telegram can't send; the phone still works.
    cfg = Config(data=tmp_path / "data", band=BAND, library=library(tmp_path, "notify", "telegram"), users=users(),
                 doors={"http": {"bind": "127.0.0.1:0"}},
                 settings={"telegram": {"token": "tg-bot", "api": "http://127.0.0.1:9", "poll": 1},
                           "notify": {"channels": ["telegram", "phone"]}})

    async def body(hub, owner):
        assert (await call(hub, owner, "setting.set", plugin="notify", key="phone", value="rook"))["ok"]
        out = await call(hub, owner, "notify.send", text="hi")
        assert out["result"]["sent"] == [{"to": "person:bake", "channel": "phone"}]
        assert "telegram" in out["result"]["errors"]["person:bake"]
        st = (await call(hub, owner, "telegram.status"))["result"]
        assert st["connected"] is False and "tg-bot is not in the vault" in (st["error"] or "")
        # A worker that isn't there: the phone channel says so.
        assert (await call(hub, owner, "setting.set", plugin="notify", key="phone", value="no-such-phone"))["ok"]
        out = await call(hub, owner, "notify.send", text="hi", channel="phone")
        assert not out["ok"] and "unknown worker: no-such-phone" in out["error"], out
    run_hub(cfg, body)


def test_llm_complete_routes_to_the_chosen_provider(tmp_path):
    seed_vault(tmp_path / "data", **{"claude-api": "sk-ant-test"})
    oa, cl = FakeOpenAI(), FakeClaude(key="sk-ant-test")

    async def main():
        async with serve(oa.app) as oa_url, serve(cl.app) as cl_url:
            cfg = Config(data=tmp_path / "data", band=BAND, users=users(),
                         library=library(tmp_path, "llm-openai", "llm-anthropic"),
                         doors={"http": {"bind": "127.0.0.1:0"}}, contracts={"llm.complete": "llm-openai.complete"},
                         settings={"llm-openai": {"url": oa_url + "/v1", "model_two": "qwen", "timeout": "5s"},
                                   "llm-anthropic": {"url": cl_url, "key": "claude-api"}})
            hub = Hub(cfg, transport=Loopback())
            await hub.start()
            try:
                owner = (hub.data / "owner.token").read_text().strip()
                kid = hub.add_token(Caller(person="person:kid"), label="kid")
                msgs = [{"role": "user", "content": "hi"}]
                out = await call(hub, owner, "llm.complete", messages=msgs)
                assert out["ok"] and out["result"]["text"] == "echo: hi" and out["result"]["model"] == "qwen", out
                entry = hub.journal.query(cap="llm-openai.complete", limit=1)[0]
                assert (entry["person"], entry["ok"]) == ("person:bake", True)
                caps = hub.plugins.caps(hub._meta)
                assert caps["llm.complete"]["scope"] == "external" and caps["llm.complete"]["contract"]
                assert not (await call(hub, kid, "llm.complete", messages=msgs))["ok"]   # the child preset

                got = (await call(hub, owner, "llm.providers"))["result"]
                assert got == {"chosen": "llm-openai", "providers": [
                    {"provider": "llm-anthropic", "cap": "llm-anthropic.complete", "chosen": False},
                    {"provider": "llm-openai", "cap": "llm-openai.complete", "chosen": True}]}
                got = (await call(hub, owner, "llm.models"))["result"]
                assert got["provider"] == "llm-openai" and got["models"] == ["llama", "qwen"]
                got = (await call(hub, owner, "llm.models", provider="llm-anthropic"))["result"]
                assert got["systems"]["two"] == "claude-opus-5-5"
                got = (await call(hub, owner, "llm.status"))["result"]["providers"]
                assert [(p["provider"], p["ok"]) for p in got] == [("llm-anthropic", True), ("llm-openai", True)]
                out = await call(hub, owner, "llm.models", provider="nope")
                assert not out["ok"] and "doesn't provide llm.complete" in out["error"]
                out = await call(hub, owner, "llm-anthropic.complete", messages=msgs, model="one")
                assert out["result"]["model"] == "claude-haiku-5-5"
            finally:
                await hub.stop()
    asyncio.run(asyncio.wait_for(main(), 40))
