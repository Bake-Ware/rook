"""voice.speak (Android APK plugin) against a fake SpeakBridge, no device needed."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PY = Path(__file__).parents[1] / "android/app/src/main/python"


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules, "rook_android.androidctx",
                        SimpleNamespace(app_context=lambda: None))
    spec = importlib.util.spec_from_file_location("speak_android_test",
                                                  PY / "rook_android/plugins/speak_android.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.POLL_S = 0.001
    return m


class FakeBridge:
    """Mimics SpeakBridge's JSON-string API; jobs finish after `ticks` status polls."""

    def __init__(self, ticks=2, final="done", engine=True):
        self.ticks, self.final, self.engine = ticks, final, engine
        self.spoken, self.chats, self.polls = [], [], {}

    def engineAvailable(self, ctx): return self.engine
    def warmUp(self, ctx): self.warmed = True
    def chat(self, text): self.chats.append(text)
    def stop(self): return json.dumps({"ok": True, "stopped": 0})

    def speak(self, ctx, text, voice, rate, pitch, interrupt):
        sid = f"say-{len(self.spoken)}"
        self.spoken.append(dict(id=sid, text=text, voice=voice, rate=rate, pitch=pitch, interrupt=interrupt))
        return json.dumps({"ok": True, "id": sid, "state": "queued", "volume": "7/15"})

    def status(self, sid):
        n = self.polls[sid] = self.polls.get(sid, 0) + 1
        if self.ticks is not None and n >= self.ticks:
            return json.dumps({"ok": True, "id": sid, "state": self.final, "done": True})
        return json.dumps({"ok": True, "id": sid, "state": "queued", "done": False, "waiting_for": "voice reply"})


def plugin(mod, monkeypatch, bridge):
    monkeypatch.setattr(mod, "_Bridge", bridge)
    monkeypatch.setattr(mod, "app_context", lambda: object())
    return mod.AndroidSpeakPlugin()


def test_split_text_keeps_short_text_and_cuts_long_at_sentences(mod):
    assert mod.split_text("  hello \n world ") == ["hello world"]
    assert mod.split_text("") == []
    text = ("This is a sentence. " * 400).strip()
    chunks = mod.split_text(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert " ".join(chunks) == text
    blob = "x" * 1200  # no boundary at all: hard cut
    assert [len(c) for c in mod.split_text(blob, limit=500)] == [500, 500, 200]


def test_unavailable_off_device_and_without_engine(mod, monkeypatch):
    assert mod.AndroidSpeakPlugin().available() is False
    assert plugin(mod, monkeypatch, FakeBridge(engine=False)).available() is False
    b = FakeBridge()
    assert plugin(mod, monkeypatch, b).available() is True and b.warmed
    monkeypatch.setattr(mod, "_Bridge", None)
    assert asyncio.run(mod.AndroidSpeakPlugin()._speak("hi"))["ok"] is False


def test_speak_waits_until_done_and_posts_chat(mod, monkeypatch):
    b = FakeBridge(ticks=3)
    p = plugin(mod, monkeypatch, b)
    r = asyncio.run(p._speak("Build finished", rate=9, pitch="bad", voice=" en-GB "))
    assert r["ok"] and r["spoken"] and r["state"] == "done" and r["chunks"] == 1
    assert b.spoken == [dict(id="say-0", text="Build finished", voice="en-GB", rate=4.0, pitch=1.0, interrupt=False)]
    assert b.chats == ["Build finished"]


def test_speak_no_wait_returns_id_and_respects_chat_false(mod, monkeypatch):
    b = FakeBridge(ticks=None)
    r = asyncio.run(plugin(mod, monkeypatch, b)._speak("hi", wait=False, chat=False, interrupt=True))
    assert r == {"ok": True, "id": "say-0", "chunks": 1, "chat": False, "volume": "7/15", "state": "queued"}
    assert b.chats == [] and b.spoken[0]["interrupt"] is True


def test_long_text_only_first_chunk_interrupts(mod, monkeypatch):
    b = FakeBridge()
    monkeypatch.setattr(mod, "MAX_CHUNK", 100)
    text = "One two three four five. " * 20
    r = asyncio.run(plugin(mod, monkeypatch, b)._speak(text, interrupt=True))
    assert r["ok"] and r["chunks"] > 1 and r["ids"][-1] == r["id"]
    assert [s["interrupt"] for s in b.spoken] == [True] + [False] * (len(b.spoken) - 1)
    assert len(b.chats) == 1


def test_wait_timeout_and_stop_and_error(mod, monkeypatch):
    b = FakeBridge(ticks=None)
    r = asyncio.run(plugin(mod, monkeypatch, b)._speak("hi", timeout=0))
    assert r["ok"] and r["timed_out"] and r["waiting_for"] == "voice reply" and not r["spoken"]
    b = FakeBridge(final="stopped")
    r = asyncio.run(plugin(mod, monkeypatch, b)._speak("hi"))
    assert r["ok"] and r["state"] == "stopped" and r["spoken"] is False and r["stopped_by"]
    b = FakeBridge(final="error")
    r = asyncio.run(plugin(mod, monkeypatch, b)._speak("hi"))
    assert r["ok"] is False and r["state"] == "error"


def test_empty_text_rejected(mod, monkeypatch):
    b = FakeBridge()
    assert asyncio.run(plugin(mod, monkeypatch, b)._speak("   "))["ok"] is False
    assert b.spoken == []


def test_caps_describe_schema(mod):
    from rook.core.registry import CapabilityRegistry
    p = mod.AndroidSpeakPlugin()
    reg = CapabilityRegistry()
    for name, fn in p.caps().items():
        reg.register(name, fn)
    d = reg.describe("voice.")
    assert set(d) == {"voice.speak", "voice.speak_status", "voice.speak_stop", "voice.speak_voices"}
    params = {x["name"]: x for x in d["voice.speak"]["params"]}
    assert params["text"]["required"] and params["text"]["type"] == "str"
    assert params["interrupt"]["default"] is False and params["chat"]["default"] is True
    assert params["timeout"]["default"] == 60
    assert d["voice.speak"]["risk"] == "write" and d["voice.speak"]["tags"] == ["physical"]
