"""Long huddle messages read aloud in chunks.

The hub cuts a message into sentence-sized chunks the voice server reads
whole (huddle/speech.py :func:`parts`), hands the page the plan and one
chunk's audio at a time, and sends "Play on <device>" chunk by chunk; the
voice plugin asks its server for one line at a time. The page plays the
chunks in order with two loading ahead, retries a failed chunk once, skips
it after that, stops on Stop, and replays from what it kept
(rook/web/huddles.js :func:`playChunks`). The voice pickers read the voice
server's real /voices shape.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rook.hub import site as site_mod
from rook.hub.plugins.huddle import speech
from tests.test_huddle_chair import _client, hub  # noqa: F401 - fixture
from tests.test_huddle_speak import _HARNESS, FakeVoice, _qa_said, _with_voice
from tests.test_voice_plugin import clean_env, node_for, wait_for  # noqa: F401 - autouse fixture

ROOT = Path(__file__).resolve().parent.parent

SENTENCES = ("The build finished on every worker. Two tests were slow; both passed. "
             "The phone is on battery, so its jobs wait until it charges. Yes. "
             "Next we look at the deploy, which needs a fresh backup, a rollback that passes "
             "a dry run, the worker bundle and the app copied by hand, every setting kept "
             "as it is, the family devices left alone, the health and login checks, the jobs, "
             "the roles and the approvals looked at once more, before anything on the live "
             "hub changes. Done. ")


# -- the chunks ------------------------------------------------------------------------

def test_the_chunks_fit_what_the_voice_server_reads_whole():
    # Chatterbox stops at about 32 s of audio (~16 characters a second); the
    # server's own cap is 700 characters.
    assert speech.FIRST_PART < speech.PART <= 300 and speech.MAX_SPOKEN >= 8000
    text = speech.speakable(SENTENCES * 12)
    parts = speech.parts(text)
    assert len(text) > 3000 and len(parts) > 10
    assert len(parts[0]) <= speech.FIRST_PART
    assert all(len(p) <= speech.PART for p in parts)
    assert " ".join(parts) == text                      # nothing lost, nothing reordered
    words = set(text.split())
    for p in parts:                                     # never inside a word
        assert p.split()[0] in words and p.split()[-1] in words


def test_chunks_end_at_sentences_then_clauses_and_short_ones_ride_along():
    parts = speech.parts(SENTENCES)
    # Whole sentences where they fit: every chunk but a cut one ends a sentence.
    long = next(s for s in re.split(r"(?<=\.) ", SENTENCES) if len(s) > speech.PART - 20)
    assert all(p.endswith((".", ",", ";")) for p in parts)
    # The long sentence is cut after a clause, not between any two words.
    cut = [p for p in parts if not p.endswith(".")]
    assert cut and all(p.endswith((",", ";")) for p in cut) and long[:40] in " ".join(parts)
    # "Yes." and "Done." are never alone.
    assert "Yes." not in parts and "Done." not in parts
    assert all(len(p) >= speech.MIN_PART for p in parts)
    # A sentence with no clause to cut at is cut between words.
    plain = "word " * 200
    pieces = speech.parts(plain.strip(), limit=50, first=50)
    assert all(len(p) <= 50 and p.endswith("word") for p in pieces)
    # Only a word longer than a chunk is ever cut inside.
    blob = "x" * 120
    assert speech.parts(blob, limit=50, first=50) == ["x" * 50, "x" * 50, "x" * 20]
    assert speech.parts("") == [] and speech.parts("  Hi.  ") == ["Hi."]


def test_a_short_message_is_one_chunk():
    assert speech.parts("Deploy is done. All green.") == ["Deploy is done. All green."]


# -- the hub API: the plan, then one chunk at a time, and Play on --------------------

@pytest.mark.asyncio
async def test_the_page_gets_the_plan_then_each_chunk_in_the_roles_voice(hub, monkeypatch):  # noqa: F811
    voice = FakeVoice()
    _with_voice(hub, monkeypatch, voice)
    room, qa, _ = await _qa_said(hub, SENTENCES * 3)
    async with _client(hub) as c:
        async def speak(**body):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                           "room": room, "seq": qa["seq"], **body})
            assert r.status_code == 200, r.text
            return r.json()["result"]
        plan = await speak(plan=True)
        chunks = plan["chunks"]
        assert plan["mode"] == "hub" and plan["source"] == "role" and plan["voice"] == "nova"
        assert "audio" not in plan and voice.said == []          # a plan costs no speech
        assert plan["parts"] == len(chunks) > 3
        assert chunks == speech.parts(speech.speakable(SENTENCES * 3))
        for i in (0, 2, 1):
            r = await speak(part=i)
            assert r["part"] == i and r["parts"] == len(chunks)
            assert voice.said[-1] == (chunks[i], "nova")
            assert base64.b64decode(r["audio"]) == b"RIFF" + chunks[i].encode()[:8]


@pytest.mark.asyncio
async def test_play_on_a_device_sends_the_chunks_in_order(hub, monkeypatch):  # noqa: F811
    room, qa, _ = await _qa_said(hub, SENTENCES * 3)
    hub.band.workers["w-phone"] = {"name": "myphone", "caps": ["voice.speak"], "last_seen": time.time()}
    monkeypatch.setattr(hub.roles, "protected", lambda: ["Kid tablet"], raising=False)
    chunks = speech.parts(speech.speakable(SENTENCES * 3))
    async with _client(hub) as c:
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak_on", "room": room,
                                                      "seq": qa["seq"], "worker": "myphone"})
        assert r.status_code == 200 and r.json()["result"]["parts"] == len(chunks)

        def said():
            return [x for x in hub.band.calls if x.cap == "voice.speak"]
        assert await wait_for(lambda: len(said()) == len(chunks))
    calls = said()
    assert [x.args["text"] for x in calls] == chunks
    assert all(x.args == {"text": x.args["text"], "wait": False, "chat": False, "voice": "nova"}
               and x.target == "w-phone" for x in calls)


# -- the voice plugin asks for one line at a time -------------------------------------

# The voice server's /voices as it answers (2026-10): every voice in
# ``voices``, Kokoro listed first, each with engine, name and label in
# ``catalog``, and which engines are up.
REAL_SHAPE = {
    "voices": ["af_heart", "am_adam", "bf_emma", "default", "narrator", "narrator_slow"],
    "default": "narrator",
    "catalog": [
        {"id": "af_heart", "engine": "kokoro", "name": "af_heart", "label": "Heart (US female)"},
        {"id": "am_adam", "engine": "kokoro", "name": "am_adam", "label": "Adam (US male)"},
        {"id": "bf_emma", "engine": "kokoro", "name": "bf_emma", "label": "Emma (UK female)"},
        {"id": "default", "engine": "chatterbox", "name": "default", "label": "Chatterbox (default)"},
        {"id": "narrator", "engine": "chatterbox", "name": "narrator", "label": "Narrator"},
        {"id": "narrator_slow", "engine": "chatterbox", "name": "narrator_slow",
         "label": "Narrator Slow"}],
    "engines": {"kokoro": {"available": True}, "chatterbox": {"available": True}},
}


def one_line_server(seen: list):
    """A voice server that, like the real one, reads one line at a time and
    answers 503 busy to a second."""
    slot = asyncio.Lock()

    async def voices(request):
        return web.json_response(REAL_SHAPE)

    async def speak(request):
        body = await request.json()
        if slot.locked():
            return web.json_response({"detail": "Voice engine is busy."}, status=503)
        async with slot:
            await asyncio.sleep(0.05)
            seen.append(body["text"])
            return web.Response(body=b"RIFF" + body["text"].encode(), content_type="audio/wav")

    app = web.Application()
    app.router.add_get("/voices", voices)
    app.router.add_post("/api/voice", speak)
    return TestServer(app)


@pytest.mark.asyncio
async def test_the_hub_reads_the_real_voice_list_and_sends_one_line_at_a_time(tmp_path, monkeypatch):
    from rook.hub.plugins.voice import engine as eng
    monkeypatch.setattr(site_mod, "_SERVED", False)
    monkeypatch.setattr(eng, "BUSY_RETRIES", 0)         # a busy answer would fail the chunk
    seen: list = []
    server = one_line_server(seen)
    await server.start_server()
    node = node_for(tmp_path, engine="remote", remote_url=str(server.make_url("")).rstrip("/"),
                    speech_cache_mb=1.0)
    await node.start()
    try:
        voice = node.plugin("voice")
        assert voice is not None, node.host.status["voice"]
        v = await voice.voices()
        # Chatterbox first (the natural voices), then Kokoro; the server's default.
        assert v["voices"] == ["default", "narrator", "narrator_slow", "af_heart", "am_adam", "bf_emma"]
        assert [c["engine"] for c in v["catalog"]] == ["chatterbox"] * 3 + ["kokoro"] * 3
        assert v["catalog"][1] == {"id": "narrator", "label": "Narrator", "engine": "chatterbox"}
        assert v["default"] == v["engine_default"] == "narrator"
        # Three chunks at once and one of them twice: all spoken, each once.
        lines = ["One.", "Two.", "Three.", "Two."]
        out = await asyncio.gather(*(voice.synthesize(t, "narrator") for t in lines))
        assert out == [b"RIFF" + t.encode() for t in lines]
        assert sorted(seen) == ["One.", "Three.", "Two."]
    finally:
        await node.stop()
        await server.close()


# -- the page: in order, two ahead, retry once, skip, stop, replay from memory --------

_CHUNKS = r"""
const tick=(ms=5)=>new Promise(r=>setTimeout(r,ms));
const out={};
// load(i) answers in reverse speed (later chunks faster); played order must hold.
async function run({count,fail={},stopAt=-1,ahead=2}){
  const log={loads:[],played:[],skipped:[],busy:0,max:0,stopped:false};
  const tries={};
  const p=playChunks({count,ahead,stopped:()=>log.stopped,
    load:async i=>{tries[i]=(tries[i]||0)+1;log.loads.push(i);log.busy++;log.max=Math.max(log.max,log.busy);
      await tick(2*(count-i));log.busy--;
      if((fail[i]||0)>=tries[i])throw Error('chunk '+i+' failed');return 'clip'+i;},
    play:async(c,i)=>{log.played.push(c);if(i===stopAt)log.stopped=true;
      return {started:Promise.resolve(),ended:tick(1)};},
    onSkip:(i,e)=>log.skipped.push([i,e.message])});
  try{await p;log.result='ok';}catch(e){log.result=e.message;}
  log.tries=tries;return log;
}
out.order=await run({count:6});
out.retry=await run({count:4,fail:{1:1}});
out.skip=await run({count:5,fail:{2:2}});
out.twice=await run({count:5,fail:{1:2,2:2}});
out.stop=await run({count:8,stopAt:1});
console.log(JSON.stringify(out));
"""


def _module(tmp_path, harness=_HARNESS):
    src = (ROOT / "rook/web/huddles.js").read_text(encoding="utf-8")
    first = src.index("\n", src.index("const {h,el,more,copyChip}"))
    body = src[first:].replace("new URL(import.meta.url).search", "''")
    (tmp_path / "hx.mjs").write_text("const search='';" + harness + body.replace(
        "const search=new URL(import.meta.url).search;", ""), encoding="utf-8")


def _node(tmp_path, imports: str, scenario: str) -> dict:
    (tmp_path / "t.mjs").write_text(f"import {{{imports}}} from './hx.mjs';\n" + scenario,
                                    encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_chunks_play_in_order_two_ahead_retry_once_skip_and_stop(tmp_path):
    _module(tmp_path)
    out = _node(tmp_path, "playChunks", _CHUNKS)
    o = out["order"]
    assert o["played"] == [f"clip{i}" for i in range(6)] and o["result"] == "ok"
    assert o["max"] <= 2 and sorted(o["loads"]) == list(range(6))
    r = out["retry"]                                  # failed once: tried again, played
    assert r["played"] == ["clip0", "clip1", "clip2", "clip3"] and r["tries"]["1"] == 2
    assert r["skipped"] == []
    s = out["skip"]                                   # failed twice: skipped with a note
    assert s["played"] == ["clip0", "clip1", "clip3", "clip4"] and s["tries"]["2"] == 2
    assert s["skipped"] == [[2, "chunk 2 failed"]] and s["result"] == "ok"
    t = out["twice"]                                  # two in a row: the engine is gone
    assert t["played"] == ["clip0"] and t["result"] == "chunk 2 failed"
    st = out["stop"]                                  # Stop: nothing more played or asked for
    assert st["played"] == ["clip0", "clip1"] and st["result"] == "ok"
    assert max(st["loads"]) <= 3


_REPLAY = r"""
const tick=()=>new Promise(r=>setTimeout(r,5));
const ev={preventDefault(){},stopPropagation(){}};
const plan={mode:'hub',source:'default',voice:'',label:'narrator',parts:3,part:0,
            chunks:['One is first.','Two is next.','Three is last.']};
