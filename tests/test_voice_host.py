"""The hub reading huddle messages aloud through a voice server (the voice host).

Which voice says a message (the role's, the house agent's, the band's
default), the remote engine against a fake voice server (its voice list in
picker order, a busy server, the band default), the audio kept per (voice,
text), and the voice pickers on the Roles, Home agent, Voice and Settings
pages.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rook.hub import site as site_mod
from rook.hub.plugins import voice as voice_mod
from rook.hub.plugins.voice import AudioCache
from rook.hub.plugins.voice import engine as eng
from tests.test_huddle_chair import _client, hub  # noqa: F401 - fixture
from tests.test_huddle_speak import _HARNESS, FakeVoice, _qa_said, _with_voice
from tests.test_voice_plugin import clean_env, node_for, wait_for  # noqa: F401 - autouse fixture

ROOT = Path(__file__).resolve().parent.parent

# What a voice server's /voices says: Kokoro first, as the real one lists them.
CATALOG = [{"id": "af_heart", "engine": "kokoro", "name": "af_heart", "label": "Heart (US female)"},
           {"id": "bm_george", "engine": "kokoro", "name": "bm_george", "label": "George (UK male)"},
           {"id": "default", "engine": "chatterbox", "name": "default", "label": "Chatterbox (default)"},
           {"id": "narrator", "engine": "chatterbox", "name": "narrator", "label": "Narrator"}]


def fake_voice_server(seen: list, busy: list | None = None):
    """A voice server: /voices, and /api/voice saying 503 busy ``busy[0]`` times."""
    busy = busy if busy is not None else [0]

    async def voices(request):
        seen.append(("GET /voices", None))
        return web.json_response({"voices": [c["id"] for c in CATALOG], "default": "narrator",
                                  "catalog": CATALOG})

    async def speak(request):
        body = await request.json()
        seen.append((request.headers.get("Authorization"), body))
        if busy[0] > 0:
            busy[0] -= 1
            return web.json_response({"detail": "Voice engine is busy."}, status=503)
        if body.get("voice") not in (None, *[c["id"] for c in CATALOG]):
            return web.json_response({"detail": "Unknown voice. Inspect /voices."}, status=400)
        return web.Response(body=b"RIFF" + json.dumps(body).encode(), content_type="audio/wav")

    app = web.Application()
    app.router.add_get("/voices", voices)
    app.router.add_post("/api/voice", speak)
    return TestServer(app)


# -- the audio kept ------------------------------------------------------------------

def test_audio_is_kept_per_voice_and_text_up_to_its_size():
    cache = AudioCache(10)
    cache.put(("a", "one"), b"1234")
    cache.put(("b", "one"), b"5678")
    assert cache.get(("a", "one")) == b"1234" and cache.hits == 1   # now the newest
    cache.put(("c", "two"), b"abcd")                                # 12 bytes: the oldest goes
    assert cache.get(("b", "one")) is None and cache.get(("a", "one")) == b"1234"
    assert len(cache) == 2 and cache.size == 8
    cache.put(("d", "big"), b"x" * 11)                              # larger than all of it
    assert cache.get(("d", "big")) is None and len(cache) == 2
    cache.put(("a", "one"), b"12")                                  # replaced, not added
    assert cache.size == 6
    off = AudioCache(0)
    off.put(("a", "one"), b"1")
    assert off.get(("a", "one")) is None and len(off) == 0


# -- the remote engine against a voice server -------------------------------------------

@pytest.mark.asyncio
async def test_remote_engine_waits_out_a_busy_voice_server(monkeypatch):
    monkeypatch.setattr(eng, "BUSY_WAIT", 0.01)
    seen, busy = [], [2]
    server = fake_voice_server(seen, busy)
    await server.start_server()
    remote = eng.RemoteEngine(str(server.make_url("")).rstrip("/"), "/voice")
    try:
        audio = await remote.synthesize("Build is green.", "narrator")
        assert audio.startswith(b"RIFF") and len(seen) == 3
        assert seen[-1] == (None, {"text": "Build is green.", "voice": "narrator"})  # no key: a guest
        busy[0] = eng.BUSY_RETRIES + 1
        with pytest.raises(RuntimeError, match="HTTP 503: Voice engine is busy"):
            await remote.synthesize("Again.")
    finally:
        await remote.stop()
        await server.close()


@pytest.mark.asyncio
async def test_the_voice_host_lists_chatterbox_first_speaks_the_band_default_and_keeps_audio(
        tmp_path, monkeypatch):
    # The two-process hub: no voice paths served, the Speak buttons still read.
    monkeypatch.setattr(site_mod, "_SERVED", False)
    seen: list = []
    server = fake_voice_server(seen)
    await server.start_server()
    node = node_for(tmp_path, engine="remote", remote_url=str(server.make_url("")).rstrip("/"),
                    tts_default="default", speech_cache_mb=1.0)
    await node.start()
    try:
        voice = node.plugin("voice")
        assert voice is not None, node.host.status["voice"]
        v = await voice.voices()
        assert [c["id"] for c in v["catalog"]] == ["default", "narrator", "af_heart", "bm_george"]
        assert v["voices"] == ["default", "narrator", "af_heart", "bm_george"]
        assert v["default"] == "default" and v["engine_default"] == "narrator"
        await voice.voices()                                     # kept a minute
        assert [x for x in seen if x[0] == "GET /voices"] == [("GET /voices", None)]

        # Blank: the band's default voice, said to the server by name.
        first = await voice.synthesize("Deploy  is\ndone.")
        assert seen[-1] == (None, {"text": "Deploy is done.", "voice": "default"})
        calls = len(seen)
        # The same line again, however it is spaced, comes from memory.
        assert await voice.synthesize("Deploy is done.", "default") == first
        assert await voice.synthesize(" Deploy is done. ") == first
        assert len(seen) == calls and voice.status()["cache"]["hits"] == 2
        await voice.synthesize("Deploy is done.", "af_heart")     # another voice: asked again
        assert len(seen) == calls + 1 and seen[-1][1]["voice"] == "af_heart"

        p = await voice.preview("narrator")
        assert p["voice"] == "narrator" and p["mime"] == "audio/wav"
        assert json.loads(base64.b64decode(p["audio"])[4:])["voice"] == "narrator"
        assert (await voice.preview(""))["voice"] == "default"
        with pytest.raises(RuntimeError, match="Unknown voice"):
            await voice.synthesize("Hi", "nobody")

        # Kept nothing when the cap is 0.
        node.settings.set("voice.speech_cache_mb", 0.0, actor="human:op")   # reloads it
        assert await wait_for(lambda: node.plugin("voice") not in (None, voice))
        voice = node.plugin("voice")
        n = len(seen)
        await voice.synthesize("Twice.")
        await voice.synthesize("Twice.")
        assert len(seen) == n + 2
    finally:
        await node.stop()
        await server.close()


def test_the_voice_settings_name_the_band_default_and_the_cache():
    by = {s.name: s for s in voice_mod.SETTINGS}
    assert by["tts_default"].label == "Band default voice" and by["tts_default"].default == ""
    assert by["speech_cache_mb"].default > 0
    assert {"speech_cache_mb", "engine", "remote_url"} <= voice_mod.HUB_ONLY
    assert voice_mod.ENGINE_ORDER["chatterbox"] < voice_mod.ENGINE_ORDER["kokoro"]


# -- which voice reads a message ----------------------------------------------------------

class BandDefaultVoice(FakeVoice):
    """A voice plugin whose blank voice is the band default ``narrator``."""

    async def voices(self):
        return {"voices": ["narrator", "af_heart", "nova"], "default": "narrator",
                "engine_default": "af_heart",
                "catalog": [{"id": "narrator", "label": "Narrator", "engine": "chatterbox"},
                            {"id": "nova", "label": "Nova", "engine": "chatterbox"},
                            {"id": "af_heart", "label": "Heart", "engine": "kokoro"}]}

    async def preview(self, voice=""):
        await self.synthesize("preview", voice)
        return {"audio": base64.b64encode(b"RIFFp").decode(), "mime": "audio/wav",
                "voice": voice or "narrator"}


@pytest.mark.asyncio
async def test_the_house_agent_speaks_in_its_own_voice_and_roles_in_theirs(hub, monkeypatch):  # noqa: F811
    voice = BandDefaultVoice()
    _with_voice(hub, monkeypatch, voice)
    room, qa, _ = await _qa_said(hub, "Tests pass.")            # qa's voice is nova
    home = hub.node.plugin("home")
    hub.svc.chat.send(room, home.identity, "I asked QA.", [], False)
    chair_seq = hub.svc.chat.tail(room, 1)[0]["seq"]
    async with _client(hub) as c:
        async def speak(seq):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak",
                                                          "room": room, "seq": seq})
            assert r.status_code == 200, r.text
            return r.json()["result"]
        r = await speak(qa["seq"])
        assert r["source"] == "role" and voice.said[-1] == ("Tests pass.", "nova")
        # The house agent without a voice: the band's default (blank to the engine).
        r = await speak(chair_seq)
        assert r["source"] == "default" and r["label"] == "narrator" and voice.said[-1][1] == ""
        hub.node.settings.set("home.voice", "af_heart", actor="human:op")
        r = await speak(chair_seq)
        assert r["source"] == "role" and r["voice"] == "af_heart" and r["role"] == ""
        assert voice.said[-1] == ("I asked QA.", "af_heart")

        # A preview in the picked voice, or the band default when blank.
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "preview",
                                                       "voice": "nova"})).json()["result"]
        assert r["mode"] == "hub" and r["voice"] == "nova" and voice.said[-1][1] == "nova"
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "preview"})).json()
        assert r["result"]["voice"] == "narrator"
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "preview",
                                                      "voice": "a b"})
        assert r.status_code == 400
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "voices"})).json()
        assert r["result"]["default"] == "narrator" and r["result"]["engine_default"] == "af_heart"

        # Play on a phone: the house agent's voice when it has one; none sent
        # otherwise, so the phone keeps its own (never the band default).
        now = time.time()
        hub.band.workers.update({"w-phone": {"name": "myphone", "caps": ["voice.speak"],
                                             "last_seen": now}})
        monkeypatch.setattr(hub.roles, "protected", lambda: ["Kid tablet"], raising=False)

        async def play(seq):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "speak_on",
                                                          "room": room, "seq": seq, "worker": "myphone"})
            assert r.status_code == 200, r.text
            return [x for x in hub.band.calls if x.cap == "voice.speak"][-1].args
        assert (await play(chair_seq)).get("voice") == "af_heart"
        hub.node.settings.set("home.voice", "", actor="human:op")
        assert "voice" not in await play(chair_seq)


@pytest.mark.asyncio
async def test_preview_without_a_voice_engine_is_read_by_the_browser(hub, monkeypatch):  # noqa: F811
    _with_voice(hub, monkeypatch, None)
    async with _client(hub) as c:
        r = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "preview",
                                                       "voice": "nova"})).json()["result"]
        assert r["mode"] == "browser" and r["text"] and "no voice engine" in r["reason"]


def test_the_house_agent_has_a_speaking_voice_setting(hub):  # noqa: F811
    home = hub.node.plugin("home")
    s = {x.name: x for x in home.SETTINGS}["voice"]
    assert s.label == "Speaking voice" and s.default == ""
    hub.node.settings.set("home.voice", "chatterbox:narrator", actor="human:op")
    with pytest.raises(Exception):
        hub.node.settings.set("home.voice", "bad voice!", actor="human:op")


# -- the voice pickers ------------------------------------------------------------------

def test_every_page_with_a_voice_shows_a_picker_with_a_preview():
    web_dir = ROOT / "rook/web"
    roles = (web_dir / "roles.js").read_text(encoding="utf-8")
    home = (web_dir / "home.js").read_text(encoding="utf-8")
    voice = (web_dir / "voice.js").read_text(encoding="utf-8")
    settings = (web_dir / "settings-area.js").read_text(encoding="utf-8")
    # Roles: in the main form, not folded away under Model (where Bake could not see it).
    model = roles[roles.index("h('summary',{text:'Model'})"):]
    model = model[:model.index("hostBox")]
    assert "'Speaking voice'" in roles and "Speaking voice" not in model and "vp.el" not in model
    assert "voicePicker(doc.voice)" in roles
    # The house agent (the chair): its own field, saved as home.voice.
    assert "voicePicker(val('voice'))" in home and "voice:vp.select.value" in home
    assert "field('Speaking voice'" in home
    # The band default: on Manage > Voice and in Settings.
    assert "voicePicker(val('tts_default')" in voice and "key:'voice.tts_default'" in voice
    assert "['voice.tts_default'" in settings and "['home.voice'" in settings
    assert "fillVoiceSelect(input,cur" in settings


_PICKER = r"""
const tick=()=>new Promise(r=>setTimeout(r,5));
globalThis.replies=[{available:true,default:'narrator',engine_default:'af_heart',voices:['af_heart','narrator','nova'],
  catalog:[{id:'af_heart',label:'Heart',engine:'kokoro'},{id:'narrator',label:'Narrator',engine:'chatterbox'},
           {id:'nova',label:'nova',engine:'chatterbox'}]},
  {mode:'hub',audio:btoa('RIFF'),mime:'audio/wav',voice:'nova'}];
