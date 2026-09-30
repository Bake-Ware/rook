"""End-to-end checks against an isolated test hub (opt-in; see conftest.py)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

CORE_TOOLS = {"rook_whoami", "rook_workers", "rook_caps", "rook_call", "rook_chat_start",
              "rook_chat_send", "rook_chat_read", "rook_console_open", "rook_console_read",
              "rook_console_close"}


def test_mcp_initialize_and_tools_list(hub):
    async def go(s):
        return {t.name for t in (await s.list_tools()).tools}
    names = hub.run(go)
    assert CORE_TOOLS <= names, sorted(CORE_TOOLS - names)
    if hub.knowledge:
        assert "rook_knowledge" in names


def test_workers_visible(hub):
    roster = hub.call("rook_workers")
    names = {w["name"] for w in roster}
    assert set(hub.workers) <= names


def test_shell_exec_on_test_worker(hub):
    worker = hub.workers[0]
    marker = uuid.uuid4().hex
    reply = hub.call("rook_call", cap="shell.exec", worker=worker,
                     args={"argv": ["sh", "-c", f'echo {marker}; echo "$HOME"']})
    assert reply["ok"], reply
    result = reply["result"]
    assert result["code"] == 0, result
    out = result["stdout"].splitlines()
    assert out[0] == marker
    # Test workers must run with a throwaway HOME inside the test hub's data dir.
    data_dir = hub.env.get("ROOK_IT_DATA_DIR")
    if data_dir:
        assert out[1].startswith(data_dir), out[1]


def test_chat_room_round_trip(hub):
    text = f"integration ping {uuid.uuid4().hex[:8]}"

    async def go(s):
        room = (await hub.acall(s, "rook_chat_start", title="integration chat"))["room"]
        sent = await hub.acall(s, "rook_chat_send", room=room, text=text)
        read = await hub.acall(s, "rook_chat_read", room=room)
        await hub.acall(s, "rook_chat_delete", room=room)
        return sent, read
    sent, read = hub.run(go)
    assert sent["ok"], sent
    assert read["ok"], read
    assert text in [m["text"] for m in read["messages"]]


def test_console_open_read_close(hub):
    worker = hub.workers[-1]
    marker = uuid.uuid4().hex

    async def go(s):
        opened = await hub.acall(s, "rook_console_open", worker=worker,
                                 task="integration test console",
                                 argv=["sh", "-c", f"echo {marker}"])
        assert opened.get("ok"), opened
        room = opened["room"]
        lines = []
        for _ in range(50):
            read = await hub.acall(s, "rook_console_read", room=room)
            lines = [ln["text"] for ln in read.get("lines", [])]
            if marker in lines and read.get("state") != "live":
                break
            await asyncio.sleep(0.2)
        closed = await hub.acall(s, "rook_console_close", room=room,
                                 summary="integration test", kill=True)
        return lines, closed
    lines, closed = hub.run(go)
    assert marker in lines
    assert closed.get("ok"), closed


def test_knowledge_create_and_search(hub):
    if not hub.knowledge:
        pytest.skip("test hub started with --no-knowledge")
    word = "it" + uuid.uuid4().hex[:10]

    async def go(s):
        created = await hub.acall(s, "rook_knowledge", action="create",
                                  data={"title": f"Integration note {word}",
                                        "body": f"searchable token {word}"},
                                  request_id=uuid.uuid4().hex)
        found = await hub.acall(s, "rook_knowledge", action="search", query=word)
        return created, found
    created, found = hub.run(go)
    assert created["ok"], created
    ids = [r["id"] for r in found["result"]["results"]]
    assert created["result"]["id"] in ids
