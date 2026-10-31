"""speech-elevenlabs against a fake ElevenLabs API on localhost: voices from
the account or the setting, speech as a 24 kHz WAV, the key never in an
error, and a voice.tts provider the contract check accepts."""

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("jsonschema")

from port_helpers import CONTRACTS, ME, PLUGINS, go  # noqa: E402
from rook.kit.ctx import Ctx, KitError, invoke, load_run  # noqa: E402
from rook.kit.manifest import load  # noqa: E402

KEY = "xi-test-key"
MODULES: dict = {}       # one load of the plugin, as on the hub (its voice-list cache lives there)


class Fake(BaseHTTPRequestHandler):
    seen: list = []

    def _send(self, code, body: bytes, kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):        # noqa: N802
        Fake.seen.append(("GET", self.path, None))
        if self.headers.get("xi-api-key") != KEY:
            return self._send(401, json.dumps({"detail": {"message": f"bad key {self.headers.get('xi-api-key')}"}}).encode())
        self._send(200, json.dumps({"voices": [{"voice_id": "v1", "name": "Rachel"},
                                               {"voice_id": "v2", "name": "Adam Smith"}]}).encode())

    def do_POST(self):       # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        Fake.seen.append(("POST", self.path, body))
        if "/v2" in self.path:
            return self._send(429, json.dumps({"detail": "slow down"}).encode())
        self._send(200, b"\x01\x00" * 48, "application/octet-stream")

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Fake.seen = []
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def run(func, settings, **args):
    m = load(PLUGINS / "speech-elevenlabs", CONTRACTS).data
    script = m["caps"][f"speech-elevenlabs.{func}"]["run"]
    secrets = {"el-key": KEY, "wrong": "nope-key"}

    async def body():
        ctx = Ctx(m, ME, settings=settings, resolve_secret=secrets.get, cap=func)
        return await invoke(load_run(PLUGINS / "speech-elevenlabs", script, MODULES), ctx, args)
    return go(body)


def test_a_clean_provider():
    m = load(PLUGINS / "speech-elevenlabs", CONTRACTS)
    assert [str(w) for w in m.warnings] == []
    assert m.data["provides"] == {"voice.tts": "speech-elevenlabs.tts"}


def test_voices_and_speech(api):
    s = {"key": "el-key", "url": api}
    got = run("voices", s)
    assert got["voices"] == ["eleven_adam_smith", "eleven_rachel"] and got["default"] == "eleven_adam_smith"
    out = run("tts", {**s, "default": "eleven_rachel"}, text="Hello there.")
    assert (out.mime, out.name) == ("audio/wav", "speech.wav")
    with wave.open(io.BytesIO(out.data)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 24000, 48)
    method, path, body = Fake.seen[-1]
    assert (method, path) == ("POST", "/v1/text-to-speech/v1?output_format=pcm_24000")
    assert body == {"text": "Hello there.", "model_id": "eleven_flash_v2_5"}

    # The account's list is kept a while; voices named in the setting: it isn't asked at all.
    Fake.seen = []
    run("tts", s, text="Again.", voice="eleven_rachel")
    assert [m for m, _, _ in Fake.seen] == ["POST"]
    # Voices named in the setting: the account isn't asked.
    Fake.seen = []
    got = run("voices", {**s, "voices": "Jo=vJ, bad id=has space"})
    assert got["voices"] == ["eleven_jo"] and Fake.seen == []


def test_failures(api):
    with pytest.raises(KitError, match="key is not set"):
        run("tts", {"url": api}, text="hi")
    with pytest.raises(KitError, match="must be https"):
        run("voices", {"key": "el-key", "url": "http://example.com"})
    with pytest.raises(KitError, match="no ElevenLabs voice 'eleven_nobody'"):
        run("tts", {"key": "el-key", "url": api}, text="hi", voice="eleven_nobody")
    with pytest.raises(KitError, match="HTTP 429: slow down") as e:
        run("tts", {"key": "el-key", "url": api}, text="hi", voice="eleven_adam_smith")
    assert e.value.retry
    with pytest.raises(KitError, match="HTTP 401") as e:
        run("voices", {"key": "wrong", "url": api})
    assert "nope-key" not in e.value.message and "***" in e.value.message
