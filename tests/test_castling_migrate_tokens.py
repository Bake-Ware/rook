"""0.4 MCP keys carried to 0.5 (castling/switchover/migrate/tokens.py) and the
MCP door answering on several names (doors.mcp.oauth as a list).

Bake (voice, Nov 1 2026): "can we just migrate the existing... MCP keys...
the tokens, to the new system? So they just pick up when DNS switches."
Every test runs under a hard timeout."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import nacl.secret
import pytest

pytest.importorskip("jsonschema")

from castling.switchover.migrate import migrate, render, tokens, verify  # noqa: E402
from rook.core2.hub import runtime  # noqa: E402
from rook.core2.hub.doors.mcp_oauth import OAuthShim, check_urls  # noqa: E402
from test_castling_migrate import NOW, make_04, make_05  # noqa: E402
from test_core2_hub_runtime import WHY, WID, FakeWorker, config, run, until  # noqa: E402
from test_core2_oauth import http, rpc_body  # noqa: E402

KEY_WEB = "k-web-" + "a" * 37
KEY_AUTUMN = "k-aut-" + "b" * 37
KEY_ORPHAN = "k-orp-" + "c" * 37
KEY_OLD = "k-old-" + "d" * 37
STATIC = "s-tat-" + "e" * 43
VALUES = (KEY_WEB, KEY_AUTUMN, KEY_ORPHAN, KEY_OLD, STATIC)


def add_keys(copy, static=True):
    """0.4's oauth.json (API keys), an access.db row for Autumn's key, and the
    static token in 0.4's vault, as on the live hub."""
    keys = [{"token": KEY_WEB, "id": "bd549ebe", "name": "Claude web", "agent_id": "agent_web",
             "created_at": NOW - 100, "expires_at": None},
            {"token": KEY_AUTUMN, "id": "54436296", "name": "Autumn's laptop", "agent_id": "agent_aut",
             "created_at": NOW - 90, "expires_at": None},
            {"token": KEY_ORPHAN, "id": "2bd09b72", "name": "claude web", "agent_id": "agent_orphan",
             "created_at": NOW - 80},                                    # no access.db row: the first owner's
            {"token": KEY_OLD, "id": "02007272", "name": "old", "agent_id": "agent_old",
             "created_at": NOW - 70, "expires_at": NOW - 1}]             # expired: left out
    (copy / "oauth.json").write_text(json.dumps({"api_tokens": keys}))
    with sqlite3.connect(copy / "access.db") as db:
        db.execute("INSERT INTO tokens (agent_id, user_id, label, created, actor) VALUES (?,?,?,?,?)",
                   ("agent_aut", "u2", "Autumn's laptop", NOW, "u2"))
    if static:
        box = nacl.secret.SecretBox((copy / "vault.key").read_bytes())
        with sqlite3.connect(copy / "vault.db") as db:
            db.execute("INSERT INTO secrets (name, value, description, created, updated, set_by) "
                       "VALUES (?,?,?,?,?,?)", (tokens.STATIC, box.encrypt(STATIC.encode()), "static", NOW, NOW, "u1"))


def test_agent_names():
    taken = {"static"}
    assert [tokens.agent_name(n, taken) for n in ("Claude web", "claude web", "Dj", "foundry desktop",
                                                  "minecraft-server", "9lives", "", "static")] == \
        ["claude-web", "claude-web-2", "dj", "foundry-desktop", "minecraft-server", "key-9lives", "key", "static-2"]
    assert all(tokens.AGENT.fullmatch(n) for n in taken)


