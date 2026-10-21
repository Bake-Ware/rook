"""Reading huddle messages aloud (rook/hub/plugins/huddle/speech.py and the
Speak button in rook/web/huddles.js): the text a voice says, which voice says
it (the role's > the band's default > the reader's browser), the dashboard
API that returns the audio, playing on a device, and the button itself."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rook.hub.plugins.huddle import speech
from rook.hub.plugins.roles import model
from tests.test_huddle_chair import _call, _client, _roles, _settle, hub  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[1]


# -- the text ----------------------------------------------------------------------

def test_markdown_code_and_links_become_plain_sentences():
    text = ("## Plan\n"
            "**Ship it** after _one_ more check, see [the PR](https://github.com/o/r/pull/9).\n"
            "- run `rook-pytest -q`\n"
            "- read https://example.com/docs?a=1 first\n"
            "```bash\nrm -rf /tmp/x\n```\n"
            "> quoted line\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
            "![diagram](https://x/y.png) and <b>bold</b> and [[huddles-live-rollback]]")
    said = speech.speakable(text)
    assert "**" not in said and "`" not in said and "#" not in said and "http" not in said
    assert "rm -rf" not in said and "(code left out)" in said
    assert said.startswith("Plan. Ship it after one more check, see the PR.")
    assert "run rook-pytest -q." in said and "read first." in said
    assert "quoted line." in said and "diagram and bold and huddles live rollback" in said
    assert "|" not in said and "---" not in said


def test_ids_are_said_by_their_three_word_names_or_left_out():
    named = {"t_7b47df858bfc": "brave-otter-lamp", "e5ab864d4ae84869": "husky-stairwell-hive"}
    said = speech.speakable("Task t_7b47df858bfc is in room e5ab864d4ae84869; commit 9b48092 "
                            "and k_3096587d7f5c443fa8eb450fda8c1242 too. @role:qa asked agent:home.",
                            named.get)
    assert said == ("Task brave otter lamp is in room husky stairwell hive; commit and too. "
                    "qa asked home.")
    # Words that only look like hex (no digit), and a failing lookup, are fine.
    assert speech.speakable("added a facade to my_facade", lambda _: 1 / 0) == \
        "added a facade to my_facade."


def test_long_text_is_cut_and_split_for_the_engine():
    long = " ".join(f"Sentence number {i} is here." for i in range(400))
    said = speech.speakable(long)
    assert len(said) <= speech.MAX_SPOKEN + 20 and said.endswith("The rest is cut.")
    parts = speech.parts(said)
    assert all(len(p) <= speech.PART for p in parts) and len(parts) > 5
    assert all(p.endswith(".") for p in parts[:-1])
    assert " ".join(parts) == said
    assert speech.parts("") == [] and speech.speakable("```\nonly code\n```") == "(code left out)."


# -- the voice ---------------------------------------------------------------------

def test_role_voice_then_band_default_then_the_browser():
    voices = ["af_heart", "sojourn"]
    assert speech.choose_voice("sojourn", voices=voices, default="af_heart") == {
        "mode": "hub", "voice": "sojourn", "source": "role", "label": "sojourn"}
    assert speech.choose_voice("chatterbox:sojourn", voices=voices)["source"] == "role"
    assert speech.choose_voice("", voices=voices, default="af_heart") == {
        "mode": "hub", "voice": "", "source": "default", "label": "af_heart"}
    gone = speech.choose_voice("alan", voices=voices, default="af_heart")
    assert gone["voice"] == "" and gone["source"] == "default" and "alan" in gone["reason"]
    # An engine that cannot list its voices still gets the role's voice.
    assert speech.choose_voice("alan", voices=None)["voice"] == "alan"
    off = speech.choose_voice("sojourn", unavailable="voice is off on this hub", voices=voices)
    assert off == {"mode": "browser", "voice": "", "source": "browser",
                   "reason": "voice is off on this hub"}


def test_a_role_may_name_its_voice():
    base = {"id": "qa", "name": "QA"}
    assert "voice" not in model.validate(base)
    assert "voice" not in model.validate({**base, "voice": "  "})
    assert model.validate({**base, "voice": "kokoro:af_heart"})["voice"] == "kokoro:af_heart"
    for bad in ("two words", "x" * 65, 3, "a/b"):
        with pytest.raises(model.RoleError, match="voice"):
            model.validate({**base, "voice": bad})


# -- the dashboard API ---------------------------------------------------------------

class FakeVoice:
    """The voice plugin as the huddle API sees it."""

    def __init__(self, why: str = "", broken: tuple = ()):
        self.why, self.broken, self.said = why, broken, []

    def speech_unavailable(self):
        return self.why

    async def voices(self):
        return {"voices": ["af_heart", "sojourn"], "default": "af_heart",
                "catalog": [{"id": "af_heart", "label": "Heart", "engine": "kokoro"},
                            {"id": "sojourn", "label": "Sojourn", "engine": "chatterbox"}]}

    async def synthesize(self, text, voice=""):
        if voice in self.broken:
            raise RuntimeError("chatterbox is down")
        self.said.append((text, voice))
        return b"RIFF" + text.encode()[:8]


def _with_voice(hub, monkeypatch, voice):  # noqa: F811
    real = hub.node.plugin
    monkeypatch.setattr(hub.node, "plugin", lambda ns: voice if ns == "voice" else real(ns))


async def _qa_said(hub, text):  # noqa: F811
    await _roles(hub, voice="sojourn")
    started = await _call(hub, "huddle.start", {"title": "Speak", "roles": ["qa"]})
    room = started["room"]
    hub.band.script = {"huddle-qa": [{"text": text}]}
    await _call(hub, "huddle.post", {"huddle": room, "text": "@qa status?"})
    await _settle(hub)
    msgs = [m for m in hub.svc.chat.tail(room, 50)]
    qa = next(m for m in msgs if m["sender"] == "role:qa")
    chair = [m for m in msgs if m["sender"] == "agent:home"]
    return room, qa, chair


@pytest.mark.asyncio
async def test_speak_reads_a_role_message_in_the_role_voice(hub, monkeypatch):  # noqa: F811
    voice = FakeVoice()
    _with_voice(hub, monkeypatch, voice)
    room, qa, chair = await _qa_said(hub, "**Done**: see https://x.y/z.")
    hub.svc.chat.send(room, "role:qa", f"Room {room} is **green**.", [], False)
    seq = hub.svc.chat.tail(room, 1)[0]["seq"]
    name = hub.svc.store.name("room", room)
    async with _client(hub) as c:
        async def api(**body):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", **body})
            return r.status_code, r.json()
        code, body = await api(action="speak", room=room, seq=seq)
        r = body["result"]
        assert code == 200 and r["mode"] == "hub" and r["source"] == "role" and r["voice"] == "sojourn"
        assert r["role"] == "qa" and r["parts"] == 1 and r["mime"] == "audio/wav"
        assert base64.b64decode(r["audio"]).startswith(b"RIFF")
        assert voice.said[-1] == (f"Room {name.replace('-', ' ')} is green.", "sojourn")
        # The chair has no role voice: the band's default (the engine's own).
        if chair:
            code, body = await api(action="speak", room=room, seq=chair[-1]["seq"])
            assert body["result"]["source"] == "default" and voice.said[-1][1] == ""
        code, body = await api(action="speak", room=room, seq=seq, part=3)
        assert code == 400
        code, body = await api(action="speak", room=room, seq=99999)
        assert code == 400 and "no message" in body["error"]
        code, body = await api(action="voices")
        assert body["result"]["available"] and body["result"]["default"] == "af_heart"
        assert [v["id"] for v in body["result"]["catalog"]] == ["af_heart", "sojourn"]


@pytest.mark.asyncio
async def test_a_failing_role_voice_falls_back_to_the_default_then_the_browser(hub, monkeypatch):  # noqa: F811
    voice = FakeVoice(broken=("sojourn",))
    _with_voice(hub, monkeypatch, voice)
    room, qa, _ = await _qa_said(hub, "All checks pass.")
    async with _client(hub) as c:
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                       "room": room, "seq": qa["seq"]})).json()["result"]
        assert r["mode"] == "hub" and r["source"] == "default" and "chatterbox is down" in r["reason"]
        assert voice.said == [("All checks pass.", "")]
        voice.broken = ("sojourn", "")
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                       "room": room, "seq": qa["seq"]})).json()["result"]
        assert r["mode"] == "browser" and r["text"] == "All checks pass." and "audio" not in r


@pytest.mark.asyncio
async def test_without_a_voice_engine_the_browser_reads_it(hub, monkeypatch):  # noqa: F811
    _with_voice(hub, monkeypatch, None)
    room, qa, _ = await _qa_said(hub, "Use `t_7b47df858bfc` now.")
    async with _client(hub) as c:
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                       "room": room, "seq": qa["seq"]})).json()["result"]
        assert r == {"mode": "browser", "voice": "", "source": "browser", "role": "qa",
                     "reason": "this hub has no voice engine (the voice plugin is not running)",
                     "parts": 1, "part": 0, "text": "Use now."}
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "voices"})).json()
        assert r["result"]["available"] is False and r["result"]["voices"] == []
    _with_voice(hub, monkeypatch, FakeVoice(why="the voice engine is starting"))
    async with _client(hub) as c:
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                       "room": room, "seq": qa["seq"]})).json()["result"]
        assert r["mode"] == "browser" and r["reason"] == "the voice engine is starting"


@pytest.mark.asyncio
async def test_a_card_is_read_in_its_proposers_voice(hub, monkeypatch):  # noqa: F811
    voice = FakeVoice()
    _with_voice(hub, monkeypatch, voice)
    await _roles(hub, voice="sojourn")
    room = (await _call(hub, "huddle.start", {"title": "Cards", "roles": ["qa"]}))["room"]
    hub.band.script = {"huddle-qa": [{"text": "Card.", "tools": [("propose", {
        "kind": "task.create", "summary": "Ping **box** before the deploy", "worker": "box",
        "cap": "info.ping"})]}]}
    await _call(hub, "huddle.post", {"huddle": room, "text": "@qa plan?"})
    await _settle(hub)
    card = hub.svc.store.cards(room)[0]
    async with _client(hub) as c:
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                       "room": room, "card": card["id"]})).json()
        assert r["result"]["source"] == "role" and voice.said[-1] == (
            "Ping box before the deploy.", "sojourn")
        other = (await _call(hub, "huddle.start", {"title": "Other", "roles": []}))["room"]
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                      "room": other, "card": card["id"]})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_play_on_a_device_never_offers_protected_ones(hub, monkeypatch):  # noqa: F811
    room, qa, _ = await _qa_said(hub, "Deploy is **done**.")
    now = time.time()
    hub.band.workers.update({
        "w-phone": {"name": "myphone", "caps": ["voice.speak"], "last_seen": now},
        "w-kid": {"name": "Kid tablet", "caps": ["voice.speak"], "last_seen": now}})

    async def devices(c):
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak_devices"})
        return [d["worker"] for d in r.json()["result"]["devices"]]
    async with _client(hub) as c:
        # No protected list: the hub cannot tell family devices apart, so none.
        assert await devices(c) == []
        monkeypatch.setattr(hub.roles, "protected", lambda: ["Kid tablet"], raising=False)
        assert await devices(c) == ["myphone"]
        hub.node.settings.set("huddle.speak_devices", ["Kid tablet", "myphone"], actor="human:op")
        assert await devices(c) == ["myphone"]
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak_on", "room": room,
                                                      "seq": qa["seq"], "worker": "Kid tablet"})
        assert r.status_code == 403
        assert not [x for x in hub.band.calls if x.cap == "voice.speak"]
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak_on", "room": room,
                                                      "seq": qa["seq"], "worker": "myphone"})
        assert r.status_code == 200 and r.json()["result"]["worker"] == "myphone"
        call = [x for x in hub.band.calls if x.cap == "voice.speak"][-1]
        assert call.target == "w-phone" and call.identity == "human:op"
        assert call.args == {"text": "Deploy is done.", "wait": False, "voice": "sojourn"}


# -- the button ----------------------------------------------------------------------

def test_every_bubble_and_card_gets_a_compact_speak_button():
    hx = (ROOT / "rook/web/huddles.js").read_text(encoding="utf-8")
    hd = (ROOT / "rook/web/huddle.js").read_text(encoding="utf-8")
    css = (ROOT / "rook/web/huddle.css").read_text(encoding="utf-8")
    assert "speakButton(cur.room,{seq:m.seq})" in hx and "speakButton(cur.room,{card:c.id})" in hx
    assert "speakButton(cur.room,{seq:m.seq})" in hd and "stopSpeaking()" in hd
    phone = css[css.rindex("@media(max-width:760px)"):]
    assert ".hx-speak{min-width:44px;min-height:44px" in phone
    roles = (ROOT / "rook/web/roles.js").read_text(encoding="utf-8")
    assert "hapi('voices')" in roles and "voice:voice.value" in roles and "Band default" in roles


_HARNESS = r"""
class El{constructor(t){this.tag=t;this.attrs={};this.children=[];this.dataset={};this.ls={};this.textContent='';this.title='';
  const s=new Set();this.classList={toggle:(c,on)=>on?s.add(c):s.delete(c),contains:c=>s.has(c)};}
  setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k];}
  addEventListener(e,f){(this.ls[e]=this.ls[e]||[]).push(f);} append(...k){this.children.push(...k);}
  remove(){} focus(){} getBoundingClientRect(){return {bottom:0,left:0};} querySelector(){return null;}}
