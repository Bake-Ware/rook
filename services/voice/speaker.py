"""Speaker identification for voice turns: shadow mode, opt-in enrollment, echo guard.

A turn's audio (16 kHz mono PCM16 after VAD) becomes a 192-number voice
embedding (NeMo TitaNet through sherpa-onnx, CPU, no torch). ``identify``
compares it with

* the voiceprints of people who enrolled (opt-in, per principal), and
* the reference embedding of the assistant's own TTS voice (``learn_assistant_voice``),

and labels the turn ``<principal>``, ``guest`` or ``assistant`` (its own voice
coming back through a speaker: echo). The result is advisory: the server logs
it per turn and never gates on it. A voice match is not authentication.

The model runs in its own Python (``VOICE_SPEAKER_PYTHON``, a venv with
sherpa-onnx) through ``speaker_worker.py``, the same pattern as Chatterbox.
Without ``VOICE_SPEAKER_PYTHON`` the worker code is imported in-process (only if
sherpa-onnx is importable here). Without ``VOICE_SPEAKER_MODEL``, or if loading
fails, the feature is off: ``available`` is False, every call returns
None/False, and that is logged once. Nothing here ever raises into a turn.

Voiceprints, pending enrollment samples and assistant references live only in
the SQLite file ``db_path`` (mode 0600) on the voice host. Embeddings are
stored, never audio.

Thresholds (cosine similarity, TitaNet small) come from measurements on kaiju,
2026-10-22; see services/voice/README.md "Speaker identification".
"""
import asyncio, importlib.util, inspect, json, logging, os, sqlite3, subprocess, threading, time

import numpy as np

log = logging.getLogger("voice.speaker")

SAMPLE_RATE = 16000
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speaker_worker.py")

MIN_SECONDS = 0.6            # shorter audio gives no embedding (identify -> None)
MAX_SECONDS = 10.0           # longer audio is cut to its first 10 s (latency and worker RSS grow with length)
ENROLL_MIN_SECONDS = 1.5     # an enrollment sample must be at least this long ...
ENROLL_MIN_DBFS = -45.0      # ... with speech at least this loud (RMS of the voiced 20 ms frames)
ENROLL_MAX_CLIPPED = 0.01    # ... and at most 1% of samples at full scale
ENROLL_CONSISTENCY = 0.35    # a sample this far from the person's earlier samples is refused
SAMPLES_NEEDED = 3
RESERVED = frozenset({"guest", "assistant", "unmapped"})
MAX_PENDING = 8              # pending samples kept per person (oldest dropped)
PENDING_TTL_S = 24 * 3600    # abandoned enrollments are pruned at startup

# Measured defaults (kaiju, 2026-10-22, TitaNet small; README "Speaker identification"):
# an enrolled print scored 0.43-0.92 against the same person's held-out speech and at
# most 0.32 against other people (0.44 against TTS voices). The assistant's
# multi-condition reference scored >= 0.59 (p5) on its own voice through simulated
# phone/room channels and at most 0.43 against people.
DEFAULT_THRESHOLD = 0.45
DEFAULT_ASSISTANT_THRESHOLD = 0.50
TIMEOUT_S = 2.0

# The clip bank (Bake, 2026-10-27: "a SAMPLE BANK, not one averaged voiceprint"). Each
# person keeps up to BANK_MAX clips: the enrollment, pinned and never evicted, plus clips
# learned from their own turns, tagged with the device and setting they came from. A turn
# scores against its BANK_TOP_K closest clips, never lower than against the enrollment
# and never more than BANK_MAX_LIFT above it, so learned clips can only widen the match a
# bounded amount (one clip is at most one of the closest three).
BANK_MAX = int(os.environ.get("VOICE_SPEAKER_BANK_MAX", "40") or 40)
BANK_TOP_K = 3
BANK_MAX_LIFT = float(os.environ.get("VOICE_SPEAKER_BANK_MAX_LIFT", "0.20") or 0.20)
# Learning (Bake, 2026-10-27): only clips at least LEARN_MIN close to the enrollment, of
# usable quality (LEARN_MIN_SECONDS of speech, the enrollment's level and clipping limits,
# at least LEARN_MIN_SNR_DB over the noise floor), that nobody else matches as well.
LEARN_MIN = float(os.environ.get("VOICE_SPEAKER_LEARN_MIN", "0.30") or 0.30)
LEARN_MIN_SECONDS = 1.0
LEARN_MIN_SNR_DB = 6.0
LEARN_DUPLICATE = 0.98       # a clip this close to one already banked adds nothing
NOISY_SNR_DB = 15.0          # below this a clip's setting is "noisy"
# Cohort normalization: a turn's score is also compared with how well it matches voices
# that are not the person (strangers heard on the owner's phone, the assistant's voices,
# other enrolled people): z = (score - mean) / sd over its COHORT_TOP closest. Used once
# the cohort has COHORT_MIN voices: z >= NORM_SURE with a score of at least LEARN_MIN
# names the person even under the threshold (a noisy channel lowers every score alike).
COHORT_MAX = 60
COHORT_MIN = 8
COHORT_TOP = 10
NORM_SURE = float(os.environ.get("VOICE_SPEAKER_NORM_SURE", "3.0") or 3.0)
START_TIMEOUT_S = 30.0
RESTART_BACKOFF_S = 60.0

