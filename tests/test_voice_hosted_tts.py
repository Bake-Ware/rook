"""Hosted voices (ElevenLabs) beside Kokoro and Chatterbox: off unless asked for,
the key only in a header, every failure spoken by Kokoro instead. Tested against
a fake ElevenLabs (httpx transport, and a real local HTTP server); no key needed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

np = pytest.importorskip("numpy")
httpx = pytest.importorskip("httpx")

from services.voice import tts
from tests.test_voice_tts import FakeChatterbox, FakeKokoro

KEY = "el-test-key-0123456789"
VOICES = [{"voice_id": "21m00Tcm4TlvDq8ntWcQ", "name": "Rachel"},
          {"voice_id": "pNInz6obpgDQGcFmaJgB", "name": "Adam Deep"},
          {"voice_id": "bad id!", "name": "Broken"},
          {"name": "No id"}]
PCM = (np.full(2400, 0.25, dtype=np.float32) * 32767).astype("<i2").tobytes()


class FakeElevenLabs:
    """What the engine sends, and ElevenLabs-shaped answers."""

    def __init__(self, status=200, detail=None):
        self.status, self.detail, self.requests = status, detail, []

    def answer(self, method, path, query, headers, body):
        self.requests.append({"method": method, "path": path, "query": query,
                              "key": headers.get("xi-api-key"), "body": body})
        if headers.get("xi-api-key") != KEY:
            return 401, "application/json", json.dumps({"detail": {"status": "invalid_api_key",
                                                                   "message": "Invalid API key"}}).encode()
        if self.status != 200:
            return self.status, "application/json", json.dumps({"detail": self.detail}).encode()
        if method == "GET" and path == "/v1/voices":
            return 200, "application/json", json.dumps({"voices": VOICES}).encode()
        if method == "POST" and path.startswith("/v1/text-to-speech/"):
            return 200, "application/octet-stream", PCM
        return 404, "application/json", b'{"detail": "not found"}'

    def transport(self):
        def handle(request):
            body = json.loads(request.content) if request.content else None
            status, ctype, data = self.answer(request.method, request.url.path, dict(request.url.params),
                                              request.headers, body)
            return httpx.Response(status, content=data, headers={"content-type": ctype})
        return httpx.MockTransport(handle)


def engine(fake=None, voices=None, **kw):
    fake = fake or FakeElevenLabs()
    return tts.ElevenLabsEngine(KEY, voices, transport=fake.transport(), **kw), fake


# --- voice names ---------------------------------------------------------------
def test_parse_voice_map():
    assert tts.parse_voice_map("Rachel=21m00Tcm4TlvDq8ntWcQ, Adam Deep = pNInz6obpgDQGcFmaJgB") == {
        "eleven_rachel": ("Rachel", "21m00Tcm4TlvDq8ntWcQ"),
        "eleven_adam_deep": ("Adam Deep", "pNInz6obpgDQGcFmaJgB")}
    assert tts.parse_voice_map("21m00Tcm4TlvDq8ntWcQ") == {
        "eleven_21m00tcm4tlvdq8ntwcq": ("21m00Tcm4TlvDq8ntWcQ", "21m00Tcm4TlvDq8ntWcQ")}
    # Anything that could escape the URL path, or names nothing, is dropped.
    assert tts.parse_voice_map("x=../../v1/user, =abc, y=, ,z=a/b") == {}
    assert tts.parse_voice_map("") == {} and tts.parse_voice_map(None) == {}


# --- the engine ---------------------------------------------------------------------
def test_lists_the_accounts_voices_when_none_are_configured():
    eng, fake = engine()
    assert eng.voices == ["eleven_adam_deep", "eleven_rachel"]
    assert eng.labels["eleven_rachel"] == "Rachel (ElevenLabs)"
    assert fake.requests[0]["path"] == "/v1/voices" and fake.requests[0]["key"] == KEY


def test_configured_voices_need_no_listing():
    eng, fake = engine(voices=tts.parse_voice_map("Sam=yoZ06aMxZJJ28mfd3POQ"))
    assert eng.voices == ["eleven_sam"] and fake.requests == []


def test_synthesize_asks_for_24khz_pcm_with_the_key_in_a_header():
    eng, fake = engine(model="eleven_multilingual_v2")
    samples, sr = eng.synthesize("Hello there.", "eleven_rachel")
    assert sr == 24000 and samples.dtype == np.float32 and len(samples) == 2400
    assert abs(float(samples[0]) - 0.25) < 1e-3
    req = fake.requests[-1]
    assert req["method"] == "POST" and req["path"] == "/v1/text-to-speech/21m00Tcm4TlvDq8ntWcQ"
    assert req["query"] == {"output_format": "pcm_24000"}
    assert req["body"] == {"text": "Hello there.", "model_id": "eleven_multilingual_v2"}
    assert req["key"] == KEY and all(KEY not in str(v) for v in (req["path"], req["query"], req["body"]))


def test_refusals_say_why_and_never_carry_the_key():
    eng, fake = engine(voices=tts.parse_voice_map("Rachel=21m00Tcm4TlvDq8ntWcQ"))
    fake.status, fake.detail = 401, {"status": "quota_exceeded", "message": f"quota used up for {KEY}"}
    with pytest.raises(RuntimeError) as err:
        eng.synthesize("Hi.", "eleven_rachel")
    assert "HTTP 401" in str(err.value) and "quota used up" in str(err.value) and KEY not in str(err.value)
    with pytest.raises(RuntimeError, match="no ElevenLabs voice"):
        eng.synthesize("Hi.", "af_heart")
    with pytest.raises(RuntimeError, match="longer than"):
        eng.synthesize("x" * (tts.ELEVENLABS_MAX_CHARS + 1), "eleven_rachel")


def test_a_bad_key_or_address_never_loads():
    with pytest.raises(ValueError, match="no ElevenLabs API key"):
        tts.ElevenLabsEngine("", None)
    for url in ("http://api.elevenlabs.io", "ftp://x", "http://192.0.2.5:8080"):
        with pytest.raises(ValueError, match="https"):
            tts.ElevenLabsEngine(KEY, tts.parse_voice_map("a=b"), base_url=url)
    tts.ElevenLabsEngine(KEY, tts.parse_voice_map("a=b"), base_url="http://127.0.0.1:9").close()
    bad = FakeElevenLabs()
    with pytest.raises(RuntimeError, match="voice list: HTTP 401"):
        tts.ElevenLabsEngine("wrong-key", None, transport=bad.transport())


# --- in the catalog ----------------------------------------------------------------------
def catalog(fake=None, chatterbox=None, **kw):
    eng, fake = engine(fake)
    kokoro = FakeKokoro()
    return tts.Catalog(kokoro, "af_heart", chatterbox, None, target_sr=24000, hosted=[eng], **kw), kokoro, fake


def test_hosted_voices_join_the_catalog_and_the_default_stays_local():
    cat, _k, _f = catalog()
    d = cat.describe()
    assert d["default"] == "af_heart"
    assert d["engines"]["elevenlabs"] == {"available": True, "hosted": True}
    hosted = [e for e in d["catalog"] if e["engine"] == "elevenlabs"]
    assert [e["id"] for e in hosted] == ["eleven_adam_deep", "eleven_rachel"]
    assert hosted[1]["label"] == "Rachel (ElevenLabs)"
    assert cat.resolve("elevenlabs:eleven_rachel") == "eleven_rachel"
    assert cat.resolve("kokoro:eleven_rachel") is None
    assert not cat.supports_tags("eleven_rachel")
    # Chosen as the default, it is the default.
    assert catalog(default="eleven_rachel")[0].default == "eleven_rachel"


def test_speaking_a_hosted_voice_sends_only_that_text_without_tags():
    cat, kokoro, fake = catalog()
    pcm, sr = cat.synthesize("Ha [laugh] okay.", "eleven_rachel")
    assert sr == 24000 and len(pcm) == 2400 * 2 and kokoro.calls == []
    assert fake.requests[-1]["body"]["text"] == "Ha okay."
    # A local voice never reaches ElevenLabs.
    before = len(fake.requests)
    cat.synthesize("Local only.", "af_nova")
    assert len(fake.requests) == before and kokoro.calls == [("Local only.", "af_nova")]


def test_a_failing_hosted_voice_is_spoken_by_kokoro():
    fake = FakeElevenLabs()
    cat, kokoro, _ = catalog(fake)
    fake.status, fake.detail = 429, {"status": "too_many_concurrent_requests"}
    told = []
    pcm, sr = cat.synthesize("Still heard [sigh].", "eleven_rachel", on_fallback=told.append)
    assert pcm and sr == 24000
    assert kokoro.calls == [("Still heard.", "af_heart")]
    assert "HTTP 429" in str(told[0]) and "too_many_concurrent_requests" in str(told[0])


def test_a_hosted_name_taken_by_a_local_voice_is_skipped():
    eng, _ = engine(voices={"warm": ("Warm", "abc"), "eleven_rachel": ("Rachel", "def")})
    cat = tts.Catalog(FakeKokoro(), "af_heart", FakeChatterbox(), None, hosted=[eng])
    assert cat.engine["warm"] == "chatterbox" and cat.engine["eleven_rachel"] == "elevenlabs"


# --- opting in: VOICE_TTS_HOSTED ----------------------------------------------------------
def test_off_unless_asked_for():
    for value in ("", "off", " none "):
        assert tts.load_hosted({"VOICE_TTS_HOSTED": value, "VOICE_ELEVENLABS_API_KEY": KEY}) == ([], {})
    assert tts.load_hosted({}) == ([], {})


def test_asked_for_without_a_key_or_by_an_unknown_name_says_why():
    engines, errors = tts.load_hosted({"VOICE_TTS_HOSTED": "elevenlabs, piper"})
    assert engines == [] and "no ElevenLabs API key" in str(errors["elevenlabs"])
    assert "unknown hosted voice service" in errors["piper"]
    cat = tts.Catalog(FakeKokoro(), "af_heart", hosted_errors=errors)
    assert cat.describe()["engines"]["elevenlabs"]["available"] is False
    assert cat.voices == ["af_heart", "af_nova", "bf_emma"]


@pytest.fixture
def local_elevenlabs():
    """A fake ElevenLabs on a loopback port, spoken to over real HTTP."""
    fake = FakeElevenLabs()

    class Handler(BaseHTTPRequestHandler):
        def _do(self):
            from urllib.parse import parse_qsl, urlsplit
            url = urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, ctype, data = fake.answer(self.command, url.path, dict(parse_qsl(url.query)),
                                              {k.lower(): v for k, v in self.headers.items()},
                                              json.loads(raw) if raw else None)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _do

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_load_hosted_end_to_end_against_a_local_fake(local_elevenlabs):
    fake, url = local_elevenlabs
    engines, errors = tts.load_hosted({"VOICE_TTS_HOSTED": "elevenlabs", "VOICE_ELEVENLABS_API_KEY": KEY,
                                       "VOICE_ELEVENLABS_URL": url, "VOICE_ELEVENLABS_TIMEOUT_S": "3"})
    assert errors == {} and [e.name for e in engines] == ["elevenlabs"]
    try:
        cat = tts.Catalog(FakeKokoro(), "af_heart", target_sr=24000, hosted=engines)
        pcm, sr = cat.synthesize("Over the wire.", "eleven_adam_deep")
        assert sr == 24000 and len(pcm) == len(PCM)
        assert fake.requests[-1]["path"] == "/v1/text-to-speech/pNInz6obpgDQGcFmaJgB"
        assert fake.requests[-1]["body"]["model_id"] == tts.ELEVENLABS_MODEL
    finally:
        for e in engines:
            e.close()
