"""The voice service as a Rook 0.5 worker: ``python -m services.voice.band --config worker.yaml``.

It serves ``speech-engine.*`` (rook/plugins/speech-engine/plugin.yaml, a
``builtin: true`` manifest) from this process, so the models load once and
the band reaches them over the normal ticketed call path: the hub's
``speech`` plugin calls ``voice.stt``/``voice.tts``/``voice.turn``/
``voice.speaker`` and core routes them here. The worker is an ordinary 0.5
worker otherwise (enroll it first with ``python -m rook.core2.worker
--config worker.yaml enroll ...``; the config may load plugin folders too).

The engines are the voice service's own (Whisper, Kokoro/Chatterbox, Smart
Turn, TitaNet via ``Speakers``), configured by the same environment as
``python -m services.voice.server`` (VOICE_MODEL_DIR, WHISPER_*, VOICE_SPEAKER_*...).
The standalone server keeps running unchanged beside or instead of this.

``handlers(engine)`` takes any object with the :class:`ServiceEngine`
methods, which is how the tests run it with fakes.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import io
import os
import sys
import wave
from pathlib import Path

from rook.kit.ctx import KitError

MANIFEST = Path(__file__).resolve().parents[2] / "rook" / "plugins" / "speech-engine"
RATE = 16000                 # what Whisper, Smart Turn and TitaNet take
MAX_SECONDS = 30
WAV = ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave")
PCM = ("audio/l16", "application/octet-stream")


# -- audio --------------------------------------------------------------------

def pcm16k(audio) -> bytes:
    """16 kHz mono 16-bit PCM from a file map ``{data: base64, mime}``
    (audio/L16 at 16 kHz, or a mono 16-bit 16 kHz WAV)."""
    if not isinstance(audio, dict) or not isinstance(audio.get("data"), str):
        raise KitError("audio must be a file map {data: base64, mime}")
    kind, _, params = str(audio.get("mime") or "audio/L16").partition(";")
    kind = kind.strip().lower()
    try:
        raw = base64.b64decode(audio["data"], validate=True)
    except (binascii.Error, ValueError):
        raise KitError("audio data is not base64") from None
    if kind in PCM:
        rate = dict(p.strip().partition("=")[::2] for p in params.split(";") if "=" in p).get("rate")
        if rate and rate.strip() != str(RATE):
            raise KitError(f"audio/L16 must be {RATE} Hz")
        pcm = raw
    elif kind in WAV:
        try:
            with wave.open(io.BytesIO(raw)) as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
                    raise KitError(f"WAV audio must be mono 16-bit {RATE} Hz")
                pcm = w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            raise KitError("audio is not a readable WAV file") from None
    else:
        raise KitError(f"audio must be audio/L16 or audio/wav, not {kind}")
    if len(pcm) % 2:
        raise KitError("audio must be whole 16-bit samples")
    if len(pcm) > MAX_SECONDS * RATE * 2:
        raise KitError(f"audio longer than {MAX_SECONDS} s")
    return pcm


def wav(pcm: bytes, rate: int) -> bytes:
    """A mono 16-bit WAV file around ``pcm``."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    return out.getvalue()


# -- the caps -------------------------------------------------------------------

def handlers(engine) -> dict:
    """``{cap: handler(ctx, **args)}`` for the speech-engine manifest."""
    async def stt(ctx, audio, language=None):
        got = await engine.stt(pcm16k(audio), language or None)
        out = {"text": (got.get("text") or "").strip(), "language": got.get("language") or "en"}
        if got.get("reason"):
            out["reason"] = str(got["reason"])
        return out

    async def tts(ctx, text, voice=None):
        if not text.strip():
            raise ctx.Error("nothing to say")
        pcm, rate = await engine.tts(text, voice or "")
        return ctx.file(data=wav(pcm, rate), name="speech.wav", mime="audio/wav")

    async def turn(ctx, audio):
        return {"complete": bool(await engine.turn(pcm16k(audio)))}

    async def speaker(ctx, audio):
        got = await engine.speaker(pcm16k(audio))
        if got is None:
            raise ctx.Error("no speaker result: speaker ID is off here, or the audio is under 0.6 s")
        return {k: got[k] for k in ("label", "score", "candidate") if got.get(k) is not None}

    async def voices(ctx):
        return engine.voices()

    async def status(ctx):
        return engine.status()

    return {f"speech-engine.{name}": f for name, f in
            (("stt", stt), ("tts", tts), ("turn", turn), ("speaker", speaker),
             ("voices", voices), ("status", status))}


class ServiceEngine:
    """The voice service's loaded models (``providers.Provider``, and
    ``speaker.Speakers`` when VOICE_SPEAKER_MODEL is set), one call at a time
    per model, as in the standalone server."""

    def __init__(self, provider, speakers=None, speakers_error: str | None = None):
        self.provider, self.speakers, self.speakers_error = provider, speakers, speakers_error

    @classmethod
    def load(cls) -> "ServiceEngine":
        from .providers import Provider
        provider, speakers, error = Provider(), None, "VOICE_SPEAKER_MODEL not set"
        if os.environ.get("VOICE_SPEAKER_MODEL", "").strip():
            from .speaker import Speakers
            root = Path(os.environ.get("VOICE_MODEL_DIR", "."))
            speakers = Speakers(os.environ.get("VOICE_SPEAKER_DB", str(root / "voice-speakers.sqlite3")))
            error = speakers.error
        return cls(provider, speakers, error)

    async def stt(self, pcm, language=None):
        return await self.provider.transcribe_detail(pcm)

    async def tts(self, text, voice):
        return await self.provider.synthesize(text, voice)

    async def turn(self, pcm):
        return await self.provider._model("turn", lambda: self.provider.turn.complete(pcm))

    async def speaker(self, pcm):
        if self.speakers is None or not self.speakers.available:
            return None
        return await self.speakers.identify(pcm, self.provider.default_voice)

    def voices(self):
        return self.provider.tts.describe()

    def status(self):
        on = self.speakers is not None and self.speakers.available
        return {"stt": True, "tts": self.provider.tts.engines, "turn": True,
                "speaker": {"available": on, **({} if on else {"error": self.speakers_error})}}

    def close(self):
        if self.speakers is not None:
            self.speakers.close()


def manifest() -> dict:
    from rook.kit.manifest import check
    data, issues = check(MANIFEST)
    errors = [str(i) for i in issues if i.level == "error"]
    if data is None or errors:
        raise RuntimeError(f"speech-engine manifest is invalid: {errors}")
    return data


def main(argv=None, engine=None) -> int:
    from rook.core2.worker.__main__ import serve
    from rook.core2.worker.runtime import Worker, load_config, setup_logging
    ap = argparse.ArgumentParser(prog="python -m services.voice.band",
                                 description="The voice service as a Rook 0.5 worker (speech-engine.*)")
    ap.add_argument("--config", required=True, help="the worker's YAML config (enroll with rook.core2.worker)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.log)
    engine = engine or ServiceEngine.load()
    try:
        asyncio.run(serve(Worker(cfg, extra_builtins=[(manifest(), handlers(engine))])))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as e:
        print(f"worker: {e}", file=sys.stderr)
        return 1
    finally:
        if hasattr(engine, "close"):
            engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