const h=(tag,props,...kids)=>{const e=new El(tag);for(const [k,v] of Object.entries(props||{})){
  if(k==='text')e.textContent=v;else if(k==='class')e.className=v;else if(k.startsWith('on'))e[k]=v;else e.setAttribute(k,v);}
  e.append(...kids.flat());return e;};
const el=h,more=0,copyChip=0;
globalThis.document={querySelector:()=>null,head:{append(){}},body:{append(){}},addEventListener(){},removeEventListener(){}};
globalThis.window=globalThis;globalThis.innerHeight=800;globalThis.innerWidth=400;
globalThis.posts=[];globalThis.replies=[];
globalThis.fetch=async(url,opt)=>{if(!opt)return {ok:true,status:200,json:async()=>({csrf:'tok'})};
  const body=JSON.parse(opt.body);posts.push(body);const r=globalThis.replies.shift()||{mode:'hub',parts:1,part:body.part||0,audio:btoa('RIFF'),mime:'audio/wav',source:'role',voice:'sojourn'};
  return {ok:true,status:200,json:async()=>({ok:true,result:r})};};
globalThis.audios=[];
globalThis.Audio=class{constructor(u){this.src=u;this.paused=false;audios.push(this);} play(){return Promise.resolve();} pause(){this.paused=true;}};
globalThis.URL.createObjectURL=()=>'blob:x';globalThis.URL.revokeObjectURL=()=>{};
globalThis.spoken=[];
globalThis.SpeechSynthesisUtterance=class{constructor(t){this.text=t;}};
globalThis.speechSynthesis={speak(u){spoken.push(u);setTimeout(()=>u.onend&&u.onend());},cancel(){}};
"""

_SCENARIO = r"""
const tick=()=>new Promise(r=>setTimeout(r,5));
const ev={preventDefault(){},stopPropagation(){}};
const out={};
const a=speakButton('r1',{seq:5}),b=speakButton('r1',{seq:6});
out.label=a.getAttribute('aria-label');out.cls=a.className;out.idle=a.textContent;
a.onclick(ev);await tick();
out.post=posts[0];out.playing=[a.getAttribute('aria-pressed'),a.classList.contains('on'),a.textContent,a.title];
b.onclick(ev);await tick();
out.one_at_a_time=[audios[0].paused,a.getAttribute('aria-pressed'),b.getAttribute('aria-pressed')];
b.onclick(ev);await tick();
out.stopped=[audios[1].paused,b.getAttribute('aria-pressed'),b.textContent];
globalThis.replies=[{mode:'hub',parts:2,part:0,audio:btoa('RIFF'),mime:'audio/wav',source:'default',voice:'',label:'af_heart'},
         {mode:'hub',parts:2,part:1,audio:btoa('RIFF'),mime:'audio/wav',source:'default',voice:'',label:'af_heart'}];
