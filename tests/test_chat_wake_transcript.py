"""rook_chat_wake hands the woken agent the room's most recent messages, so it
answers what the room is talking about now rather than how the room began."""
import json

import pytest

from rook.band_mcp.chat_rooms import ChatStore

STATIC = "s" * 40


class HermesBand:
    def __init__(self):
        self.workers = {"w1": {"worker_id": "w1", "name": "nas", "band": "x",
                               "caps": ["hermes.chat"], "last_seen": 0}}
        self.calls = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, **kw):
        self.calls.append((cap, args))
        return {"id": "c", "from": target, "ok": True, "result": {"stdout": "On it."}}


@pytest.mark.asyncio
async def test_wake_sends_the_latest_messages_oldest_first(tmp_path):
    import rook.band_mcp.server as srv
    band = HermesBand()
    chat = ChatStore(str(tmp_path / "chat.db"))
    rid = chat.start("planning", "agent:me", ["agent:nas"])["room"]
    for i in range(40):
        chat.send(rid, "agent:me", f"message number {i:02d}", [], False)
    mcp, _ = srv.build_server(band, public_url="https://mcp.example.com",
                              persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                              journal_path=str(tmp_path / "journal.db"), chat=chat)
    wake = mcp._tool_manager._tools["rook_chat_wake"].fn
    out = json.loads(await wake(room=rid, worker="nas"))
    assert out["ok"], out
    prompt = next(a["message"] for c, a in band.calls if c == "hermes.chat")
    assert "message number 39" in prompt and "message number 10" in prompt
    assert "message number 09" not in prompt and "message number 00" not in prompt
    assert prompt.index("message number 10") < prompt.index("message number 39")