const p=voicePicker('nova');await tick();
const out={};
out.groups=p.select.children.map(c=>c.tag==='optgroup'?[c.attrs.label,c.children.map(o=>[o.attrs.value,o.textContent,!!o.attrs.selected])]:[c.attrs.value,c.textContent]);
p.select.value='nova';const play=p.el.children[1];play.onclick({preventDefault(){}});await tick();
out.preview=posts.at(-1);out.played=audios.length;
const gone=voicePicker('retired');await tick();
out.unknown=gone.select.children.at(-1).textContent;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_picker_lists_chatterbox_first_and_previews(tmp_path):
    src = (ROOT / "rook/web/huddles.js").read_text(encoding="utf-8")
    first = src.index("\n", src.index("const {h,el,more,copyChip}"))   # the manage.js import
    body = src[first:].replace("new URL(import.meta.url).search", "''")
    harness = _HARNESS.replace("append(...k){this.children.push(...k);}",
                               "append(...k){this.children.push(...k);} "
                               "replaceChildren(...k){this.children=k;}").replace(
        "for(const [k,v] of Object.entries(props||{})){",
        "for(const [k,v] of Object.entries(props||{})){if(v===false||v==null)continue;")
    (tmp_path / "hx.mjs").write_text("const search='';" + harness + body.replace(
        "const search=new URL(import.meta.url).search;", ""), encoding="utf-8")
    (tmp_path / "t.mjs").write_text("import {voicePicker} from './hx.mjs';\n" + _PICKER,
                                    encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["groups"] == [
        ["", "Band default (narrator)"],
        ["Chatterbox (natural voices)", [["narrator", "Narrator", False], ["nova", "nova", True]]],
        ["Kokoro (quick voices)", [["af_heart", "Heart (af_heart)", False]]]]
    assert out["preview"] == {"csrf": "tok", "action": "preview", "voice": "nova"}
    assert out["played"] == 1
    assert out["unknown"] == "retired (not on the voice engine)"


@pytest.mark.asyncio
async def test_remote_engine_reads_a_reply_compressed_on_the_way():
    # A CDN in front of the voice server gzips JSON; the hub's session never
    # decompresses (it forwards replies untouched), so its own calls must.
    import gzip
    seen = []

    async def voices(request):
        seen.append(request.headers.get("Accept-Encoding"))
        body = gzip.compress(json.dumps({"voices": ["narrator"], "default": "narrator",
                                         "catalog": CATALOG[2:]}).encode())
        return web.Response(body=body, headers={"Content-Encoding": "gzip",
                                                "Content-Type": "application/json"})

    async def health(request):
        return web.Response(body=gzip.compress(b'{"ok": true, "version": "2.0.0"}'),
                            headers={"Content-Encoding": "gzip"})

    async def speak(request):
        seen.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=gzip.compress(b"RIFFwav"), headers={"Content-Encoding": "gzip"},
                            content_type="audio/wav")

    app = web.Application()
    app.router.add_get("/voices", voices)
    app.router.add_get("/health", health)
    app.router.add_post("/api/voice", speak)
    server = TestServer(app)
    await server.start_server()
    remote = eng.RemoteEngine(str(server.make_url("")).rstrip("/"), "/voice")
    try:
        assert (await remote.voices())["default"] == "narrator"
        assert await remote.synthesize("Hi.") == b"RIFFwav"
        assert (await remote.health())["ok"] is True
        assert seen == ["identity", "identity"]
    finally:
        await remote.stop()
        await server.close()
