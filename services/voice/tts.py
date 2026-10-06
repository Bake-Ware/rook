"""Text-to-speech engines and the voice catalog.

Two engines, both first-class:

* ``kokoro``: kokoro-onnx on the CPU, always loaded (the provider builds it).
* ``chatterbox``: Resemble AI's Chatterbox Turbo on a GPU. Loaded only when
  ``VOICE_CHATTERBOX_DEVICE`` (e.g. ``cuda:0``) is set. It runs in its own Python
  (``VOICE_CHATTERBOX_PYTHON``, a venv with torch + chatterbox-tts) through
  ``chatterbox_worker.py`` so torch never shares this process; without that
  variable the worker code is imported in-process instead.

Voice ids are namespaced by engine: ``kokoro:af_heart``, ``chatterbox:default``.
A bare id (``af_heart``) is a legacy Kokoro id and keeps meaning Kokoro.

Paralinguistic tags such as ``[laugh]`` go to Chatterbox unchanged and are
stripped before Kokoro, which would read them aloud. If Chatterbox fails for an
utterance, that utterance is spoken by Kokoro and the caller's ``on_fallback``
is told (the runtime reports it once per session).
"""
import json, logging, os, re, struct, subprocess, sys, threading, time

import numpy as np

log = logging.getLogger("voice.tts")

KOKORO, CHATTERBOX = "kokoro", "chatterbox"
ENGINES = (KOKORO, CHATTERBOX)
# Tags Front may use with Chatterbox. Chatterbox Turbo knows a few more; these sound natural.
FRONT_TAGS = ("[laugh]", "[chuckle]", "[sigh]")
_TAG = re.compile(r"\s*\[(?:[a-z][a-z -]{0,22}[a-z])\]")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatterbox_worker.py")
RESTART_BACKOFF_S = 60.0

_LANG = {"a": "US", "b": "UK", "e": "Spanish", "f": "French", "h": "Hindi", "i": "Italian",
         "j": "Japanese", "p": "Brazilian Portuguese", "z": "Mandarin"}


def strip_tags(text):
    """Remove ``[laugh]``-style sound tags (for engines that would read them aloud)."""
    return re.sub(r"\s{2,}", " ", _TAG.sub("", text)).strip()


def parse_voice_id(voice):
    """``'kokoro:af_heart'`` -> ``('kokoro', 'af_heart')``; a bare id is Kokoro; bad input -> None."""
    if not isinstance(voice, str):
        return None
    voice = voice.strip()
    if not voice or len(voice) > 120:
        return None
    engine, sep, name = voice.partition(":")
    if not sep:
        return KOKORO, voice
    engine = engine.strip().lower()
    if engine not in ENGINES or not name.strip():
        return None
    return engine, name.strip()


def resolve_voice(provider, voice):
    """A provider's canonical id for ``voice`` or None. Providers without a catalog
    (test fakes, older providers) accept exactly the ids in ``provider.voices``."""
    if hasattr(provider, "resolve_voice"):
        return provider.resolve_voice(voice)
    return voice if voice in provider.voices else None


def kokoro_label(name):
    if len(name) < 4 or name[2] != "_" or name[0] not in _LANG or name[1] not in "fm":
        return name
    pretty = " ".join(p.capitalize() for p in name[3:].split("_"))
    return f"{pretty} ({_LANG[name[0]]} {'female' if name[1] == 'f' else 'male'})"


def resample(samples, src, dst):
    """Linear resampling of a float mono signal (enough for speech between 22.05/24/48 kHz)."""
    if src == dst or len(samples) == 0:
        return samples
    n = max(1, int(round(len(samples) * dst / src)))
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