ENROLLMENT_SENTENCES = [
    "Hey Rook, it's me. I'm setting up my voice so you know who's talking.",
    "This morning I made coffee, checked the weather and fed the dog before work.",
    "Could you remind me to call my sister on Thursday around six in the evening?",
    "The quick brown fox jumps over the lazy dog, and then it naps in the sun.",
]

# What the assistant's TTS voice says to build its reference embedding: varied
# prosody (statement, question, list, exclamation), 2-4 s each.
ASSISTANT_SENTENCES = [
    "Sure, I've set a timer for ten minutes, and I'll let you know when it's done.",
    "Do you want me to turn off the lights in the kitchen and the living room?",
    "Tomorrow looks cloudy in the morning, with a good chance of rain after lunch.",
    "Oh, that's great news! I'll add it to your calendar for next Friday.",
]


# --- audio helpers ---------------------------------------------------------------
def pcm_to_float(pcm):
    """Little-endian int16 bytes -> float32 in [-1, 1)."""
    pcm = bytes(pcm or b"")
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768


def as_float(samples):
    """int16 bytes / int16 array / float array -> float32 array."""
    if isinstance(samples, (bytes, bytearray, memoryview)):
        return pcm_to_float(samples)
    x = np.asarray(samples)
    if x.ndim > 1:
        x = x.reshape(-1) if 1 in x.shape else x.mean(axis=-1 if x.shape[-1] < x.shape[0] else 0)
    if np.issubdtype(x.dtype, np.integer):
        return x.astype(np.float32) / 32768
    return x.astype(np.float32)


def to_16k(samples, sr):
    """Resample float mono audio to 16 kHz (windowed-sinc low-pass before decimation)."""
    x = as_float(samples)
    sr = int(sr)
    if sr == SAMPLE_RATE or x.size == 0:
        return x
    if sr > SAMPLE_RATE:
        taps = max(31, int(round(16 * sr / SAMPLE_RATE))) | 1
        fc = 0.45 * SAMPLE_RATE / sr                       # cutoff 7.2 kHz, as a fraction of the source rate
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
        x = np.convolve(x, (h / h.sum()).astype(np.float32), mode="same")
    count = max(1, int(round(x.size * SAMPLE_RATE / sr)))
    return np.interp(np.linspace(0, x.size - 1, count), np.arange(x.size), x).astype(np.float32)


def _shape(x, lo=None, hi=None, order=4):
    """Zero-phase Butterworth-shaped band limit applied in the FFT domain."""
    spectrum = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.size, 1 / SAMPLE_RATE)
    gain = np.ones_like(f)
    if lo:
        gain /= np.sqrt(1 + (lo / np.maximum(f, 1e-3)) ** (2 * order))
    if hi:
        gain /= np.sqrt(1 + (f / hi) ** (2 * order))
    return np.fft.irfft(spectrum * gain, x.size).astype(np.float32)


def _noise(x, snr_db, rng):
    power = float(np.mean(x.astype(np.float64) ** 2))
    return (x + rng.normal(0, np.sqrt(power / 10 ** (snr_db / 10)), x.size)).astype(np.float32)


def _room(x, rng, rt60=0.4):
    t = np.arange(int(rt60 * SAMPLE_RATE)) / SAMPLE_RATE
    ir = rng.normal(0, 1, t.size) * np.exp(-6.9 * t / rt60)
    ir *= 0.3 / np.sqrt(np.sum(ir ** 2))
    ir[0] = 1.0
    return np.convolve(x, ir)[: x.size].astype(np.float32)


