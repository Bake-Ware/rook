"""Dual TTS engines (Kokoro + Chatterbox): ids, catalog, fallback, tags, worker pipe. No model downloads."""
import asyncio
import io
import json
import os
import sys
import threading
import time

import numpy as np
import pytest

from services.voice import tts
from services.voice.front import fixed_block
from services.voice.modes import resolve as resolve_mode


class FakeKokoro:
    voices = ['bf_emma', 'af_heart', 'af_nova']

    def __init__(self):
        self.calls = []

    def synthesize(self, text, name):
        self.calls.append((text, name))
        return np.full(2400, 0.25, dtype=np.float32), 24000


class FakeChatterbox:
    voices = ['default', 'warm']
    sample_rate = 24000

    def __init__(self, fail=False, sr=24000):
        self.calls, self.fail, self.sr = [], fail, sr

    def synthesize(self, text, name):
        self.calls.append((text, name))
        if self.fail:
            raise RuntimeError('cuda out of memory')
        return np.full(4800, -0.5, dtype=np.float32), self.sr

    def close(self):
        pass


# --- ids --------------------------------------------------------------------
@pytest.mark.parametrize('raw,expected', [
    ('kokoro:af_nova', ('kokoro', 'af_nova')),
    ('af_nova', ('kokoro', 'af_nova')),             # legacy bare id = Kokoro
    ('chatterbox:default', ('chatterbox', 'default')),
    ('Chatterbox: warm ', ('chatterbox', 'warm')),
    ('piper:amy', None), ('chatterbox:', None), ('', None), (None, None), (42, None), ('x' * 200, None),
])
def test_parse_voice_id(raw, expected):
    assert tts.parse_voice_id(raw) == expected


def test_strip_tags_removes_sound_tags_only():
    assert tts.strip_tags('Ha [laugh] that is good. [sigh] Fine.') == 'Ha that is good. Fine.'
    assert tts.strip_tags('[clear throat] Okay.') == 'Okay.'
    assert tts.strip_tags('Array [0] and [Ref 12] stay.') == 'Array [0] and [Ref 12] stay.'


# --- catalog ----------------------------------------------------------------
def test_catalog_kokoro_only_lists_namespaced_ids_and_reports_chatterbox_missing():
    cat = tts.Catalog(FakeKokoro(), 'af_heart', None, RuntimeError('no torch'), default='chatterbox:default')
    d = cat.describe()
    assert d['voices'] == ['kokoro:af_heart', 'kokoro:af_nova', 'kokoro:bf_emma']
    assert d['default'] == 'kokoro:af_heart'          # chatterbox default unavailable -> Kokoro default
    assert d['engines']['chatterbox'] == {'available': False, 'error': 'no torch'}
    assert {e['engine'] for e in d['catalog']} == {'kokoro'}
    assert d['catalog'][0]['label'] == 'Heart (US female)'
    assert cat.resolve('chatterbox:default') is None


def test_catalog_with_both_engines():
    cat = tts.Catalog(FakeKokoro(), 'af_heart', FakeChatterbox(), default='chatterbox:default')
    assert cat.default == 'chatterbox:default'
    assert 'chatterbox:warm' in cat.voices and 'kokoro:bf_emma' in cat.voices
    entries = {e['id']: e for e in cat.describe()['catalog']}
    assert entries['chatterbox:default']['engine'] == 'chatterbox'
    assert entries['chatterbox:default']['label'] == 'Chatterbox (default)'
    assert cat.describe()['engines']['chatterbox'] == {'available': True}
    assert cat.resolve('bf_emma') == 'kokoro:bf_emma'     # legacy id still selects Kokoro
    assert cat.resolve('kokoro:nope') is None
    assert cat.supports_tags('chatterbox:warm') and not cat.supports_tags('af_heart')


def test_catalog_default_accepts_legacy_and_bad_ids():
    assert tts.Catalog(FakeKokoro(), 'af_heart', default='af_nova').default == 'kokoro:af_nova'
    assert tts.Catalog(FakeKokoro(), 'missing', default='junk:1').default == 'kokoro:af_heart'


