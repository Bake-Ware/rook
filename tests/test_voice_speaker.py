"""Speaker identification (services/voice/speaker.py) with a fake embedder: no sherpa-onnx, no model files."""
import asyncio
import json
import os
import stat
import sys
import textwrap
import time

import pytest

np = pytest.importorskip("numpy")

from services.voice import speaker as spk

SR = spk.SAMPLE_RATE


def tone(freq, seconds=2.0, amp=0.3, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def pcm(freq, seconds=2.0, amp=0.3):
    return (np.clip(tone(freq, seconds, amp), -1, 1) * 32767).astype("<i2").tobytes()


class FakeEmbedder:
    """A 'speaker' is a tone: the embedding is a fixed random vector for its dominant frequency
    (rounded to 50 Hz), so the same tone always embeds alike and different tones are ~orthogonal."""
    model = "fake.onnx"

    def __init__(self, delay=0.0):
        self.calls, self.delay = 0, delay

    def embed(self, samples):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        spectrum = np.abs(np.fft.rfft(samples))
        freq = int(round(np.argmax(spectrum) * SR / len(samples) / 50.0) * 50)
        return np.random.default_rng(freq).normal(size=192).tolist()


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in ("VOICE_SPEAKER_PYTHON", "VOICE_SPEAKER_MODEL", "VOICE_SPEAKER_THRESHOLD",
                 "VOICE_SPEAKER_ASSISTANT_THRESHOLD", "VOICE_SPEAKER_THREADS"):
        monkeypatch.delenv(name, raising=False)


def run(coro):
    return asyncio.run(coro)


def make(tmp_path, **kw):
    kw.setdefault("embedder", FakeEmbedder())
    return spk.Speakers(tmp_path / "speakers.sqlite3", **kw)


def enroll(s, name, freq, n=3):
    for _ in range(n):
        assert run(s.enroll_add(name, pcm(freq)))["ok"]
    return s.enroll_finish(name)


# --- off -----------------------------------------------------------------------------
def test_off_when_unconfigured(tmp_path):
    s = spk.Speakers(tmp_path / "speakers.sqlite3")
    assert s.available is False and s.error
    assert run(s.embed(pcm(500))) is None
    assert run(s.identify(pcm(500))) is None
    reply = run(s.enroll_add("bake", pcm(500)))
    assert reply["ok"] is False and reply["reason"] == "off"
    assert s.enroll_finish("bake") is False
    assert run(s.learn_assistant_voice("af_heart", lambda text: (tone(900), SR))) is False
    assert s.enrolled() == [] and s.forget("bake") is False


def test_off_when_worker_cannot_start(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"x")
    s = spk.Speakers(tmp_path / "speakers.sqlite3", python=str(tmp_path / "no-such-python"), model=str(model))
    assert s.available is False and "no-such-python" in s.error
    assert run(s.identify(pcm(500))) is None


def test_off_when_sherpa_missing_in_process(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)      # import fails
    model = tmp_path / "model.onnx"
    model.write_bytes(b"x")
    monkeypatch.setenv("VOICE_SPEAKER_MODEL", str(model))
    s = spk.Speakers(tmp_path / "speakers.sqlite3")
    assert s.available is False and s.error
    assert run(s.embed(pcm(500))) is None


# --- embeddings -----------------------------------------------------------------------
def test_embed_and_too_short(tmp_path):
    fake = FakeEmbedder()
    s = make(tmp_path, embedder=fake)
    assert s.available
    vector = run(s.embed(pcm(500)))
    assert len(vector) == 192 and abs(float(np.linalg.norm(vector)) - 1) < 1e-5
    calls = fake.calls
    assert run(s.embed(pcm(500, seconds=0.5))) is None          # under 0.6 s
    assert run(s.identify(pcm(500, seconds=0.5))) is None
    assert run(s.embed(b"")) is None and run(s.embed(b"\x01")) is None
    assert fake.calls == calls                                   # never reached the model


def test_embed_is_bounded_by_timeout(tmp_path):
    s = make(tmp_path, embedder=FakeEmbedder(delay=1.0), timeout=0.2)

    async def timed():           # measured inside the loop: asyncio.run's teardown waits for the thread
        started = time.monotonic()
        result = await s.embed(pcm(500))
        return result, time.monotonic() - started
    result, elapsed = run(timed())
    assert result is None and elapsed < 0.9


def test_failing_embedder_never_raises(tmp_path):
    class Broken:
        def embed(self, samples):
            raise RuntimeError("boom")
    s = make(tmp_path, embedder=Broken())
    assert run(s.identify(pcm(500))) is None


# --- identification -----------------------------------------------------------------------
def test_identify_labels(tmp_path):
    s = make(tmp_path)
    assert enroll(s, "bake", 500) and enroll(s, "kim", 1100)
    assert s.enrolled() == ["bake", "kim"]

    me = run(s.identify(pcm(500)))
    assert me["label"] == "bake" and me["principal"] == "bake" and me["score"] > 0.99
    assert me["assistant_score"] is None and isinstance(me["ms"], int)

    stranger = run(s.identify(pcm(700)))
    assert stranger["label"] == "guest" and stranger["principal"] is None
    assert stranger["score"] < s.threshold and stranger["candidate"] in ("bake", "kim")

    renders = []

    async def synthesize(text):                       # Kokoro-like: float at 24 kHz
        renders.append(text)
        return tone(900, 2.5, sr=24000), 24000
    assert run(s.learn_assistant_voice("af_heart", synthesize))
    assert len(renders) == len(spk.ASSISTANT_SENTENCES) and s.has_assistant_voice("af_heart")

    echo = run(s.identify(pcm(900), assistant_voice="af_heart"))
    assert echo["label"] == "assistant" and echo["principal"] is None and echo["assistant_score"] > 0.9
    # the person is still themselves while the assistant voice is known
    assert run(s.identify(pcm(500), assistant_voice="af_heart"))["label"] == "bake"
    # unknown voice id or no voice: no echo score
    assert run(s.identify(pcm(900), assistant_voice="other"))["assistant_score"] is None
    assert run(s.identify(pcm(900)))["label"] == "guest"


def test_guest_when_nobody_enrolled(tmp_path):
    result = run(make(tmp_path).identify(pcm(500)))
    assert result["label"] == "guest" and result["score"] is None and result["principal"] is None


def test_thresholds_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_SPEAKER_THRESHOLD", "0.7")
    monkeypatch.setenv("VOICE_SPEAKER_ASSISTANT_THRESHOLD", "nonsense")
    s = make(tmp_path)
    assert s.threshold == 0.7 and s.assistant_threshold == spk.DEFAULT_ASSISTANT_THRESHOLD
    assert make(tmp_path, threshold=0.2).threshold == 0.2


def test_assistant_reference_cached_in_db(tmp_path):
    calls = []

    def synthesize(text):                             # plain callable, int16 PCM bytes (Catalog.synthesize form)
        calls.append(text)
        return (tone(900, 2.0, sr=24000) * 32767).astype("<i2").tobytes(), 24000
    assert run(make(tmp_path).learn_assistant_voice("sojourn", synthesize))
    first = len(calls)
    again = make(tmp_path)                            # restart: loaded from the db, nothing rendered
    assert again.has_assistant_voice("sojourn")
    assert run(again.learn_assistant_voice("sojourn", synthesize)) and len(calls) == first
    other_model = make(tmp_path, embedder=type("E", (FakeEmbedder,), {"model": "other.onnx"})())
    assert not other_model.has_assistant_voice("sojourn")       # another model recomputes
    assert run(other_model.learn_assistant_voice("sojourn", synthesize)) and len(calls) == 2 * first


def test_learn_assistant_voice_fails_softly(tmp_path):
    async def broken(text):
        raise RuntimeError("tts down")
    s = make(tmp_path)
    assert run(s.learn_assistant_voice("af_heart", broken)) is False
    assert not s.has_assistant_voice("af_heart")


# --- enrollment -------------------------------------------------------------------------
def test_enrollment_add_finish_forget(tmp_path):
    s = make(tmp_path)
    sentences = s.enrollment_sentences()
    assert len(sentences) == 4 and all(isinstance(x, str) and x for x in sentences)

    short = run(s.enroll_add("bake", pcm(500, seconds=1.0)))
    assert short == {**short, "ok": False, "reason": "too_short", "samples": 0, "needed": 3}
    quiet = run(s.enroll_add("bake", pcm(500, amp=0.001)))
    assert quiet["ok"] is False and quiet["reason"] == "too_quiet"
    clipped = run(s.enroll_add("bake", pcm(500, amp=1.5)))
    assert clipped["ok"] is False and clipped["reason"] == "clipped"

    first = run(s.enroll_add("bake", pcm(500)))
    assert first["ok"] and first["samples"] == 1 and first["needed"] == 2
    assert first["quality"]["seconds"] == 2.0 and first["quality"]["dbfs"] > -20
    assert s.enroll_finish("bake") is False                         # too few
    other = run(s.enroll_add("bake", pcm(1300)))                     # someone else's voice
    assert other["ok"] is False and other["reason"] == "inconsistent" and other["samples"] == 1
    assert run(s.enroll_add("bake", pcm(500)))["needed"] == 1
    third = run(s.enroll_add("bake", pcm(500)))
    assert third["ok"] and third["samples"] == 3 and third["needed"] == 0
    assert s.enrolled() == []                                       # not until finished
    assert s.enroll_finish("bake") is True
    assert s.enrolled() == ["bake"]
    assert make(tmp_path).enrolled() == ["bake"]                     # persisted
    assert s.forget("bake") is True and s.enrolled() == [] and s.forget("bake") is False
    assert make(tmp_path).enrolled() == []
    assert run(s.identify(pcm(500)))["label"] == "guest"


def test_enrollment_keeps_samples_bounded_and_validates_name(tmp_path):
    s = make(tmp_path)
    for _ in range(spk.MAX_PENDING + 3):
        reply = run(s.enroll_add("bake", pcm(500)))
    assert reply["samples"] == spk.MAX_PENDING
    for bad in ("  ", "guest", "Assistant", "unmapped", "x" * 121):
        with pytest.raises(ValueError):
            run(s.enroll_add(bad, pcm(500)))


def test_voiceprint_is_normalised_mean(tmp_path):
    s = make(tmp_path)
    assert enroll(s, "bake", 500)
    voiceprint = s._prints["bake"]
    assert abs(float(np.linalg.norm(voiceprint)) - 1) < 1e-5


def test_db_file_is_private(tmp_path):
    s = make(tmp_path)
    assert enroll(s, "bake", 500)
    mode = stat.S_IMODE(os.stat(tmp_path / "speakers.sqlite3").st_mode)
    assert mode == 0o600
    path = tmp_path / "exists.sqlite3"
    path.write_bytes(b"")
    path.chmod(0o644)
    spk.Speakers(path, embedder=FakeEmbedder())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_unusable_db_turns_feature_off(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = spk.Speakers(blocker / "speakers.sqlite3", embedder=FakeEmbedder())
    assert s.available is False
    assert run(s.identify(pcm(500))) is None


# --- helpers ------------------------------------------------------------------------------
def test_resample_and_echo_views():
    x = tone(440, 1.0, sr=24000)
    y = spk.to_16k(x, 24000)
    assert y.dtype == np.float32 and abs(len(y) - SR) <= 1
    assert spk.to_16k((x * 32767).astype("<i2"), 24000).max() == pytest.approx(y.max(), rel=0.01)
    views = spk.echo_views(tone(900, 1.0))
    assert len(views) == 3 and all(len(v) == SR for v in views)


def test_shadow_record():
    assert spk.shadow_record(None, 3) == {"event": "voice_speaker", "turn": 3, "label": None}
    record = spk.shadow_record({"label": "bake", "score": 0.8, "principal": "bake", "candidate": "bake",
                                "assistant_score": 0.1, "ms": 22}, 4)
    assert record["label"] == "bake" and "embedding" not in record and json.dumps(record)


# --- the worker pipe (a fake worker speaking speaker_worker.py's protocol) -----------------
FAKE_WORKER = textwrap.dedent("""
    import json, sys
    out = sys.stdout.buffer
    out.write(b'{"ready": true, "dim": 3, "model": "fake.onnx"}\\n'); out.flush()
    stdin = sys.stdin.buffer
    for line in stdin:
        n = json.loads(line)["bytes"]
        data = stdin.read(n)
        if n == 2 * 16000 * 3:          # 3 s: crash
            sys.exit(3)
        if n == 2 * 16000 * 4:          # 4 s: hang
            import time; time.sleep(30)
        reply = {"ok": True, "embedding": [1.0, float(n), 0.0], "ms": 1} if n != 2 * 16000 * 2 else {"ok": False, "error": "nope"}
        out.write((json.dumps(reply) + "\\n").encode()); out.flush()
""")


def test_worker_pipe(tmp_path, monkeypatch):
    import subprocess
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    spawned = []

    def spawn():
        spawned.append(1)
        return subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    worker = spk.WorkerEmbedder("python", "/models/fake.onnx", spawn=spawn)
    try:
        assert worker.model == "fake.onnx" and worker.dim == 3
        assert worker.embed(np.zeros(SR, dtype=np.float32))[1] == 2.0 * SR
        with pytest.raises(RuntimeError, match="nope"):
            worker.embed(np.zeros(2 * SR, dtype=np.float32))          # one failed utterance, worker stays
        assert worker.embed(np.zeros(SR, dtype=np.float32))[0] == 1.0 and len(spawned) == 1
        with pytest.raises(RuntimeError):
            worker.embed(np.zeros(3 * SR, dtype=np.float32))          # crash
        with pytest.raises(RuntimeError, match="down"):
            worker.embed(np.zeros(SR, dtype=np.float32))              # within the restart backoff
        monkeypatch.setattr(spk, "RESTART_BACKOFF_S", 0.0)
        assert worker.embed(np.zeros(SR, dtype=np.float32))[0] == 1.0 and len(spawned) == 2
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            worker.embed(np.zeros(4 * SR, dtype=np.float32), timeout=0.3)   # hang: killed by the watchdog
        assert time.monotonic() - started < 5
    finally:
        worker.close()


def test_scribe_labels_use_an_enrolled_name_only_for_a_sure_match():
    from services.voice.speaker import SessionSpeakers
    s = SessionSpeakers(sure=0.65)
    v = [1.0, 0.0, 0.0]
    assert s.label({"principal": "bake", "score": 0.73, "vector": v}) == "bake"
    # A weak match (a sibling who sounds like the owner) is just another speaker.
    assert s.label({"principal": "bake", "score": 0.52, "vector": v}) == "Speaker A"
    assert s.label({"principal": None, "score": 0.2, "vector": [0.0, 1.0, 0.0]}) == "Speaker B"
    assert s.label(None) == "Speaker"


def test_a_named_match_is_sure_by_default(monkeypatch):
    # Bake 2026-10-25: "more than half sure that it's me" is enough, so "probably" never blocks.
    from services.voice import house, speaker
    from services.voice.speaker import SessionSpeakers
    monkeypatch.delenv('VOICE_SPEAKER_SURE', raising=False)
    assert house.SPEAKER_SURE <= speaker.DEFAULT_THRESHOLD
    assert SessionSpeakers().label({"principal": "bake", "score": 0.46, "vector": [1.0, 0.0]}) == "bake"


# --- per-person scores ----------------------------------------------------------------
def enrolled(tmp_path, *people):
    s = make(tmp_path)
    for name, freq in people:
        for _ in range(3):
            assert run(s.enroll_add(name, pcm(freq)))["ok"]
        assert s.enroll_finish(name)
    return s


def test_identify_reports_every_enrolled_score(tmp_path):
    s = enrolled(tmp_path, ("bake", 500), ("logan", 1300))
    result = run(s.identify(pcm(500)))
    assert set(result["scores"]) == {"bake", "logan"} and result["scores"]["bake"] > 0.9


# --- the clip bank (Bake 2026-10-27) -------------------------------------------------------
def toward(anchor, cos, seed=1):
    """A unit vector with cosine ``cos`` to ``anchor`` (a fixed other direction per seed)."""
    other = np.random.default_rng(seed).normal(size=anchor.size)
    other -= other.dot(anchor) * anchor
    other /= np.linalg.norm(other)
    return (cos * anchor + np.sqrt(1 - cos * cos) * other).astype(np.float32)


def speech(freq=500, seconds=2.0, noise=0.0):
    """Tone bursts with pauses (a noise floor, like speech), as PCM16; ``noise`` adds hiss."""
    x = tone(freq, seconds)
    x[(np.arange(x.size) // 4000) % 3 == 2] = 0.0                 # every third 0.25 s is a pause
    x = x + np.random.default_rng(3).normal(0, noise or 1e-4, x.size).astype(np.float32)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def test_bank_migrates_an_old_print_into_a_pinned_clip(tmp_path):
    s = enrolled(tmp_path, ("bake", 500))
    with s._db_lock:
        s._db.execute("DELETE FROM voice_clips")                     # as a store from before the bank
    again = make(tmp_path)
    assert [(c["source"], c["pinned"], c["active"]) for c in again.bank("bake")] == [("enroll", True, True)]
    assert run(again.identify(pcm(500)))["label"] == "bake"
    fresh = enrolled(tmp_path / "new", ("bake", 500))
    assert [c["pinned"] for c in fresh.bank("bake")] == [True, True, True]   # the 3 enrollment clips


def test_learned_clips_lift_a_new_setting_but_only_so_far(tmp_path):
    s = enrolled(tmp_path, ("bake", 500))
    anchor = s._prints["bake"]
    u = toward(anchor, 0.0, seed=5)
    car = normalize_np(0.35 * anchor + np.sqrt(1 - 0.35 ** 2) * u)  # Bake in the car: 0.35 against the enrollment
    assert s._score("bake", car)[0] == pytest.approx(0.35, abs=1e-4)
    for i in range(3):                                               # three car clips, a little different each
        w = toward(anchor, 0.0, seed=20 + i)
        w = normalize_np(w - w.dot(u) * u)
        clip = normalize_np(0.35 * anchor + np.sqrt(1 - 0.35 ** 2) * normalize_np(0.9 * u + 0.436 * w))
        assert s.learn_sample("bake", clip, device="bakecar")["ok"]
    lifted, clip = s._score("bake", car)
    assert lifted == pytest.approx(0.35 + spk.BANK_MAX_LIFT, abs=1e-4) and clip["setting"] == "car"
    assert s._score("bake", anchor)[0] >= 0.99                       # his phone voice: never lower
    elsewhere = toward(anchor, 0.1, seed=99)
    assert s._score("bake", elsewhere)[0] < 0.3                      # nobody else got closer


def normalize_np(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_learn_sample_gates(tmp_path):
    s = enrolled(tmp_path, ("bake", 500), ("logan", 1300))
    bake, logan = s._prints["bake"], s._prints["logan"]
    assert s.learn_sample("bake", toward(bake, 0.2))["reason"] == "too_far"
    mixed = normalize_np(0.45 * bake + 0.6 * logan)
    assert s.learn_sample("bake", mixed)["reason"] == "other_higher"
    assert s.learn_sample("nobody", toward(bake, 0.5))["reason"] == "not_enrolled"
    ok = s.learn_sample("bake", toward(bake, 0.4, seed=4))
    assert ok["ok"] and s.learn_sample("bake", toward(bake, 0.4, seed=4))["reason"] == "duplicate"
    # Quality: the clip's own audio must be long, loud, unclipped and clear of noise enough.
    assert s.learn_sample("bake", toward(bake, 0.4, seed=6), pcm=speech(seconds=0.8))["reason"] == "quality"
    assert s.learn_sample("bake", toward(bake, 0.4, seed=6), pcm=speech(noise=0.3))["reason"] == "quality"
    good = s.learn_sample("bake", toward(bake, 0.4, seed=6), pcm=speech(), device="bakephone")
    assert good["ok"] and good["device"] == "bakephone" and good["setting"] == "quiet"


def test_bank_is_capped_and_never_evicts_the_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(spk, "BANK_MAX", 6)
    s = enrolled(tmp_path, ("bake", 500))
    bake = s._prints["bake"]
    for i, cos in enumerate((0.40, 0.42, 0.44)):                     # fills the bank: 3 pinned + 3 learned
        assert s.learn_sample("bake", toward(bake, cos, seed=40 + i))["ok"]
    stronger = s.learn_sample("bake", toward(bake, 0.5, seed=50))
    weakest = [c for c in s.bank("bake") if c["score"] == pytest.approx(0.40, abs=1e-3)][0]
    assert stronger["evicted"] == weakest["version"] and weakest["ended"] == "evicted"
    weaker = s.learn_sample("bake", toward(bake, 0.31, seed=51))     # weaker than all: the oldest learned goes
    oldest = [c for c in s.bank("bake") if c["source"] == "learned"][1]
    assert weaker["evicted"] == oldest["version"]
    active = [c for c in s.bank("bake") if c["active"]]
    assert len(active) == 6 and sum(c["pinned"] for c in active) == 3


def test_learned_clips_roll_back_and_end_on_re_enrollment(tmp_path):
    s = enrolled(tmp_path, ("bake", 500))
    bake = s._prints["bake"].copy()
    versions = [s.learn_sample("bake", toward(bake, 0.4, seed=60 + i))["version"] for i in range(3)]
    assert s.learned_rollback("bake", to_version=versions[0]) == 2
    assert [c["active"] for c in s.bank("bake") if c["source"] == "learned"] == [True, False, False]
    assert len([c for c in make(tmp_path)._banks["bake"] if not c["pinned"]]) == 1   # persisted
    assert s.learned_rollback("bake") == 1
    assert all(c["pinned"] for c in s._banks["bake"])
    assert s.learn_sample("bake", toward(bake, 0.4, seed=70))["ok"]
    for _ in range(3):
        assert run(s.enroll_add("bake", pcm(500)))["ok"]
    assert s.enroll_finish("bake")
    assert [c["source"] for c in s.bank("bake") if c["active"]] == ["enroll"] * 3
    assert s.forget("bake") and s.bank("bake") == []


def test_cohort_normalization_names_a_clear_match_under_the_threshold(tmp_path):
    s = enrolled(tmp_path, ("bake", 500))
    bake = s._prints["bake"]
    turn = toward(bake, 0.38, seed=80)                               # a noisy line of his: under 0.45
    assert run_identify(s, turn)["norm"] is None and run_identify(s, turn)["label"] == "guest"
    for i in range(spk.COHORT_MIN + 2):                              # strangers heard on his phone
        assert s.add_cohort(toward(bake, 0.02 * (i % 3), seed=200 + i))
    assert not s.add_cohort(toward(bake, 0.02 * 0, seed=200))       # a near-copy is skipped
    result = run_identify(s, turn)
    assert result["norm"] >= spk.NORM_SURE and result["label"] == "bake"
    assert run_identify(s, toward(bake, 0.2, seed=81))["label"] == "guest"   # under LEARN_MIN: never


def run_identify(s, vector):
    """identify() for a given embedding (the fake embedder maps audio, not vectors)."""
    s._embed_samples = lambda samples: _done((vector, 1))
    return run(s.identify(pcm(500)))


async def _done(value):
    return value


@pytest.mark.parametrize("device,snr,setting", [
    ("bakecar", 30, "car"), ("bake-watch", 30, "watch"), ("pixel-wear", 30, "watch"),
    ("bakephone", 30, "quiet"), ("bakephone", 8, "noisy"), ("", 30, "quiet"),
])
def test_clip_setting_tags(device, snr, setting):
    assert spk.setting_for(device, {"snr_db": snr}) == setting