const clip=i=>({mode:'hub',source:'default',voice:'',label:'narrator',parts:3,part:i,audio:btoa('RIFF'+i),mime:'audio/wav'});
const out={};
globalThis.replies=[plan,clip(0),clip(1),clip(2)];
const a=speakButton('r1',{seq:7});
a.onclick(ev);await tick();
out.first=posts.map(p=>p.plan?'plan':p.part);
out.title=a.title;
for(let i=0;i<5&&a.getAttribute('aria-pressed')==='true';i++){audios.at(-1).onended();await tick();}
out.played=audios.length;out.done=a.getAttribute('aria-pressed');
posts.length=0;globalThis.replies=[plan];
a.onclick(ev);await tick();
for(let i=0;i<5&&a.getAttribute('aria-pressed')==='true';i++){audios.at(-1).onended();await tick();}
out.replay=posts.map(p=>p.plan?'plan':p.part);out.replayed=audios.length;
// Stop while loading: the pending request is aborted.
posts.length=0;const signals=[];const real=globalThis.fetch;
globalThis.fetch=async(url,opt)=>{if(opt&&opt.signal)signals.push(opt.signal);return real(url,opt);};
globalThis.replies=[{...plan,chunks:['Four is new.','Five is new.'],parts:2},clip(0),clip(1)];
const b=speakButton('r1',{seq:8});b.onclick(ev);await tick();b.onclick(ev);await tick();
out.aborted=signals.length>0&&signals.every(s=>s.aborted);out.stopped=b.getAttribute('aria-pressed');
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_speak_button_asks_for_the_plan_plays_every_chunk_and_replays_from_memory(tmp_path):
    _module(tmp_path)
    out = _node(tmp_path, "speakButton", _REPLAY)
    assert out["first"] == ["plan", 0, 1, 2]         # the plan, then two ahead of the one playing
    assert out["title"] == "Stop (part 1 of 3)"
    assert out["played"] == 3 and out["done"] == "false"
    assert out["replay"] == ["plan"] and out["replayed"] == 6   # every chunk from memory
    assert out["aborted"] is True and out["stopped"] == "false"


_EMPTY_LIST = r"""
const tick=()=>new Promise(r=>setTimeout(r,5));
const out={};
globalThis.replies=[{available:true,voices:[],catalog:[],default:'',error:"'utf-8' codec can't decode byte 0x8b"}];
const p=voicePicker('');await tick();
out.note=p.note.textContent;
globalThis.replies=[{available:true,voices:['narrator'],catalog:[{id:'narrator',label:'Narrator',engine:'chatterbox'}],default:'narrator'}];
const q=voicePicker('narrator');await tick();
out.asked=posts.filter(x=>x.action==='voices').length;
out.options=q.select.children.length;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_picker_that_got_no_voices_says_why_and_the_next_one_asks_again(tmp_path):
    harness = _HARNESS.replace("append(...k){this.children.push(...k);}",
                               "append(...k){this.children.push(...k);} "
                               "replaceChildren(...k){this.children=k;}")
    _module(tmp_path, harness)
    out = _node(tmp_path, "voicePicker", _EMPTY_LIST)
    assert out["note"].startswith("The voice engine did not list its voices") and "0x8b" in out["note"]
    assert out["asked"] == 2 and out["options"] == 2  # band default + the Chatterbox group