def test_dry_run_lists_every_key_by_hash_never_by_value(tmp_path):
    copy = make_04(tmp_path)
    add_keys(copy)
    r = migrate(copy, make_05(tmp_path))
    got = {t["label"]: (t["person"], t["agent"]) for t in r["tokens"]}
    assert got == {"Claude web (0.4 key bd549ebe)": ("person:bake", "agent:claude-web"),
                   "Autumn's laptop (0.4 key 54436296)": ("person:autumn-r", "agent:autumn-s-laptop"),
                   "claude web (0.4 key 2bd09b72)": ("person:bake", "agent:claude-web-2"),
                   "0.4 static token": ("person:bake", "agent:static")}
    assert {t["id"] for t in r["tokens"]} == {tokens.digest(k)[:12] for k in (KEY_WEB, KEY_AUTUMN, KEY_ORPHAN, STATIC)}
    assert any("old (02007272) had expired" in n for n in r["notes"])
    assert not any("API key" in n and "mint" in n for n in r["notes"])
    text = render(r) + json.dumps(r)
    assert "4 0.4 keys work on 0.5 unchanged" in render(r)
    for v in VALUES:
        assert v not in text


def test_apply_writes_hashes_the_hub_reads_and_a_rerun_keeps_them(tmp_path):
    copy = make_04(tmp_path)
    add_keys(copy)
    cfg = make_05(tmp_path)
    r = migrate(copy, cfg, apply=True)
    assert r["tokens_written"] == {"written": 4, "kept": 0}
    data = cfg.parent / "data"
    raw = (data / "tokens.json").read_text()
    for v in VALUES:
        assert v not in raw
    for f in (data / "migrate").iterdir():
        assert all(v not in f.read_text() for v in VALUES)
    stored = json.loads(raw)
    assert stored[tokens.digest(KEY_WEB)]["rights"] == {"grants": ["*"]}       # not in hub.yaml: every grant
    assert stored[tokens.digest(KEY_WEB)]["by"] == "migrate:0.4"
    assert migrate(copy, cfg, apply=True)["tokens_written"] == {"written": 0, "kept": 4}
    assert json.loads((data / "tokens.json").read_text()) == stored

    rows = {k["kind"]: k for k in verify.verify(copy, cfg)["kinds"]}
    row = rows["MCP keys (API keys + static token)"]
    assert (row["source"], row["moved"], row["missing"]) == (4, 4, 0)

    async def go():
        hub = runtime.Hub(runtime.load_config(cfg), transport=runtime.Loopback())
        await hub.start()
        try:
            assert hub.authenticate(KEY_WEB) == runtime.Caller("person:bake", "agent:claude-web")
            assert hub.authenticate(KEY_AUTUMN) == runtime.Caller("person:autumn-r", "agent:autumn-s-laptop")
            assert hub.authenticate(STATIC) == runtime.Caller("person:bake", "agent:static")
            assert hub.authenticate(KEY_OLD) is None
        finally:
            await hub.stop()
    run(go(), timeout=60)


def test_a_key_lost_since_shows_in_verify(tmp_path):
    copy = make_04(tmp_path)
    add_keys(copy, static=False)
    cfg = make_05(tmp_path)
    r = migrate(copy, cfg, apply=True)
    assert any(f"no {tokens.STATIC} in the 0.4 vault" in n for n in r["notes"])
    tj = cfg.parent / "data" / "tokens.json"
    stored = json.loads(tj.read_text())
    stored.pop(tokens.digest(KEY_AUTUMN))
    tj.write_text(json.dumps(stored))
    row = {k["kind"]: k for k in verify.verify(copy, cfg)["kinds"]}["MCP keys (API keys + static token)"]
    assert (row["source"], row["missing"], row["examples"]) == (3, 1, ["Autumn's laptop (0.4 key 54436296)"])


def test_an_agent_in_hub_yaml_keeps_its_own_rights(tmp_path):
    entries, _ = tokens.keys(tmp_path, [], [{"from": "u1", "name": "bake", "preset": "owner"}],
                             [{"name": tokens.STATIC, "value": STATIC}], {"static": {"grants": ["notify.*"]}})
    assert entries[0]["agent"] == "agent:static" and "rights" not in entries[0]


# -- the door on several names -----------------------------------------------------------

