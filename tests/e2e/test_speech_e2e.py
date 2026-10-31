"""A voice session through a real 0.5 band (see harness.py): the hub runs the
speech and agents plugins from its library, with a fake System One and a
fake agent backend; the speech engine is the voice service's own worker
process (services/voice/band.py, fake models) announcing the builtin
speech-engine manifest. voice.stt, voice.speaker, voice.turn and voice.tts
go over the band to it as the person speaking, with tickets it verifies."""
from __future__ import annotations

import base64
import io
import shutil
import sys
import wave

from harness import HERE, REPO, until
from speech_fakes import BRAIN, BRAIN_PY, TALKER, TALKER_PY, plugin


def said(text: str) -> dict:
    data = text.encode()
    return {"data": base64.b64encode(data.ljust(len(data) + len(data) % 2, b" ")).decode(), "mime": "audio/L16"}


def spoken(clips: list) -> str:
    out = []
    for f in clips:
        assert f["mime"] == "audio/wav"
        with wave.open(io.BytesIO(base64.b64decode(f["data"]))) as w:
            assert w.getframerate() == 24000
            out.append(w.readframes(w.getnframes()).decode().strip())
    return " ".join(out)


def test_a_voice_session_over_the_band(fresh):
    for name in ("speech", "agents"):
        shutil.copytree(REPO / "rook" / "plugins" / name, fresh.library / name)
    plugin(fresh.library, "llm-openai", TALKER, "talker.py", TALKER_PY)
    plugin(fresh.library, "agents-local", BRAIN, "brain.py", BRAIN_PY)

    fresh.start_hub()
    fresh.enroll()
    fresh.worker = fresh._spawn("worker", [sys.executable, str(HERE / "speech_worker.py"), "--config", "worker.yaml"],
                                fresh.worker_dir)
    worker_id = fresh.worker.wait_log("worker started")["worker"]
    until(lambda: (c := fresh.caps()).get("online") and "speech-engine.stt" in c.get("caps", {}),
          30, "the voice worker online with speech-engine")
    hub = fresh.owner

    out = hub.call("speech.open", {"device": "e2e-worker"}, worker="rook")
    assert out["ok"], out
    sid = out["result"]["stream"]

    out = hub.call("speech.hear", {"stream": sid, "audio": said("hello there")}, worker="rook")
    assert out["ok"], out
    r = out["result"]
    assert (r["heard"], r["speaker"], r["handoff"]) == ("hello there", {"label": "bake", "score": 0.9}, False), r
    assert spoken(r["say"]["audio"]) == r["say"]["text"] == "Hi Bake! (sounds like bake)"
    for cap in ("speech-engine.stt", "speech-engine.speaker", "speech-engine.turn", "speech-engine.tts"):
        entry = hub.get(f"/journal?cap={cap}&limit=1")["entries"][0]
        assert (entry["worker"], entry["person"], entry["ok"]) == (worker_id, "person:bake", True), entry

    out = hub.call("speech.hear", {"stream": sid, "audio": said("is the build green")}, worker="rook")
    assert out["result"]["handoff"] is True, out

    def reply():
        got = hub.call("speech.read", {"stream": sid, "wait": 2, "audio": True}, worker="rook")
        return next((e for e in got["result"]["events"]
                     if e["type"] == "turn" and e["data"].get("system") == "two"), None)
    e = until(reply, 30, "the agent's reply")
    assert e["data"]["text"] == "brain: (Spoken, in a voice session.) is the build green"
    assert spoken(e["audio"]) == e["data"]["text"]

    st = hub.call("speech.status", {}, worker="rook")["result"]
    assert st["engine"] == {"stt": True, "fake": True} and [s["id"] for s in st["sessions"]] == [sid]