def to_pcm16(samples):
    return (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()


class ChatterboxEngine:
    """Chatterbox Turbo behind a small request/response pipe (or in-process).

    ``synthesize(text, name) -> (float32 samples, sample_rate)``; blocking, so
    callers run it in an executor. Thread-safe (one request at a time).
    """

    def __init__(self, device, python=None, voices_dir=None, spawn=None, timeout=30.0):
        self.device, self.python, self.voices_dir = device, python, voices_dir
        self.timeout = timeout
        self.start_timeout = max(timeout, float(os.environ.get("VOICE_CHATTERBOX_START_TIMEOUT_S", "180")))
        self._spawn = spawn or self._spawn_process
        self._lock = threading.Lock()
        self._proc = None
        self._inproc = None
        self._dead_since = None
        self.voices, self.sample_rate = [], 24000
        self._start()

    # --- lifecycle -----------------------------------------------------
    def _spawn_process(self):
        env = dict(os.environ, CHATTERBOX_DEVICE=self.device, CHATTERBOX_VOICES_DIR=self.voices_dir or "")
        return subprocess.Popen([self.python, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, env=env)

    def _watchdog(self, seconds):
        """Kill the worker if it doesn't answer in time (a hung GPU would otherwise
        block readline forever while holding the lock, stalling every reply)."""
        proc = self._proc
        timer = threading.Timer(seconds, lambda: proc is not None and proc.poll() is None and proc.kill())
        timer.daemon = True
        timer.start()
        return timer

    def _start(self):
        if self.python:
            self._proc = self._spawn()
            timer = self._watchdog(self.start_timeout)
            try:
                ready = self._read_header()
            finally:
                timer.cancel()
            if not ready.get("ready"):
                self.close()
                raise RuntimeError(ready.get("error") or "chatterbox worker did not start")
        else:
            from . import chatterbox_worker
            self._inproc = chatterbox_worker.Synth(self.device, self.voices_dir)
            ready = self._inproc.ready()
        self.voices = list(ready.get("voices") or ["default"])
        self.sample_rate = int(ready.get("sr") or 24000)
        self._dead_since = None

    def close(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(5)
            except Exception:
                pass

    # --- pipe protocol: one JSON line out; one JSON header line + raw PCM back ---
    def _read_header(self):
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("chatterbox worker exited")
        return json.loads(line)

    def synthesize(self, text, name):
        with self._lock:
            if self._inproc is not None:
                samples, sr = self._inproc.synthesize(text, name)
                return np.asarray(samples, dtype=np.float32), int(sr)
            if self._proc is None or self._proc.poll() is not None:
                # One restart attempt per backoff window; in between, fail fast (Kokoro speaks).
                if self._dead_since is not None and time.monotonic() - self._dead_since < RESTART_BACKOFF_S:
                    raise RuntimeError("chatterbox worker is down")
                self.close()
                try:
                    self._start()
                except Exception:
                    self._dead_since = time.monotonic()
                    raise
            timer = self._watchdog(self.timeout)
            try:
                self._proc.stdin.write((json.dumps({"text": text, "voice": name}) + "\n").encode())
                self._proc.stdin.flush()
                head = self._read_header()
                if head.get("ok"):
                    data = self._proc.stdout.read(int(head["bytes"]))
                    if len(data) != int(head["bytes"]):
                        raise RuntimeError("chatterbox worker sent short audio")
            except Exception as error:     # the pipe is broken, out of step or timed out: drop the worker
                timer.cancel()
                self.close()
                self._dead_since = time.monotonic()
                raise RuntimeError(f"chatterbox worker failed: {error}") from error
            timer.cancel()
            if not head.get("ok"):         # the worker is fine; this one utterance failed
                raise RuntimeError(head.get("error") or "chatterbox synthesis failed")
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
            return samples, int(head.get("sr") or self.sample_rate)


class Catalog:
    """Both engines' voices, id resolution, and synthesis with Kokoro fallback.

    ``kokoro`` needs ``voices`` (bare names) and ``synthesize(text, name) -> (samples, sr)``;
    ``chatterbox`` the same plus ``sample_rate`` (or None when not loaded).
    """

    def __init__(self, kokoro, kokoro_default, chatterbox=None, chatterbox_error=None, default=None, target_sr=None):
        self.kokoro, self.chatterbox = kokoro, chatterbox
        self.target_sr = target_sr     # Chatterbox audio is resampled to this rate (Kokoro's) when it differs
        self.engines = {KOKORO: {"available": True}, CHATTERBOX: {"available": chatterbox is not None}}
        if chatterbox_error:
            self.engines[CHATTERBOX]["error"] = str(chatterbox_error)[:200]
        self.kokoro_voices = sorted(kokoro.voices)
        self.kokoro_default = kokoro_default if kokoro_default in self.kokoro_voices else self.kokoro_voices[0]
        entries = [{"id": f"{KOKORO}:{v}", "engine": KOKORO, "name": v, "label": kokoro_label(v)}
                   for v in self.kokoro_voices]
        if chatterbox is not None:
            entries += [{"id": f"{CHATTERBOX}:{v}", "engine": CHATTERBOX, "name": v,
                         "label": "Chatterbox " + ("(default)" if v == "default" else v.replace("_", " ").title())}
                        for v in chatterbox.voices]
        self.entries = entries
        self.voices = [e["id"] for e in entries]
        self.default = self.resolve(default) or f"{KOKORO}:{self.kokoro_default}"

    def resolve(self, voice):
        """Canonical ``engine:name`` for any accepted id (legacy bare ids included), else None."""
        parsed = parse_voice_id(voice)
        if parsed is None:
            return None
        canonical = f"{parsed[0]}:{parsed[1]}"
        return canonical if canonical in self.voices else None

    def engine_of(self, voice):
        parsed = parse_voice_id(self.resolve(voice) or "")
        return parsed[0] if parsed else KOKORO

    def supports_tags(self, voice):
        return self.engine_of(voice) == CHATTERBOX

    def describe(self):
        return {"voices": self.voices, "default": self.default, "catalog": self.entries, "engines": self.engines}

    def synthesize(self, text, voice, on_fallback=None):
        """Blocking. Returns (int16 PCM bytes, sample rate)."""
        canonical = self.resolve(voice) or self.default
        engine, name = parse_voice_id(canonical)
        if engine == CHATTERBOX:
            try:
                samples, sr = self.chatterbox.synthesize(text, name)
                if len(samples) == 0:
                    raise RuntimeError("chatterbox returned no audio")
                if self.target_sr and int(sr) != self.target_sr:
                    samples, sr = resample(samples, int(sr), self.target_sr), self.target_sr
                return to_pcm16(samples), int(sr)
            except Exception as error:
                log.warning("chatterbox failed, speaking with kokoro: %s", error)
                if on_fallback:
                    on_fallback(error)
                name = self.kokoro_default
        samples, sr = self.kokoro.synthesize(strip_tags(text), name)
        return to_pcm16(samples), int(sr)


def load_chatterbox(environ=None):
    """(engine, None) when VOICE_CHATTERBOX_DEVICE is set and the model loads; (None, reason) otherwise."""
    environ = os.environ if environ is None else environ
    device = environ.get("VOICE_CHATTERBOX_DEVICE", "").strip()
    if not device:
        return None, None
    try:
        engine = ChatterboxEngine(device, environ.get("VOICE_CHATTERBOX_PYTHON", "").strip() or None,
                                  environ.get("VOICE_CHATTERBOX_VOICES_DIR", "").strip() or None)
        log.info("chatterbox loaded on %s: voices %s", device, engine.voices)
        return engine, None
    except Exception as error:
        log.warning("chatterbox not loaded (%s); only kokoro voices are offered", error)
        return None, error