# --- synthesis --------------------------------------------------------------
def test_chatterbox_gets_tags_and_kokoro_gets_them_stripped():
    kokoro, chatter = FakeKokoro(), FakeChatterbox()
    cat = tts.Catalog(kokoro, 'af_heart', chatter)
    pcm, sr = cat.synthesize('Ha [laugh] nice.', 'chatterbox:warm')
    assert chatter.calls == [('Ha [laugh] nice.', 'warm')] and sr == 24000
    assert np.frombuffer(pcm, '<i2')[0] == -16383
    cat.synthesize('Ha [laugh] nice.', 'af_nova')
    assert kokoro.calls == [('Ha nice.', 'af_nova')]


def test_chatterbox_failure_falls_back_to_kokoro_and_reports():
    kokoro = FakeKokoro()
    cat = tts.Catalog(kokoro, 'af_heart', FakeChatterbox(fail=True))
    seen = []
    pcm, sr = cat.synthesize('Well [sigh] okay.', 'chatterbox:default', on_fallback=seen.append)
    assert kokoro.calls == [('Well okay.', 'af_heart')] and sr == 24000 and len(pcm) == 4800
    assert len(seen) == 1 and 'out of memory' in str(seen[0])


def test_chatterbox_audio_is_resampled_to_target_rate():
    cat = tts.Catalog(FakeKokoro(), 'af_heart', FakeChatterbox(sr=48000), target_sr=24000)
    pcm, sr = cat.synthesize('Hi.', 'chatterbox:default')
    assert sr == 24000 and len(pcm) == 2 * 2400


def test_unknown_voice_uses_session_default():
    kokoro = FakeKokoro()
    cat = tts.Catalog(kokoro, 'af_heart', None, default='af_nova')
    cat.synthesize('Hi.', 'chatterbox:default')       # not loaded -> default voice, no error
    assert kokoro.calls == [('Hi.', 'af_nova')]


def test_resolve_voice_helper_supports_catalog_and_plain_providers():
    class Plain:
        voices = ['test']
    class WithCatalog:
        def __init__(self):
            self.tts = tts.Catalog(FakeKokoro(), 'af_heart')
        def resolve_voice(self, v):
            return self.tts.resolve(v)
    assert tts.resolve_voice(Plain(), 'test') == 'test' and tts.resolve_voice(Plain(), 'x') is None
    assert tts.resolve_voice(WithCatalog(), 'af_nova') == 'kokoro:af_nova'


# --- loading ----------------------------------------------------------------
def test_load_chatterbox_is_off_without_device():
    assert tts.load_chatterbox({}) == (None, None)


def test_load_chatterbox_failure_is_reported_not_raised(tmp_path):
    engine, error = tts.load_chatterbox({'VOICE_CHATTERBOX_DEVICE': 'cuda:0',
                                         'VOICE_CHATTERBOX_PYTHON': str(tmp_path / 'missing-python')})
    assert engine is None and error is not None


# --- worker pipe ------------------------------------------------------------
class FakeProc:
    """Stands in for the worker subprocess: answers each request line with a header + PCM."""
    def __init__(self, ready=None, fail_after=None):
        self.stdout = io.BytesIO()
        self.ready = ready or {'ready': True, 'sr': 24000, 'voices': ['default', 'warm']}
        self.stdin = self
        self.requests, self.fail_after, self.dead = [], fail_after, False
        self._queue(json.dumps(self.ready).encode() + b'\n')

    def _queue(self, data):
        pos = self.stdout.tell()
        self.stdout.seek(0, 2); self.stdout.write(data); self.stdout.seek(pos)

    def write(self, data):
        req = json.loads(data)
        self.requests.append(req)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            self.dead = True
            return
        pcm = (np.full(480, 0.5) * 32767).astype('<i2').tobytes()
        self._queue(json.dumps({'ok': True, 'sr': 24000, 'bytes': len(pcm)}).encode() + b'\n' + pcm)

    def flush(self):
        pass

    def poll(self):
        return 1 if self.dead else None

    def kill(self):
        self.dead = True

    def wait(self, timeout=None):
        return 1


