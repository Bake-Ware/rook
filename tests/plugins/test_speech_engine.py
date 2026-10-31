"""The voice service's 0.5 worker side (services/voice/band.py) with fake
engines: audio in and out, the caps behind the speech-engine builtin
manifest, the worker host announcing it, and ServiceEngine's mapping onto
the voice service's Provider and Speakers."""

import asyncio
import base64
import io
import wave

import pytest

pytest.importorskip("jsonschema")

from port_helpers import ME, go  # noqa: E402
from rook.core2.worker.host import Host  # noqa: E402
from rook.kit.ctx import Ctx, KitError, invoke  # noqa: E402
from services.voice import band  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.heard = []

    async def stt(self, pcm, language=None):
        self.heard.append(pcm)
        return {"text": " hi there ", "reason": None} if pcm.strip(b"\0") else {"text": "", "reason": "quiet"}

    async def tts(self, text, voice):
        return b"\x01\x00" * len(text), 24000

    async def turn(self, pcm):
        return len(pcm) > 8

    async def speaker(self, pcm):
        return {"label": "bake", "score": 0.7, "candidate": "bake", "vector": [1.0]} if pcm else None

    def voices(self):
        return {"voices": ["af_heart"], "default": "af_heart"}

    def status(self):
        return {"stt": True}


def l16(pcm: bytes, mime="audio/L16;rate=16000") -> dict:
    return {"data": base64.b64encode(pcm).decode(), "mime": mime}


def wav_file(pcm: bytes, rate=16000, channels=1) -> dict:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return {"data": base64.b64encode(out.getvalue()).decode(), "mime": "audio/wav"}


def test_audio_in():
    pcm = b"\x10\x00" * 100
    assert band.pcm16k(l16(pcm)) == pcm
    assert band.pcm16k(wav_file(pcm)) == pcm
    bad = [("not a map", "file map"), (l16(pcm, "audio/L16;rate=8000"), "16000 Hz"),
           (wav_file(pcm, rate=8000), "mono 16-bit"), (wav_file(pcm * 2, channels=2), "mono 16-bit"),
           (l16(b"\x00" * 3), "whole 16-bit"), (l16(pcm, "audio/ogg"), "audio/L16 or audio/wav"),
           ({"data": "!!", "mime": "audio/L16"}, "base64"), ({"data": "AAAA", "mime": "audio/wav"}, "WAV"),
           (l16(b"\x00\x00" * (band.RATE * band.MAX_SECONDS + 1)), "longer than")]
    for audio, why in bad:
        with pytest.raises(KitError, match=why):
            band.pcm16k(audio)


def test_wav_out():
    with wave.open(io.BytesIO(band.wav(b"\x01\x00\x02\x00", 24000))) as w:
        assert (w.getnchannels(), w.getframerate(), w.readframes(10)) == (1, 24000, b"\x01\x00\x02\x00")


def test_the_caps(tmp_path):
    engine = FakeEngine()
    caps = band.handlers(engine)
    m = band.manifest()
    assert set(caps) == {f"speech-engine.{c}" for c in m["caps"]} and m["builtin"] is True

    async def run(cap, **args):
        return await invoke(caps[cap], Ctx(m, ME, cap=cap, data_dir=tmp_path), args)

    async def body():
        speech = b"\x05\x00" * 10
        assert await run("speech-engine.stt", audio=l16(speech)) == {"text": "hi there", "language": "en"}
        quiet = await run("speech-engine.stt", audio=l16(b"\x00\x00" * 10))
        assert quiet == {"text": "", "language": "en", "reason": "quiet"}
        got = await run("speech-engine.tts", text="hey")
        assert (got.mime, got.name) == ("audio/wav", "speech.wav")
        with wave.open(io.BytesIO(got.data)) as w:
            assert (w.getframerate(), w.getnframes()) == (24000, 3)
        with pytest.raises(KitError, match="nothing to say"):
            await run("speech-engine.tts", text="  ")
        assert await run("speech-engine.turn", audio=l16(speech)) == {"complete": True}
        assert await run("speech-engine.turn", audio=l16(b"\x05\x00")) == {"complete": False}
        # The embedding stays on kaiju; only the label and scores travel.
        assert await run("speech-engine.speaker", audio=l16(speech)) == {
            "label": "bake", "score": 0.7, "candidate": "bake"}
        with pytest.raises(KitError, match="speaker ID is off"):
            await run("speech-engine.speaker", audio=l16(b""))
        assert (await run("speech-engine.voices"))["default"] == "af_heart"
        assert await run("speech-engine.status") == {"stt": True}
    go(body)


def test_the_worker_host_announces_it():
    async def body():
        host = Host()
        host.add_builtin(band.manifest(), band.handlers(FakeEngine()))
        entry = next(e for e in (await host.announce())["manifests"] if e["plugin"] == "speech-engine")
        assert entry["builtin"] is True
        assert host.plugins["speech-engine"].unavailable == {}         # every cap has its handler
        got = host.manifests(["speech-engine"])["manifests"][0]
        assert got["provides"]["voice.stt"] == "speech-engine.stt"
        await host.close()
    go(body)


def test_service_engine_maps_the_voice_service():
    class Provider:
        default_voice = "af_heart"

        class tts:
            engines = {"kokoro": {"available": True}}

            @staticmethod
            def describe():
                return {"voices": ["af_heart"]}

        class turn:
            @staticmethod
            def complete(pcm):
                return True

        async def transcribe_detail(self, pcm):
            return {"text": "yes", "reason": None}

        async def synthesize(self, text, voice):
            return b"\x00\x00", 24000

        async def _model(self, name, fn):
            assert name == "turn"
            return await asyncio.to_thread(fn)

    class Speakers:
        available = True

        async def identify(self, pcm, assistant_voice=None):
            return {"label": "guest", "voice": assistant_voice}

        def close(self):
            self.closed = True

    async def body():
        off = band.ServiceEngine(Provider(), None, "VOICE_SPEAKER_MODEL not set")
        assert await off.speaker(b"\x00\x00") is None
        assert off.status()["speaker"] == {"available": False, "error": "VOICE_SPEAKER_MODEL not set"}
        on = band.ServiceEngine(Provider(), Speakers())
        assert await on.speaker(b"\x00\x00") == {"label": "guest", "voice": "af_heart"}
        assert await on.turn(b"\x00\x00") is True
        assert (await on.stt(b"\x00\x00"))["text"] == "yes"
        assert on.voices() == {"voices": ["af_heart"]}
        assert on.status() == {"stt": True, "tts": {"kokoro": {"available": True}}, "turn": True,
                               "speaker": {"available": True}}
        on.close()
        assert on.speakers.closed
    go(body)
