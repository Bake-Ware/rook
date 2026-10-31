"""The speech plugin on a real 0.5 hub (rook.core2.hub.runtime) through the
HTTP door, with a fake speech engine, a fake System One (llm.complete) and
the agents plugin over a fake backend in the library: a voice session is a
stream; an utterance is heard, its speaker kept on the session; System One
answers greetings itself and hands the rest to the agent, whose reply lands
in the stream with its audio; a pause waits for the rest of the sentence."""

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import CONTRACTS, PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.manifest import load  # noqa: E402
from speech_fakes import BRAIN, BRAIN_PY, ENGINE, ENGINE_PY, TALKER, TALKER_PY, plugin, said  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the speech plugin on the hub."


async def http(hub, path, body=None, token=None):
    def go():
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{hub.doors[0].port}{path}", data=data,
                                     method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    return await asyncio.to_thread(go)


async def call(hub, token, cap, **args):
    return await http(hub, "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY}, token)


def audio_text(f):
    return base64.b64decode(f["data"]).decode()


def library(tmp_path, *, talker=True):
    lib = tmp_path / "lib"
    plugin(lib, "speech-engine", ENGINE, "engine.py", ENGINE_PY)
    plugin(lib, "agents-local", BRAIN, "brain.py", BRAIN_PY)
    if talker:
        plugin(lib, "llm-openai", TALKER, "talker.py", TALKER_PY)
    for name in ("speech", "agents"):
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return lib


def hub_config(tmp_path, lib, **settings):
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "sam": {"preset": "adult"},
                         "guest": {"preset": "guest", "created": time.time()}},
                  settings={"agents": {"warm": "5s"}, **settings}, doors={"http": {"bind": "127.0.0.1:0"}})


def run(cfg, body, timeout=40):
    async def main():
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            await body(hub, (hub.data / "owner.token").read_text().strip())
        finally:
            await hub.stop()
    asyncio.run(asyncio.wait_for(main(), timeout))


async def reply_from(hub, token, stream, system, after=0, audio=False):
    """Read the session until a reply by ``system`` arrives."""
    for _ in range(100):
        out = await call(hub, token, "speech.read", stream=stream, after=after, wait=1, audio=audio)
        assert out["ok"], out
        for e in out["result"]["events"]:
            if e["type"] == "turn" and e["data"].get("system") == system:
                return e, out["result"]
            assert e["data"].get("state") != "error", e
        after = out["result"]["next"]
    raise AssertionError(f"no reply from System {system}")


def test_manifests_are_clean():
    for name in ("speech", "speech-engine", "speech-elevenlabs"):
        m = load(PLUGINS / name, CONTRACTS)
        assert [str(w) for w in m.warnings] == [], name
    engine = load(PLUGINS / "speech-engine", CONTRACTS).data
    assert engine["builtin"] and all("run" not in c for c in engine["caps"].values())
    assert set(engine["provides"]) == {"voice.stt", "voice.tts", "voice.turn", "voice.speaker"}