def test_the_oauth_option_takes_several_names():
    assert check_urls("https://a.example") == ["https://a.example"]
    assert check_urls(["https://a.example/", "https://b.example", "https://a.example"]) == \
        ["https://a.example", "https://b.example"]
    for bad in ([], ["https://a.example", "http://b.example"], ["https://a.example/mcp"]):
        with pytest.raises(ValueError):
            check_urls(bad)
    s = OAuthShim(["https://castling.example", "https://rook.example"], "/mcp", lambda t: None,
                  lambda p: "t")
    assert s.resource == "https://castling.example/mcp"                     # the one the hub shows
    assert s.challenge("ROOK.example") == \
        'Bearer resource_metadata="https://rook.example/.well-known/oauth-protected-resource/mcp"'
    assert s.challenge("evil.example") == s.challenge() == \
        'Bearer resource_metadata="https://castling.example/.well-known/oauth-protected-resource/mcp"'
    _, prm, _ = s.handle("GET", "/.well-known/oauth-protected-resource/mcp", "", b"", host="rook.example")
    assert prm["resource"] == "https://rook.example/mcp" and prm["authorization_servers"] == ["https://rook.example"]
    _, asm, _ = s.handle("GET", "/.well-known/oauth-authorization-server", "", b"", host="rook.example")
    assert (asm["issuer"], asm["token_endpoint"]) == ("https://rook.example", "https://rook.example/token")


def test_a_04_key_works_through_the_door_on_its_old_name(tmp_path):
    """The 0.4 claude.ai connector's case: it holds a 0.4 key as access and
    refresh token and talks to rook.example; after the move it reaches the
    0.5 door with that key, and the journal names its agent."""
    copy = make_04(tmp_path / "src")
    add_keys(copy)
    entries, _ = tokens.keys(copy, [], [{"from": "u1", "name": "bake", "preset": "owner"}], [], {})
    data = tmp_path / "data"
    data.mkdir()
    tokens.put(data, entries)

    async def go():
        band = runtime.Loopback()
        cfg = config(tmp_path)
        cfg.doors = {"mcp": {"bind": "127.0.0.1:0",
                             "oauth": ["https://castling.example", "https://rook.example", "https://mcp.rook.example"]}}
        hub = runtime.Hub(cfg, transport=band)
        await hub.start()
        try:
            w = FakeWorker(hub, band, tmp_path / "w")
            await w.announce()
            await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            base = f"http://127.0.0.1:{hub.doors[0].port}"
            json_ct = {"Content-Type": "application/json"}
            for host in ("rook.example", "mcp.rook.example"):
                status, hdrs, _ = await http(base + "/mcp", rpc_body("ping"), {**json_ct, "Host": host})
                assert status == 401 and f'"https://{host}/.well-known/' in hdrs["WWW-Authenticate"]
                status, _, prm = await http(base + "/.well-known/oauth-protected-resource/mcp", headers={"Host": host})
                assert status == 200 and prm["resource"] == f"https://{host}/mcp"
            bearer = {**json_ct, "Host": "rook.example", "Authorization": f"Bearer {KEY_WEB}"}
            call = {"name": "call", "arguments": {"worker": WID, "cap": "echo.say", "args": {"text": "hi"},
                                                  "why": WHY}}
            status, _, out = await http(base + "/mcp", rpc_body("tools/call", call), bearer)
            assert status == 200 and out["result"]["isError"] is False, out
            caller = runtime.Caller("person:bake", "agent:claude-web")
            entry = (await hub.journal_view(caller, {"cap": "echo.say"}))["entries"][0]
            assert (entry["agent"], entry["person"]) == ("agent:claude-web", "person:bake")
            status, _, tok = await http(base + "/token", f"grant_type=refresh_token&refresh_token={KEY_WEB}".encode(),
                                        {"Content-Type": "application/x-www-form-urlencoded", "Host": "rook.example"})
            assert status == 200 and tok["access_token"] == KEY_WEB       # 0.4's shim answered the same
        finally:
            await hub.stop()
    run(go(), timeout=60)
