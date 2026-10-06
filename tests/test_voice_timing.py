"""Per-turn stage timing: one structured log line per turn, same fields in activity done."""
import asyncio
import json
import logging
import time

from services.voice import timing as timing_module
from services.voice.identity import Identity
from services.voice.jobs import Jobs
from services.voice.runtime import Connection
from services.voice.state import Store
from services.voice.timing import TurnTiming


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(json.loads(record.getMessage()))


def capture(monkeypatch):
    handler = Lines()
    monkeypatch.setattr(timing_module, 'log', logging.getLogger('voice.timing.test'))
    timing_module.log.handlers[:] = [handler]
    timing_module.log.setLevel(logging.INFO)
    timing_module.log.propagate = False
    return handler.lines


def test_marks_are_from_end_of_speech_and_first_only(monkeypatch):
    lines = capture(monkeypatch)
    t = TurnTiming(3, 'voice', started=10.8, speech_end=10.0)
    t.span('stt_ms', 10.8, 11.1)
    t.mark('plan_ms', 12.0)
    t.mark('plan_ms', 13.0)            # a later retry does not move the first plan
    t.mark('first_audio_ms', 12.5)
    fields = t.finish('ok', now=14.0)
    assert fields == {'source': 'voice', 'endpoint_ms': 800, 'stt_ms': 300, 'plan_ms': 2000,
                      'first_audio_ms': 2500, 'total_ms': 4000}
    assert lines == [{'event': 'voice_turn_timing', 'turn': 3, 'status': 'ok', **fields}]


def test_typed_turn_or_stale_mark_measures_from_turn_start():
    assert TurnTiming(1, 'text', started=5.0).fields() == {'source': 'text', 'endpoint_ms': 0}
    # A speech mark after the turn started (new speech arriving) never yields negative times.
    assert TurnTiming(1, 'voice', started=5.0, speech_end=6.0).fields()['endpoint_ms'] == 0


class Provider:
    system = 'test'
    default_voice = 'test'
    supports_activity = True
    async def transcribe(self, pcm):
        await asyncio.sleep(.03)
        return 'What time is it?'
    async def chat(self, messages, on_clause, reply_only=False, **kwargs):
        await asyncio.sleep(.02)
        await on_clause('It is noon.')
        return 'It is noon.', []
    async def synthesize(self, text, voice):
        await asyncio.sleep(.01)
        return b'\0' * 1280, 16000


def test_voice_turn_reports_each_stage_in_done_event_and_one_log_line(tmp_path, monkeypatch):
    lines = capture(monkeypatch)
    async def scenario():
        store = Store(tmp_path / 'state.db')
        events = []
        async def send(event): events.append(event)
        async def audio(data): pass
        jobs = Jobs(store, {}, '', 0, lambda *a: None)
        conn = Connection(store, jobs, Provider(), 'session', send, audio, activity=True,
                          enqueue=events.append, identity=Identity('Alex', owner=True))
        try:
            conn.last_speech = time.monotonic() - .2   # the user stopped talking 200 ms ago
            await conn.start(pcm=b'\0' * 640)
            await conn.task
        finally:
            await conn.close()
            await jobs.close()
            store.db.close()
        done = [e for e in events if e.get('type') == 'activity' and e['phase'] == 'done']
        assert len(done) == 1
        t = done[0]['timing']
        assert t['source'] == 'voice' and t['endpoint_ms'] >= 190
        assert t['stt_ms'] >= 25 and t['llm_ms'] >= 15
        assert t['endpoint_ms'] + t['stt_ms'] <= t['plan_ms'] <= t['first_text_ms'] <= t['first_audio_ms'] <= t['total_ms']
        assert len(lines) == 1 and lines[0]['turn'] == done[0]['turn'] and lines[0]['status'] == 'ok'
        assert {k: lines[0][k] for k in t} == t
        assert 'What time' not in json.dumps(lines)   # no transcript text in the log
    asyncio.run(scenario())


def test_typed_unspoken_turn_has_no_audio_or_stt(tmp_path, monkeypatch):
    capture(monkeypatch)
    async def scenario():
        store = Store(tmp_path / 'state.db')
        events = []
        async def send(event): events.append(event)
        jobs = Jobs(store, {}, '', 0, lambda *a: None)
        conn = Connection(store, jobs, Provider(), 'session', send, send, activity=True, enqueue=events.append)
        try:
            await conn.start(text='hello', speak=False)
            await conn.task
        finally:
            await conn.close()
            await jobs.close()
            store.db.close()
        t = next(e for e in events if e.get('phase') == 'done')['timing']
        assert t['source'] == 'text' and t['endpoint_ms'] == 0
        assert 'stt_ms' not in t and 'first_audio_ms' not in t and t['first_text_ms'] <= t['total_ms']
    asyncio.run(scenario())