def test_a_voice_session(tmp_path):
    cfg = hub_config(tmp_path, library(tmp_path))

    async def body(hub, owner):
        sam = hub.add_token(Caller(person="person:sam"), label="sam")
        guest = hub.add_token(Caller(person="person:guest"), label="guest")
        assert {"speech", "speech-engine", "agents", "llm-openai"} <= set(hub.plugins.plugins)
        out = await call(hub, owner, "speech.open", device="bakephone")
        assert out["ok"] and out["result"]["agent"] == "house", out
        sid = out["result"]["stream"]

        # A greeting: System One answers at once, with its audio, and the
        # speaker ID lands on the session and reaches System One's prompt.
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("hello there"))
        assert out["ok"], out
        r = out["result"]
        assert (r["heard"], r["complete"], r["handoff"]) == ("hello there", True, False), r
        assert r["speaker"] == {"label": "bake", "score": 0.81}
        assert r["say"]["text"] == "Hi Bake! (sounds like bake)"
        assert r["say"]["audio"]["mime"] == "audio/wav"
        assert audio_text(r["say"]["audio"]) == "RIFF:default:Hi Bake! (sounds like bake)"
        stt = hub.journal.query(cap="speech-engine.stt", limit=1)[0]
        assert (stt["person"], stt["ok"]) == ("person:bake", True)      # routed as the speaker

        # A request: System One acknowledges, the agent answers in the
        # background as the person, in their own session with it.
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("what is on my calendar"))
        r = out["result"]
        assert (r["say"]["text"], r["handoff"]) == ("Let me check.", True), r
        e, res = await reply_from(hub, owner, sid, "two", audio=True)
        assert e["data"]["text"] == "brain: (Spoken, in a voice session.) what is on my calendar"
        assert e["by"] == "agent:house"
        assert audio_text(e["audio"]).endswith(e["data"]["text"])
        assert res["attrs"]["speaker"] == {"label": "bake", "score": 0.81}
        assert res["attrs"]["device"] == "bakephone"
        turn = hub.journal.query(cap="agents-local.turn", limit=1)[0]
        assert turn["person"] == "person:bake"
        chat = await call(hub, owner, "agents.chat.read", agent="house")
        assert "what is on my calendar" in json.dumps(chat["result"])     # one session with the agent

        # A pause: voice.turn says the person isn't done; the next part completes it.
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("remind me to..."))
        assert out["result"]["complete"] is False and out["result"]["say"] is None, out
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("call mum"))
        assert out["result"]["heard"] == "remind me to call mum", out
        # Push-to-talk released: no end-of-turn check.
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("so..."), final=True)
        assert out["result"]["complete"] is True and out["result"]["heard"] == "so", out

        # Typed text works too; an answer System One can't read goes to the agent.
        out = await call(hub, owner, "speech.hear", stream=sid, text="mumble")
        assert out["result"]["handoff"] is True and out["result"]["say"] is None, out
        assert not (await call(hub, owner, "speech.hear", stream=sid))["ok"]

        # Someone else's session isn't there for another person; core's
        # guest preset grants no speech at all.
        out = await call(hub, sam, "speech.read", stream=sid)
        assert out["ok"] is False and "no voice session" in out["error"], out
        assert (await call(hub, guest, "speech.open"))["error"] == "denied: person: grants"

        out = await call(hub, owner, "speech.status")
        assert [s["id"] for s in out["result"]["sessions"]] == [sid] and out["result"]["talker"] is True
        assert out["result"]["engine"] == {"stt": True, "turn": True}
        out = await call(hub, owner, "speech.voices")
        assert out["result"]["voices"]["speech-engine"]["default"] == "af_heart"
        assert "speech-elevenlabs" in out["result"]["errors"]                 # not installed

        assert (await call(hub, owner, "speech.close", stream=sid))["ok"]
        out = await call(hub, owner, "speech.hear", stream=sid, text="hello")
        assert not out["ok"] and "closed" in out["error"], out
        assert (await call(hub, owner, "speech.read", stream=sid))["ok"]      # history stays
    run(cfg, body)


def test_without_system_one_every_turn_goes_to_the_agent(tmp_path):
    """No llm.complete provider (or talker off): the agent hears everything,
    and a voice whose provider fails speaks in the engine's default."""
    cfg = hub_config(tmp_path, library(tmp_path, talker=False))

    async def body(hub, owner):
        sid = (await call(hub, owner, "speech.open", voice="broken"))["result"]["stream"]
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("hello"))
        assert out["result"]["handoff"] is True and out["result"]["say"] is None, out
        e, _ = await reply_from(hub, owner, sid, "two", audio=True)
        assert audio_text(e["audio"]) == "RIFF:default:" + e["data"]["text"]       # the fallback voice
    run(cfg, body)

    cfg = hub_config(tmp_path / "off", library(tmp_path / "off"), speech={"talker": False})

    async def off(hub, owner):
        sid = (await call(hub, owner, "speech.open"))["result"]["stream"]
        out = await call(hub, owner, "speech.hear", stream=sid, audio=said("hello"))
        assert out["result"]["handoff"] is True and out["result"]["say"] is None, out
        assert not hub.journal.query(cap="llm-openai.complete")
    run(cfg, off)


def test_parse_plan():
    import importlib.util
    spec = importlib.util.spec_from_file_location("speech_script", PLUGINS / "speech" / "scripts" / "speech.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.parse_plan('{"say": "Hi.", "handoff": ""}') == ("Hi.", "")
    assert mod.parse_plan('Sure:\n```json\n{"say": "", "handoff": "book it"}\n```') == ("", "book it")
    for bad in ("plain words", '{"say": 3}', '{"say": "", "handoff": ""}', "{nope}"):
        assert mod.parse_plan(bad) is None, bad
