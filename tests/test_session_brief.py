"""Session brief and receipt (design item 12, rook/band_mcp/brief.py)."""
import json

import pytest

from rook.band_mcp import brief, envelope
from test_token_envelope import hub

CURRENT = "abcd1234"


# -- the per-session state machine ------------------------------------------------

def test_first_call_is_briefed_once_for_a_client_that_never_echoes():
    b = brief.Briefs()
    assert b.due("s", CURRENT) == brief.FIRST
    for _ in range(5):
        assert b.due("s", CURRENT) is None  # no receipt ever: told once, not on every reply


def test_parallel_first_calls_carry_it_once():
    b = brief.Briefs()
    assert [b.due("s", CURRENT), b.due("s", CURRENT), b.due("s", CURRENT)] == [brief.FIRST, None, None]


def test_missing_receipt_after_an_echo_resends_once():
    b = brief.Briefs()
    assert b.due("s", CURRENT) == brief.FIRST
    assert b.due("s", CURRENT, CURRENT) is None
    assert b.due("s", CURRENT) == brief.MISSING  # compaction: the agent forgot it
    assert b.due("s", CURRENT) is None  # one resend per lapse
    assert b.due("s", CURRENT, CURRENT) is None
    assert b.due("s", CURRENT) == brief.MISSING


def test_stale_receipt_gets_the_new_one_once():
    b = brief.Briefs()
    assert b.due("s", CURRENT, "old00000") == brief.STALE
    assert b.due("s", CURRENT, "old00000") is None
    assert b.due("s", CURRENT, "other000") == brief.STALE
    assert b.due("s", CURRENT, CURRENT) is None


def test_reconnect_with_a_valid_receipt_is_not_briefed():
    b = brief.Briefs()
    assert b.due("new-session", CURRENT, CURRENT) is None
    assert b.due("new-session", CURRENT) == brief.MISSING  # it echoed, so a gap means a loss


def test_tool_without_receipt_param_never_counts_as_missing():
    b = brief.Briefs()
    assert b.due("s", CURRENT, carries=False) == brief.FIRST
    assert b.due("s", CURRENT, CURRENT) is None
    assert b.due("s", CURRENT, carries=False) is None


def test_undelivered_brief_waits_for_a_reply_that_can_carry_it():
    b = brief.Briefs()
    b.held("s", b.due("s", CURRENT))  # e.g. a JSON array reply, or the call failed
    assert b.due("s", CURRENT) == brief.FIRST
    assert b.due("s", CURRENT) is None
    b.held("s", brief.STALE)
    assert b.due("s", CURRENT, CURRENT) is None  # it holds the current one after all
    assert b.due("s", CURRENT, CURRENT) is None


def test_receipt_is_short_stable_and_per_caller():
    r = brief.receipt_for(brief.standing("a.claudecode.web", "agent"))
    assert len(r) == 8 and r == brief.receipt_for(brief.standing("a.claudecode.web", "agent"))
    assert r != brief.receipt_for(brief.standing("b.claudecode.web", "agent"))
    shared = brief.standing("static", "shared")
    assert any("static token" in line for line in shared)


def test_build_prefers_claims_over_loose_handoffs():
    out = brief.build(brief.FIRST, "me", "agent", claims=[{"id": "t1"}], handoffs=[{"thread_id": "h"}],
                      unread=[{"room": str(i)} for i in range(9)])
    assert out["claimed"] == [{"id": "t1"}] and "your_handoffs" not in out
    assert len(out["unread_chat"]) == brief.MAX_ROOMS
    bare = brief.build(brief.FIRST, "me", "agent")
    assert set(bare) == {"why", "receipt", "you", "how"}


# -- the plain-text form of rook_call ---------------------------------------------

def test_text_reply_gets_one_rook_line():
    out, ok = envelope.add_notice("hello", "_brief", {"r": 1}, text_line=True)
    assert ok and out == 'hello\n[rook] {"_brief":{"r":1}}'
    out, ok = envelope.add_notice('hi\n[rook] {"_task":"t"}', "_brief", 1, text_line=True)
    assert ok and out == 'hi\n[rook] {"_task":"t","_brief":1}'
    stdout = '{"ok": true, "data": 1}'  # a command's JSON output is output, not an envelope
    out, ok = envelope.add_notice(stdout, "_brief", 1, text_line=True)
    assert ok and out.startswith(stdout + "\n[rook] ")
    out, ok = envelope.add_notice('{"ok":false,"id":"x","error":"e"}', "_brief", 1, text_line=True)
    assert ok and json.loads(out)["_brief"] == 1  # an error reply stays one JSON object
    assert envelope.add_notice("hello", "_brief", 1) == ("hello", False)