def echo_views(samples, seed=0):
    """The clean 16 kHz render plus how it may come back as echo: a phone channel
    (300-3400 Hz, 8 kHz round trip, -10 dB, 15 dB SNR) and a loudspeaker in a room
    (300 Hz high-pass, ~0.4 s reverb, 20 dB SNR). The assistant reference averages
    all of them: a clean-only reference scored real echo too low (README)."""
    x = as_float(samples)
    if x.size < 2:
        return [x]
    rng = np.random.default_rng(seed)
    quiet = x * 10 ** (-10 / 20)
    narrow = _shape(quiet, 300, 3400)
    half = np.interp(np.linspace(0, x.size - 1, x.size // 2), np.arange(x.size), narrow)
    phone = np.interp(np.linspace(0, half.size - 1, x.size), np.arange(half.size), half).astype(np.float32)
    room = _room(_shape(quiet, 300, None, order=2), rng)
    return [x, _noise(phone, 15, rng), _noise(room, 20, rng)]


def quality(samples):
    """Duration, speech level and clipping of float 16 kHz audio."""
    x = as_float(samples)
    seconds = x.size / SAMPLE_RATE
    n = 320                                                 # 20 ms
    frames = x[: x.size // n * n].reshape(-1, n) if x.size >= n else x.reshape(1, -1)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) if x.size else np.zeros(1)
    voiced = rms[rms > 10 ** (-55 / 20)]
    level = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else 0.0
    dbfs = round(float(20 * np.log10(level)), 1) if level > 0 else -120.0
    clipped = float(np.mean(np.abs(x) >= 0.999)) if x.size else 0.0
    # Noise floor: the quietest tenth of the frames; speech: the loudest tenth.
    snr = (round(float(20 * np.log10(max(np.percentile(rms, 90), 1e-9) / max(np.percentile(rms, 10), 1e-9))), 1)
           if x.size else 0.0)
    return {"seconds": round(seconds, 2), "dbfs": dbfs, "clipped": round(clipped, 4), "snr_db": snr}


def setting_for(device, q):
    """A clip's setting tag: car or watch by the device's name, else noisy or quiet."""
    name = str(device or "").lower()
    if "car" in name:
        return "car"
    if "watch" in name or "wear" in name:
        return "watch"
    return "noisy" if (q or {}).get("snr_db", 99.0) < NOISY_SNR_DB else "quiet"


def normalize(vector):
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


def cosine(a, b):
    return float(np.dot(normalize(a), normalize(b)))


def mean_print(vectors):
    """L2-normalised mean of L2-normalised embeddings."""
    return normalize(np.mean([normalize(v) for v in vectors], axis=0))


# --- embedders ---------------------------------------------------------------------
class WorkerEmbedder:
    """``speaker_worker.py`` in another Python, behind a JSON-line + raw PCM pipe.

    ``embed(float32 16 kHz samples, timeout) -> list[float]``; blocking and
    thread-safe (one request at a time). A worker that dies or hangs is killed
    and restarted at most once per RESTART_BACKOFF_S.
    """

    def __init__(self, python, model, threads=2, spawn=None):
        self.python, self.model_path, self.threads = python, model, int(threads)
        self._spawn = spawn or self._spawn_process
        self._lock = threading.Lock()
        self._proc = None
        self._dead_since = None
        self.model, self.dim = os.path.basename(model or ""), 0
        self._start()

    def _spawn_process(self):
        env = dict(os.environ, SPEAKER_MODEL=self.model_path, SPEAKER_THREADS=str(self.threads),
                   OMP_NUM_THREADS=str(self.threads))
        return subprocess.Popen([self.python, "-I", WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, env=env)

    def _watchdog(self, seconds):
        proc = self._proc
        timer = threading.Timer(seconds, lambda: proc is not None and proc.poll() is None and proc.kill())
        timer.daemon = True
        timer.start()
        return timer

    def _read_line(self):
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("speaker worker exited")
        return json.loads(line)

    def _start(self):
        self._proc = self._spawn()
        timer = self._watchdog(START_TIMEOUT_S)
        try:
            ready = self._read_line()
        except Exception:
            self.close()
            raise
        finally:
            timer.cancel()
        if not ready.get("ready"):
            self.close()
            raise RuntimeError(ready.get("error") or "speaker worker did not start")
        self.model = str(ready.get("model") or self.model)
        self.dim = int(ready.get("dim") or 0)
        self._dead_since = None

    def close(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(5)
            except Exception:
                pass

    def embed(self, samples, timeout=TIMEOUT_S):
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("speaker worker busy")
        try:
            if self._proc is None or self._proc.poll() is not None:
                if self._dead_since is not None and time.monotonic() - self._dead_since < RESTART_BACKOFF_S:
                    raise RuntimeError("speaker worker is down")
                self.close()
                try:
                    self._start()
                except Exception:
                    self._dead_since = time.monotonic()
                    raise
            remaining = deadline - time.monotonic()
            if remaining < 0.25:       # waited for the lock too long: skip rather than kill a healthy worker
                raise TimeoutError("speaker worker busy")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            timer = self._watchdog(remaining)
            try:
                self._proc.stdin.write((json.dumps({"bytes": len(pcm)}) + "\n").encode() + pcm)
                self._proc.stdin.flush()
                head = self._read_line()
            except Exception as error:     # broken pipe, out of step or timed out: drop the worker
                timer.cancel()
                self.close()
                self._dead_since = time.monotonic()
                raise RuntimeError(f"speaker worker failed: {error}") from error
            timer.cancel()
            if not head.get("ok"):
                raise RuntimeError(head.get("error") or "speaker embedding failed")
            return head["embedding"]
        finally:
            self._lock.release()


class InProcessEmbedder:
    """``speaker_worker.Embedder`` loaded here (needs sherpa-onnx in this venv)."""

    def __init__(self, model, threads=2):
        spec = importlib.util.spec_from_file_location("_rook_speaker_worker", WORKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._impl = module.Embedder(model, threads)
        self.model, self.dim = self._impl.model, self._impl.dim
        self._lock = threading.Lock()

    def embed(self, samples, timeout=TIMEOUT_S):
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("speaker embedder busy")
        try:
            return self._impl.embed(np.asarray(samples, dtype=np.float32))
        finally:
            self._lock.release()

    def close(self):
        pass


def _env_float(name, default):
    try:
        value = os.environ.get(name, "").strip()
        return float(value) if value else default
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


# --- the store + API --------------------------------------------------------------
class Speakers:
    """Speaker identification (shadow mode), enrollment store and echo guard.

    ``python``/``model`` default to ``VOICE_SPEAKER_PYTHON``/``VOICE_SPEAKER_MODEL``,
    thresholds to ``VOICE_SPEAKER_THRESHOLD``/``VOICE_SPEAKER_ASSISTANT_THRESHOLD``.
    ``embedder`` (tests) is any object with ``embed(float32 16 kHz samples[, timeout])
    -> sequence of floats`` and optionally a ``model`` name. Construction starts the
    worker and waits for it (well under a second for TitaNet small).
    """

    def __init__(self, db_path, *, python=None, model=None, threshold=None, assistant_threshold=None,
                 embedder=None, threads=None, timeout=TIMEOUT_S):
        env = os.environ
        self.python = (python if python is not None else env.get("VOICE_SPEAKER_PYTHON", "")).strip() or None
        self.model_path = (model if model is not None else env.get("VOICE_SPEAKER_MODEL", "")).strip() or None
        self.threshold = float(threshold) if threshold is not None else _env_float("VOICE_SPEAKER_THRESHOLD", DEFAULT_THRESHOLD)
        self.assistant_threshold = (float(assistant_threshold) if assistant_threshold is not None else
                                    _env_float("VOICE_SPEAKER_ASSISTANT_THRESHOLD", DEFAULT_ASSISTANT_THRESHOLD))
        self.threads = int(threads or env.get("VOICE_SPEAKER_THREADS", "") or 2)
        self.timeout = float(timeout)
        self.error = None
        self._embedder = None
        self._db_lock = threading.Lock()
        self._db = None
        self._prints = {}            # principal -> normalised enrollment print, the anchor (current model only)
        self._banks = {}             # principal -> [clip dicts: id, vec, pinned, score, device, setting, created]
        self._cohort = []            # [(id, vec)] voices that are nobody enrolled, oldest first
        self._assistant = {}         # voice id -> normalised reference (current model only)
        self._learning = {}          # voice id -> asyncio.Task (one learn per voice at a time)
        self._takes_timeout = None
        self._warned_at, self._suppressed = 0.0, 0

        if embedder is not None:
            self._embedder = embedder
        elif not self.model_path:
            self.error = "VOICE_SPEAKER_MODEL not set"
            log.info("speaker identification off: %s", self.error)
        else:
            try:
                self._embedder = (WorkerEmbedder(self.python, self.model_path, self.threads) if self.python
                                  else InProcessEmbedder(self.model_path, self.threads))
                log.info("speaker identification on (shadow): %s, %d threads, %s",
                         getattr(self._embedder, "model", "?"), self.threads,
                         "worker " + self.python if self.python else "in-process")
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"[:300]
                self._embedder = None
                log.warning("speaker identification off: %s", self.error)
        self.model = str(getattr(self._embedder, "model", "") or "embedder") if self._embedder else None

        try:
            self._open_db(db_path)
        except Exception as error:   # no store: identification still can't label anyone, so turn it off
            log.warning("speaker identification off: store %s unusable: %s", db_path, error)
            self.error = self.error or f"store unusable: {error}"[:300]
            self.close()

    # --- lifecycle / state --------------------------------------------------------
    @property
    def available(self):
        return self._embedder is not None and self._db is not None

    def close(self):
        embedder, self._embedder = self._embedder, None
        if embedder is not None and hasattr(embedder, "close"):
            try:
                embedder.close()
            except Exception:
                pass

    def _open_db(self, path):
        path = os.fspath(path)
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA secure_delete=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS voiceprints (principal TEXT NOT NULL, model TEXT NOT NULL,
                embedding BLOB NOT NULL, samples INTEGER NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY (principal, model));
            CREATE TABLE IF NOT EXISTS enroll_samples (id INTEGER PRIMARY KEY AUTOINCREMENT,
                principal TEXT NOT NULL, model TEXT NOT NULL, embedding BLOB NOT NULL,
                quality TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS assistant_voices (voice TEXT NOT NULL, model TEXT NOT NULL,
                embedding BLOB NOT NULL, updated REAL NOT NULL, PRIMARY KEY (voice, model));
            CREATE TABLE IF NOT EXISTS voice_clips (id INTEGER PRIMARY KEY AUTOINCREMENT,
                principal TEXT NOT NULL, model TEXT NOT NULL, embedding BLOB NOT NULL,
                source TEXT NOT NULL, pinned INTEGER NOT NULL, device TEXT NOT NULL, setting TEXT NOT NULL,
                score REAL NOT NULL, quality TEXT NOT NULL, session TEXT NOT NULL, created REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, ended TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS cohort_clips (id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL, embedding BLOB NOT NULL, source TEXT NOT NULL, created REAL NOT NULL);
        """)
        db.execute("DELETE FROM enroll_samples WHERE created < ?", (time.time() - PENDING_TTL_S,))
        self._db = db
        if self.model:
            for principal, blob in db.execute("SELECT principal, embedding FROM voiceprints WHERE model=?", (self.model,)):
                self._prints[principal] = normalize(np.frombuffer(blob, dtype=np.float32))
            for voice, blob in db.execute("SELECT voice, embedding FROM assistant_voices WHERE model=?", (self.model,)):
                self._assistant[voice] = normalize(np.frombuffer(blob, dtype=np.float32))
            for principal, voiceprint in self._prints.items():
                # Prints enrolled before the bank: the print itself becomes the pinned clip.
                if not db.execute("SELECT 1 FROM voice_clips WHERE principal=? AND model=? AND pinned=1",
                                  (principal, self.model)).fetchone():
                    db.execute("INSERT INTO voice_clips (principal, model, embedding, source, pinned, device, setting, "
                               "score, quality, session, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (principal, self.model, voiceprint.astype(np.float32).tobytes(), "enroll", 1,
                                "enrollment", "enrollment", 1.0, "{}", "", time.time()))
            self._load_banks(db)
            self._cohort = [(i, normalize(np.frombuffer(b, dtype=np.float32))) for i, b in db.execute(
                "SELECT id, embedding FROM cohort_clips WHERE model=? ORDER BY id", (self.model,))]
            stale = [r[0] for r in db.execute("SELECT DISTINCT principal FROM voiceprints WHERE model<>?", (self.model,))
                     if r[0] not in self._prints]
            if stale:
                log.warning("speaker: %d voiceprint(s) were made with another model and are ignored until re-enrolled",
                            len(stale))

    def _load_banks(self, db=None):
        db = db or self._db
        banks = {}
        for cid, principal, blob, pinned, score, device, setting, created in db.execute(
                "SELECT id, principal, embedding, pinned, score, device, setting, created FROM voice_clips "
                "WHERE model=? AND active=1 ORDER BY id", (self.model,)):
            if principal in self._prints:
                banks.setdefault(principal, []).append({
                    "id": cid, "vec": normalize(np.frombuffer(blob, dtype=np.float32)), "pinned": bool(pinned),
                    "score": score, "device": device, "setting": setting, "created": created})
        self._banks = banks

    def _sql(self, query, args=()):
        with self._db_lock:
            return self._db.execute(query, args).fetchall()

    # --- embeddings ------------------------------------------------------------------
    def _embed_blocking(self, samples):
        embedder = self._embedder
        if embedder is None:
            return None
        if self._takes_timeout is None:
            try:
                self._takes_timeout = "timeout" in inspect.signature(embedder.embed).parameters
            except (TypeError, ValueError):
                self._takes_timeout = False
        vector = embedder.embed(samples, timeout=self.timeout) if self._takes_timeout else embedder.embed(samples)
        vector = normalize(vector)
        if vector.size == 0 or not np.all(np.isfinite(vector)):
            raise ValueError("embedding is empty or not finite")
        return vector

    async def _embed_samples(self, samples):
        """(normalised vector, ms) or (None, ms). Bounded by ``timeout``; never raises."""
        started = time.perf_counter()
        if not self.available or samples.size < int(MIN_SECONDS * SAMPLE_RATE):
            return None, 0
        samples = samples[: int(MAX_SECONDS * SAMPLE_RATE)]
        try:
            loop = asyncio.get_running_loop()
            vector = await asyncio.wait_for(loop.run_in_executor(None, self._embed_blocking, samples), self.timeout)
        except Exception as error:
            self._warn(f"speaker embedding failed: {type(error).__name__}: {error}"[:200])
            vector = None
        return vector, int((time.perf_counter() - started) * 1000)

    def _warn(self, message):
        """At most one failure line a minute: a dead worker must not flood the journal."""
        now = time.monotonic()
        if now - self._warned_at < RESTART_BACKOFF_S:
            self._suppressed += 1
            return
        extra = f" ({self._suppressed} more since the last report)" if self._suppressed else ""
        self._warned_at, self._suppressed = now, 0
        log.warning("%s%s", message, extra)

    async def embed(self, pcm):
        """Embedding of 16 kHz PCM16 audio as a list, or None (off, < 0.6 s, failed or timed out)."""
        vector, _ = await self._embed_samples(pcm_to_float(pcm))
        return None if vector is None else [float(v) for v in vector]

    # --- identification --------------------------------------------------------------
    async def identify(self, pcm, assistant_voice=None):
        """Who spoke this turn (advisory). None when no embedding could be made.

        ``{'label': principal | 'guest' | 'assistant', 'score': best enrolled cosine or None,
        'principal': matched name or None, 'assistant_score': cosine vs the assistant voice
        reference or None, 'scores': each enrolled person's bank score, 'norm': the best match's
        cohort-normalized score or None, 'clip': the best match's closest clip, 'ms': embedding time}``.
        ``candidate`` names the best enrolled match even below the threshold (for shadow logs).
        """
        vector, ms = await self._embed_samples(pcm_to_float(pcm))
        if vector is None:
            return None
        best, score, scores, closest = None, None, {}, None
        for principal, voiceprint in list(self._prints.items()):
            if voiceprint.shape != vector.shape:
                continue
            value, clip = self._score(principal, vector)
            scores[principal] = value
            if score is None or value > score:
                best, score, closest = principal, value, clip
        reference = self._assistant.get(assistant_voice) if assistant_voice else None
        assistant_score = (float(np.dot(vector, reference))
                           if reference is not None and reference.shape == vector.shape else None)
        norm = self._norm(vector, score, best) if best is not None else None
        principal = best if score is not None and (
            score >= self.threshold or (norm is not None and norm >= NORM_SURE and score >= LEARN_MIN)) else None
        if (assistant_score is not None and assistant_score >= self.assistant_threshold
                and (score is None or assistant_score > score)):
            label, principal = "assistant", None
        else:
            label = principal or "guest"
        return {"label": label, "score": None if score is None else round(score, 4), "principal": principal,
                "assistant_score": None if assistant_score is None else round(assistant_score, 4),
                "candidate": best, "scores": {p: round(v, 4) for p, v in scores.items()}, "norm": norm,
                "clip": None if closest is None else {k: closest[k] for k in ("id", "pinned", "device", "setting")},
                "ms": ms, "vector": vector}

    # --- echo guard: the assistant's own voice ---------------------------------------
    def _score(self, principal, vector):
        """(score, closest clip): the mean of the BANK_TOP_K closest clips' cosines, at least
        the enrollment print's own and at most BANK_MAX_LIFT above it."""
        base = float(np.dot(vector, self._prints[principal]))
        sims = sorted(((float(np.dot(vector, c["vec"])), c) for c in self._banks.get(principal, ())
                       if c["vec"].shape == vector.shape), key=lambda t: -t[0])[:BANK_TOP_K]
        if not sims:
            return base, None
        top = sum(v for v, _ in sims) / len(sims)
        return max(base, min(top, base + BANK_MAX_LIFT)), sims[0][1]

    def _norm(self, vector, score, principal):
        cohort = [v for _, v in self._cohort] + list(self._assistant.values()) + [
            p for name, p in self._prints.items() if name != principal]
        sims = [float(np.dot(vector, c)) for c in cohort if c.shape == vector.shape]
        if len(sims) < COHORT_MIN or score is None:
            return None
        top = np.sort(np.asarray(sims))[-COHORT_TOP:]
        return round((score - float(np.mean(top))) / max(float(np.std(top)), 0.05), 3)

    def has_assistant_voice(self, voice):
        return voice in self._assistant

    async def learn_assistant_voice(self, voice, synthesize):
        """Make (or load) the reference embedding of TTS voice ``voice``.

        ``synthesize(text)`` (async or plain) returns ``(samples, sample_rate)``;
        samples may be float or int16 arrays or int16 PCM bytes. Renders
        ASSISTANT_SENTENCES, embeds each at 16 kHz clean and through simulated
        echo channels (``echo_views``) and stores the normalised mean in the db (reused after restarts; recomputed when missing or the model
        changed). True when a reference is available.
        """
        if not self.available or not voice:
            return False
        if voice in self._assistant:
            return True
        task = self._learning.get(voice)
        if task is None:
            task = self._learning[voice] = asyncio.ensure_future(self._learn(voice, synthesize))
            task.add_done_callback(lambda _t, v=voice: self._learning.pop(v, None))
        try:
            return await asyncio.shield(task)
        except Exception:
            return False

    async def _learn(self, voice, synthesize):
        try:
            rows = self._sql("SELECT embedding FROM assistant_voices WHERE voice=? AND model=?", (voice, self.model))
            if rows:
                self._assistant[voice] = normalize(np.frombuffer(rows[0][0], dtype=np.float32))
                return True
            vectors = []
            for index, text in enumerate(ASSISTANT_SENTENCES):
                try:
                    result = synthesize(text)
                    if inspect.isawaitable(result):
                        result = await result
                    samples, sr = result
                    views = echo_views(to_16k(samples, sr), seed=index)
                except Exception as error:
                    log.warning("speaker: rendering %r for its reference failed: %s", voice, str(error)[:200])
                    continue
                for view in views:
                    vector, _ = await self._embed_samples(view)
                    if vector is not None:
                        vectors.append(vector)
            if len(vectors) < 4:
                log.warning("speaker: no reference for voice %r (%d usable renders)", voice, len(vectors))
                return False
            reference = mean_print(vectors)
            self._sql("INSERT OR REPLACE INTO assistant_voices (voice, model, embedding, updated) VALUES (?,?,?,?)",
                      (voice, self.model, reference.astype(np.float32).tobytes(), time.time()))
            self._assistant[voice] = reference
            log.info("speaker: learned assistant voice %r from %d renders", voice, len(vectors))
            return True
        except Exception as error:
            log.warning("speaker: learning voice %r failed: %s", voice, str(error)[:200])
            return False

    # --- enrollment (opt-in, per principal) ------------------------------------------
    def enrollment_sentences(self):
        return list(ENROLLMENT_SENTENCES)

    def _pending(self, principal):
        return self._sql("SELECT id, embedding FROM enroll_samples WHERE principal=? AND model=? ORDER BY id",
                         (principal, self.model))

    @staticmethod
    def _name(principal):
        """A principal name; ValueError for empty, overlong or reserved names (``guest``,
        ``assistant`` are labels; ``unmapped`` is an unauthenticated connection)."""
        name = str(principal or "").strip()
        if not name or len(name) > 120:
            raise ValueError("principal must be a non-empty name up to 120 characters")
        if name.lower() in RESERVED:
            raise ValueError(f"{name!r} cannot be enrolled")
        return name

    async def enroll_add(self, principal, pcm):
        """Add one read-aloud sample. ``{'ok', 'samples', 'needed', 'quality'}`` plus
        ``reason`` when refused (``off``, ``too_short``, ``too_quiet``, ``clipped``,
        ``inconsistent``, ``failed``)."""
        principal = self._name(principal)
        samples = pcm_to_float(pcm)
        q = quality(samples)
        pending = self._pending(principal) if self.available else []

        def answer(ok, reason=None):
            count = len(pending)
            reply = {"ok": ok, "samples": count, "needed": max(0, SAMPLES_NEEDED - count), "quality": q}
            if reason:
                reply["reason"] = reason
            return reply

        if not self.available:
            return answer(False, "off")
        if q["seconds"] < ENROLL_MIN_SECONDS:
            return answer(False, "too_short")
        if q["dbfs"] < ENROLL_MIN_DBFS:
            return answer(False, "too_quiet")
        if q["clipped"] > ENROLL_MAX_CLIPPED:
            return answer(False, "clipped")
        vector, _ = await self._embed_samples(samples)
        if vector is None:
            return answer(False, "failed")
        if pending:
            consistency = float(np.dot(vector, mean_print([np.frombuffer(b, dtype=np.float32) for _, b in pending])))
            q["consistency"] = round(consistency, 3)
            if consistency < ENROLL_CONSISTENCY:
                return answer(False, "inconsistent")
        self._sql("INSERT INTO enroll_samples (principal, model, embedding, quality, created) VALUES (?,?,?,?,?)",
                  (principal, self.model, vector.astype(np.float32).tobytes(), json.dumps(q), time.time()))
        pending = self._pending(principal)
        if len(pending) > MAX_PENDING:
            drop = [row[0] for row in pending[: len(pending) - MAX_PENDING]]
            self._sql(f"DELETE FROM enroll_samples WHERE id IN ({','.join('?' * len(drop))})", drop)
            pending = pending[len(drop):]
        return answer(True)

    def enroll_finish(self, principal):
        """Average the pending samples (at least SAMPLES_NEEDED) into the person's voiceprint,
        replacing any earlier one, and drop the samples. False when there are too few."""
        principal = self._name(principal)
        if not self.available:
            return False
        pending = self._pending(principal)
        if len(pending) < SAMPLES_NEEDED:
            return False
        voiceprint = mean_print([np.frombuffer(blob, dtype=np.float32) for _, blob in pending])
        with self._db_lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("INSERT OR REPLACE INTO voiceprints (principal, model, embedding, samples, updated) "
                                 "VALUES (?,?,?,?,?)", (principal, self.model, voiceprint.astype(np.float32).tobytes(),
                                                        len(pending), time.time()))
                # A new enrollment is a new anchor: its samples are the pinned clips, and the
                # clips banked against the old one end (kept, inactive).
                self._db.execute("UPDATE voice_clips SET active=0, ended='re-enrolled' WHERE principal=? AND active=1",
                                 (principal,))
                for _, blob in pending:
                    self._db.execute(
                        "INSERT INTO voice_clips (principal, model, embedding, source, pinned, device, setting, score, "
                        "quality, session, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (principal, self.model, blob, "enroll", 1, "enrollment", "enrollment", 1.0, "{}", "", time.time()))
                self._db.execute("DELETE FROM enroll_samples WHERE principal=?", (principal,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        self._prints[principal] = voiceprint
        self._load_banks()
        log.info("speaker: enrolled %s from %d samples", principal, len(pending))
        return True

    def forget(self, principal):
        """Delete the person's voiceprints (every model), pending samples and clips. True if any existed."""
        principal = self._name(principal)
        self._prints.pop(principal, None)
        self._banks.pop(principal, None)
        if self._db is None:
            return False
        with self._db_lock:
            removed = self._db.execute("DELETE FROM voiceprints WHERE principal=?", (principal,)).rowcount
            removed += self._db.execute("DELETE FROM enroll_samples WHERE principal=?", (principal,)).rowcount
            self._db.execute("DELETE FROM voice_clips WHERE principal=?", (principal,))
        return removed > 0

    def enrolled(self):
        """Principals with a voiceprint usable by the current model."""
        return sorted(self._prints)

    # --- learning: the bank grows from the person's own turns ---------------------------
    def learn_sample(self, principal, vector, pcm=None, device="", session=""):
        """Bank one turn of ``principal``'s as a learned clip. The caller decides that the
        turn may be theirs (an anchored connection on their own device); this checks the
        clip. ``{'ok': True, 'version', 'score', 'device', 'setting', 'evicted'}`` or
        ``{'ok': False, 'reason'}``: ``off``, ``not_enrolled``, ``quality`` (too short,
        quiet, clipped or noisy), ``too_far`` (under LEARN_MIN against the enrollment),
        ``other_higher`` (another enrolled voice matches as well) or ``duplicate``.
        When the bank is full the weakest learned clip ends (the oldest, if the new one is
        weaker still); pinned enrollment clips never do."""
        if not self.available:
            return {"ok": False, "reason": "off"}
        if principal not in self._prints or vector is None:
            return {"ok": False, "reason": "not_enrolled"}
        vector = normalize(np.asarray(vector, dtype=np.float32))
        if vector.shape != self._prints[principal].shape:
            return {"ok": False, "reason": "not_enrolled"}
        q = quality(pcm_to_float(pcm)) if pcm is not None else {}
        if pcm is not None and (q["seconds"] < LEARN_MIN_SECONDS or q["dbfs"] < ENROLL_MIN_DBFS
                                or q["clipped"] > ENROLL_MAX_CLIPPED or q["snr_db"] < LEARN_MIN_SNR_DB):
            return {"ok": False, "reason": "quality", "quality": q}
        base = float(np.dot(vector, self._prints[principal]))
        if base < LEARN_MIN:
            return {"ok": False, "reason": "too_far", "score": round(base, 4)}
        mine, _ = self._score(principal, vector)
        if any(self._score(name, vector)[0] >= mine for name, p in list(self._prints.items())
               if name != principal and p.shape == vector.shape):
            return {"ok": False, "reason": "other_higher", "score": round(base, 4)}
        bank = self._banks.get(principal, [])
        if any(float(np.dot(vector, c["vec"])) >= LEARN_DUPLICATE for c in bank):
            return {"ok": False, "reason": "duplicate", "score": round(base, 4)}
        setting = setting_for(device, q)
        learned = [c for c in bank if not c["pinned"]]
        evict = None
        if learned and len(bank) >= max(BANK_MAX, sum(c["pinned"] for c in bank) + 1):
            weakest = min(learned, key=lambda c: (c["score"], c["created"]))
            evict = weakest if base > weakest["score"] else min(learned, key=lambda c: c["created"])
        with self._db_lock:
            self._db.execute("BEGIN")
            try:
                if evict is not None:
                    self._db.execute("UPDATE voice_clips SET active=0, ended='evicted' WHERE id=?", (evict["id"],))
                version = self._db.execute(
                    "INSERT INTO voice_clips (principal, model, embedding, source, pinned, device, setting, score, "
                    "quality, session, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (principal, self.model, vector.astype(np.float32).tobytes(), "learned", 0,
                     str(device or "unknown")[:40], setting, base, json.dumps(q), str(session)[:64], time.time())
                ).lastrowid
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        self._load_banks()
        log.info("speaker: banked a clip of %s (version %d, score %.3f, %s/%s%s)", principal, version, base,
                 device or "unknown", setting, f", ended {evict['id']}" if evict else "")
        return {"ok": True, "version": version, "score": round(base, 4), "device": str(device or "unknown"),
                "setting": setting, "evicted": evict["id"] if evict else None}

    def bank(self, principal):
        """Every clip ever banked for ``principal`` (current model), oldest first:
        ``[{'version', 'source', 'pinned', 'device', 'setting', 'score', 'created', 'active', 'ended'}]``."""
        rows = self._sql("SELECT id, source, pinned, device, setting, score, created, active, ended FROM voice_clips "
                         "WHERE principal=? AND model=? ORDER BY id", (principal, self.model))
        return [{"version": r[0], "source": r[1], "pinned": bool(r[2]), "device": r[3], "setting": r[4],
                 "score": r[5], "created": r[6], "active": bool(r[7]), "ended": r[8]} for r in rows]

    def learned_rollback(self, principal, to_version=0):
        """End ``principal``'s learned clips newer than ``to_version`` (0: all; the pinned
        enrollment stays). Rows are kept, inactive. Returns how many ended. From a shell:
        ``Speakers(db).learned_rollback("Bake", 0)``."""
        if self._db is None:
            return 0
        with self._db_lock:
            count = self._db.execute("UPDATE voice_clips SET active=0, ended='rolled back' WHERE principal=? AND "
                                     "model=? AND pinned=0 AND id>? AND active=1",
                                     (principal, self.model, int(to_version))).rowcount
        self._load_banks()
        log.info("speaker: rolled back %d learned clip(s) of %s to version %s", count, principal, to_version)
        return count

    def add_cohort(self, vector, source="stranger"):
        """Remember a voice that is nobody enrolled (for cohort normalization). Near-copies
        of one already kept are skipped; the oldest go past COHORT_MAX."""
        if not self.available or vector is None:
            return False
        vector = normalize(np.asarray(vector, dtype=np.float32))
        if any(c.shape == vector.shape and float(np.dot(vector, c)) >= 0.95 for _, c in self._cohort):
            return False
        with self._db_lock:
            cid = self._db.execute("INSERT INTO cohort_clips (model, embedding, source, created) VALUES (?,?,?,?)",
                                   (self.model, vector.astype(np.float32).tobytes(), str(source)[:20],
                                    time.time())).lastrowid
            self._cohort.append((cid, vector))
            while len(self._cohort) > COHORT_MAX:
                old, _ = self._cohort.pop(0)
                self._db.execute("DELETE FROM cohort_clips WHERE id=?", (old,))
        return True


class SessionSpeakers:
    """Who is who in one overheard conversation, without enrollment: each new voice
    gets the next letter ("Speaker A", "Speaker B") by matching its embedding to the
    ones heard before in this conversation. Enrolled people keep their names."""

    def __init__(self, threshold=0.55, limit=8, sure=None):
        self.threshold, self.limit = threshold, limit
        # An enrolled name only for a sure match (VOICE_SPEAKER_SURE, as the house voice):
        # a weaker match must not count as the owner and keep a scribe going.
        self.sure = float(os.environ.get('VOICE_SPEAKER_SURE', '0.45')) if sure is None else sure
        self.centroids = []          # [(label, vector, count)]

    def label(self, result):
        if not result:
            return "Speaker"
        if result.get("principal") and (result.get("score") or 0) >= self.sure:
            return result["principal"]
        vector = result.get("vector")
        if vector is None:
            return "Speaker"
        vector = normalize(np.asarray(vector, dtype=np.float32))
        best, score = None, -1.0
        for i, (label, c, n) in enumerate(self.centroids):
            s = float(np.dot(vector, c))
            if s > score:
                best, score = i, s
        if best is not None and score >= self.threshold:
            label, c, n = self.centroids[best]
            self.centroids[best] = (label, normalize(c * n + vector), n + 1)
            return label
        if len(self.centroids) >= self.limit:
            return self.centroids[best][0] if best is not None else "Speaker"
        label = "Speaker " + "ABCDEFGH"[len(self.centroids)]
        self.centroids.append((label, vector, 1))
        return label


def shadow_record(result, turn=None):
    """One compact JSON-able dict for the per-turn shadow log (no audio, no embedding)."""
    if not result:
        return {"event": "voice_speaker", "turn": turn, "label": None}
    return {"event": "voice_speaker", "turn": turn, **{k: result.get(k) for k in
            ("label", "score", "principal", "candidate", "assistant_score", "ms")}}