def test_worker_engine_round_trip():
    proc = FakeProc()
    engine = tts.ChatterboxEngine('cuda:0', python='/venv/bin/python', spawn=lambda: proc)
    assert engine.voices == ['default', 'warm'] and engine.sample_rate == 24000
    samples, sr = engine.synthesize('Hello [chuckle].', 'warm')
    assert sr == 24000 and len(samples) == 480 and abs(samples[0] - 0.5) < 1e-3
    assert proc.requests == [{'text': 'Hello [chuckle].', 'voice': 'warm'}]


def test_worker_engine_not_ready_raises():
    with pytest.raises(RuntimeError, match='no GPU'):
        tts.ChatterboxEngine('cuda:0', python='p', spawn=lambda: FakeProc(ready={'ready': False, 'error': 'no GPU'}))


def test_worker_death_fails_fast_then_restarts_after_backoff(monkeypatch):
    procs = [FakeProc(fail_after=1), FakeProc()]
    engine = tts.ChatterboxEngine('cuda:0', python='p', spawn=lambda: procs.pop(0))
    engine.synthesize('one', 'default')
    with pytest.raises(RuntimeError):
        engine.synthesize('two', 'default')                  # worker died mid-request
    with pytest.raises(RuntimeError, match='down'):
        engine.synthesize('three', 'default')                # inside the backoff window: no respawn
    monkeypatch.setattr(tts, 'RESTART_BACKOFF_S', 0)
    samples, _ = engine.synthesize('four', 'default')        # respawned
    assert len(samples) == 480 and not procs


# --- per-session selection, fallback reporting, Front prompt ----------------
def test_session_reports_fallback_once():
    from services.voice.runtime import Connection
    from services.voice.state import Store

    class Provider:
        system, default_voice = 'test', 'chatterbox:default'
        def __init__(self):
            self.tts = tts.Catalog(FakeKokoro(), 'af_heart', FakeChatterbox(fail=True))
        def resolve_voice(self, v):
            return self.tts.resolve(v)
        async def synthesize(self, text, voice, on_fallback=None):
            return self.tts.synthesize(text, voice, on_fallback)

    async def scenario():
        sent = []
        async def send_json(m): sent.append(m)
        async def send_bytes(b): pass
        conn = Connection(Store(':memory:'), None, Provider(), 's1', send_json, send_bytes)
        assert conn.voice == 'chatterbox:default'
        await conn.synthesize('Hi [laugh].', 0)
        await conn.synthesize('Again.', 0)
        errors = [m for m in sent if m.get('type') == 'error']
        assert len(errors) == 1 and errors[0]['code'] == 'tts_fallback'
        conn.voice = conn.provider.resolve_voice('af_nova')   # per-session switch, legacy id
        assert conn.voice == 'kokoro:af_nova'
    asyncio.run(scenario())


def test_front_prompt_mentions_tags_only_for_chatterbox():
    mode = resolve_mode('assistant')
    plain = fixed_block(mode, set())
    tagged = fixed_block(mode, set(), sound_tags=tts.FRONT_TAGS)
    assert '[laugh]' not in plain and '[laugh]' in tagged and '[chuckle]' in tagged
    assert tagged.startswith(plain.split('Things you can')[0])     # the stable prefix is unchanged


def test_worker_module_imports_without_torch():
    # The voice service must be able to import the worker module (in-process mode is lazy).
    import services.voice.chatterbox_worker as worker
    assert hasattr(worker, 'Synth') and hasattr(worker, 'main')


class HangProc(FakeProc):
    """Answers the ready line, then hangs on every request until killed (a stuck GPU)."""
    def __init__(self):
        super().__init__()
        self.killed = threading.Event()
        ready_line = self.stdout.readline()
        outer = self

        class Out:
            def __init__(self):
                self.lines = [ready_line]
            def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                outer.killed.wait(10)
                return b''
            def read(self, n):
                return b''
        self.stdout = Out()

    def write(self, data):
        self.requests.append(json.loads(data))

    def kill(self):
        self.dead = True
        self.killed.set()


def test_hung_worker_is_killed_after_timeout_and_fails_fast():
    proc = HangProc()
    engine = tts.ChatterboxEngine('cuda:0', python='p', spawn=lambda: proc, timeout=0.3)
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        engine.synthesize('Hello there.', 'default')
    assert time.monotonic() - started < 3          # not forever
    assert proc.killed.is_set()