# -- over a real MCP session -------------------------------------------------------

def _j(text):
    return json.loads(text)


@pytest.mark.asyncio
async def test_brief_rides_the_first_reply_and_the_receipt_holds_it_back(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        rpc, tool = await env.connect()
        tools = (await rpc("tools/list", {}))["tools"]
        shown = {t["name"] for t in tools if t["inputSchema"]["properties"].get("receipt") == {"type": "string"}}
        names = {t["name"] for t in tools}
        assert shown == brief.ADVERTISED & names and "rook_call" in shown
        assert "rook_whoami" not in shown  # accepted unadvertised (below)

        first = _j((await tool("rook_whoami"))[0])
        b = first["_brief"]
        assert b["why"] == brief.FIRST and b["you"] == {"actor": first["actor"], "kind": "shared"}
        receipt = b["receipt"]
        assert "_brief" not in _j((await tool("rook_whoami"))[0])  # not echoed yet: not repeated

        assert "_brief" not in _j((await tool("rook_whoami", receipt=receipt))[0])
        call = _j((await tool("rook_call", why="test", cap="info.host", worker="worker-a", receipt=receipt))[0])
        assert call["ok"] and "_brief" not in call and env.band.sent[-1] == ("info.host", {})

        again = _j((await tool("rook_call", why="test", cap="info.host", worker="worker-a"))[0])
        assert again["_brief"]["why"] == brief.MISSING and again["_brief"]["receipt"] == receipt
        assert "_brief" not in _j((await tool("rook_whoami"))[0])

        stale = _j((await tool("rook_whoami", receipt="deadbeef"))[0])
        assert stale["_brief"]["why"] == brief.STALE

        # A reconnect (new MCP session, e.g. after a hub restart) that repeats the receipt.
        _, tool2 = await env.connect()
        assert "_brief" not in _j((await tool2("rook_whoami", receipt=receipt))[0])


@pytest.mark.asyncio
async def test_text_mode_rook_call_carries_the_brief_on_its_rook_line(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        _, tool = await env.connect()
        env.band.stdout = "hello\n"
        text, _ = await tool("rook_call", why="test", cap="shell.exec", worker="worker-a", text=True)
        body, _, line = text.rpartition("\n")
        assert body == "hello"
        notices = json.loads(line.removeprefix("[rook] "))
        assert notices["_brief"]["why"] == brief.FIRST and "_tips" in notices


@pytest.mark.asyncio
async def test_brief_replaces_the_start_of_session_round(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        _, tool = await env.connect()
        k = env.mcp._rook_knowledge
        me = {"id": "static", "kind": "shared", "label": "static"}
        c = await k.dispatch("create", kind="concept", data={"title": "C"}, request_id="c", actor=me)
        p = await k.dispatch("create", kind="project", data={"title": "P", "parent": c["id"]},
                             request_id="p", actor=me)
        task = await k.dispatch("create", kind="task", data={"title": "Ship it", "parent": p["id"]},
                                request_id="t", actor=me)
        receipt = _j((await tool("rook_task", action="claim", id=task["id"], request_id="c1"))[0])["_brief"]["receipt"]
        saved = _j((await tool("rook_handoff_save", goal="ship the thing", state="half done",
                               next_steps=["write tests", "open PR"], receipt=receipt))[0])
        assert saved.get("ok", True) and "_brief" not in saved
        chat = env.mcp._rook_chat
        identity = _j((await tool("rook_whoami", receipt=receipt))[0])["identity"]
        room = chat.start("ops", "agent:other", [identity])["room"]
        chat.send(room, "agent:other", "hi", [], False)

        # Compaction: the agent calls without its receipt.
        r = _j((await tool("rook_call", why="test", cap="info.host", worker="worker-a"))[0])
        b = r["_brief"]
        assert b["why"] == brief.MISSING
        (claim,) = b["claimed"]
        assert claim["id"] == task["id"] and claim["title"] == "Ship it" and claim["state"] == "in_progress"
        assert claim["handoff"]["goal"] == "ship the thing"
        assert claim["handoff"]["next_steps"] == ["write tests", "open PR"]
        assert b["unread_chat"][0]["room"] == room
        assert "_unread_chat" not in r  # the brief already says it: not twice in one reply


@pytest.mark.asyncio
async def test_a_failed_call_does_not_use_up_the_brief(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch) as env:
        rpc, tool = await env.connect()
        bad = await rpc("tools/call", {"name": "rook_call", "arguments": {}})  # no cap: invalid
        assert bad.get("isError")
        assert _j((await tool("rook_whoami"))[0])["_brief"]["why"] == brief.FIRST