a.onclick(ev);await tick();
out.second_part=posts.at(-1).part;audios.at(-1).onended();await tick();
out.next_clip=audios.length;audios.at(-1).onended();await tick();
out.done=[a.getAttribute('aria-pressed'),a.title];
globalThis.replies=[{mode:'browser',source:'browser',voice:'',reason:'this hub has no voice engine',parts:1,part:0,text:'Hello there. All good.'}];
const c=speakButton('r1',{card:'c_1'});c.onclick(ev);await tick();await tick();
out.browser=[spoken.map(u=>u.text),c.title,posts.at(-1).card];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_speak_button_plays_stops_and_falls_back(tmp_path):
    src = (ROOT / "rook/web/huddles.js").read_text(encoding="utf-8")
    first = src.index("\n", src.index("const {h,el,more,copyChip}"))   # the manage.js import
    body = src[first:].replace("new URL(import.meta.url).search", "''")
    (tmp_path / "hx.mjs").write_text("const search='';" + _HARNESS + body.replace(
        "const search=new URL(import.meta.url).search;", ""), encoding="utf-8")
    (tmp_path / "t.mjs").write_text("import {speakButton} from './hx.mjs';\n" + _SCENARIO,
                                    encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["label"] == "Speak" and out["cls"] == "hx-speak" and out["idle"] == "▶"
    assert out["post"] == {"csrf": "tok", "action": "speak", "room": "r1", "seq": 5, "part": 0}
    assert out["playing"] == ["true", True, "■", "Stop"]
    assert out["one_at_a_time"] == [True, "false", "true"]
    assert out["stopped"] == [True, "false", "▶"]
    assert out["second_part"] == 1 and out["next_clip"] == 4
    assert out["done"] == ["false", "Speak (the band's voice, af_heart)"]
    spoken, title, card = out["browser"]
    assert spoken == ["Hello there. All good."] and card == "c_1"
    assert title.startswith("Spoken by this browser: this hub has no voice engine.")


# -- the voice engines ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_remote_engine_speaks_with_the_hubs_voice_key():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from rook.hub.plugins.voice import engine as eng
    seen = []

    async def voices(request):
        return web.json_response({"voices": ["af_heart", "sojourn"], "default": "sojourn"})

    async def speak(request):
        body = await request.json()
        seen.append((request.headers.get("Authorization"), body))
        if body.get("voice") == "nope":
            return web.json_response({"detail": "Unknown voice. Inspect /voices."}, status=400)
        return web.Response(body=eng.wav(b"\0\0" * 80, 16000), content_type="audio/wav")
    app = web.Application()
    app.router.add_get("/voices", voices)
    app.router.add_post("/api/voice", speak)
    server = TestServer(app)
    await server.start_server()
    remote = eng.RemoteEngine(str(server.make_url("")).rstrip("/"), "/voice", token="k")
    try:
        assert (await remote.voices())["default"] == "sojourn"
        audio = await remote.synthesize("  Hello\nthere. ", "sojourn")
        assert audio[:4] == b"RIFF" and seen[-1] == ("Bearer k", {"text": "Hello there.", "voice": "sojourn"})
        await remote.synthesize("Default voice.")
        assert seen[-1][1] == {"text": "Default voice."}
        with pytest.raises(RuntimeError, match="HTTP 400: Unknown voice"):
            await remote.synthesize("x", "nope")
        with pytest.raises(ValueError):
            await remote.synthesize("x" * 701)
    finally:
        await remote.stop()
        await server.close()


@pytest.mark.asyncio
async def test_engine_here_speaks_in_process(tmp_path, monkeypatch):
    import sys
    import wave
    import io
    from rook.hub.plugins.voice import Voice
    from rook.hub.plugins.voice import engine as eng
    pkg = tmp_path / "pkgs" / "fakespeech"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "tts.py").write_text("def resolve_voice(p, v):\n    return v if v in p.voices else None\n",
                                encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))

    class Provider:
        voices, default_voice = ["af_heart", "sojourn"], "af_heart"

        async def synthesize(self, text, voice):
            return (text + "|" + voice).encode().ljust(32, b"\0")[:32], 24000
    local = eng.LocalEngine("fakespeech", {}, "/voice")
    with pytest.raises(RuntimeError, match="not ready"):
        await local.synthesize("hi")
    local.state, local.app = "ready", SimpleNamespace(state=SimpleNamespace(provider=Provider()))
    try:
        with wave.open(io.BytesIO(await local.synthesize("Hi  there", "sojourn"))) as w:
            assert w.getframerate() == 24000 and w.readframes(16).startswith(b"Hi there|sojourn")
        with wave.open(io.BytesIO(await local.synthesize("Hi"))) as w:
            assert w.readframes(16).startswith(b"Hi|af_heart")
        with pytest.raises(ValueError, match="unknown voice"):
            await local.synthesize("Hi", "alan")
        # The plugin: why it cannot speak, and its voices (kept a minute).
        plugin = Voice()
        assert plugin.speech_unavailable() == "voice is off on this hub"
        plugin.engine = local
        assert plugin.speech_unavailable() == ""
        assert (await plugin.voices()) == {"voices": ["af_heart", "sojourn"], "default": "af_heart",
                                           "catalog": []}
        local.state = "starting"
        assert plugin.speech_unavailable() == "the voice engine is starting"
        with pytest.raises(RuntimeError, match="starting"):
            await plugin.synthesize("Hi")
    finally:
        sys.modules.pop("fakespeech.tts", None)
        sys.modules.pop("fakespeech", None)
