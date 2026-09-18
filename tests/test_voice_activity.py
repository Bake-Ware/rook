import asyncio
import json
import time

import httpx
import pytest

from services.voice.activity import Activity
from services.voice.decision import DecisionClient
from services.voice.feedback import FeedbackStore
from services.voice.jobs import Jobs
from services.voice.runtime import Connection
from services.voice.state import Store
from services.voice import providers


class Provider:
    system = 'test'
    default_voice = 'test'
    async def transcribe(self, pcm): return 'Hello'
    async def chat(self, messages, on_clause, reply_only=False):
        await on_clause('Hello.')
        return 'Hello.', []
    async def synthesize(self, text, voice): return b'\0' * 1280, 16000


def payload():
    from services.voice.decision import questions
    answers = []
    for q in questions('text'):
        a = {'id': q['id'], 'type': q['type'], 'confidence': .9}
        if q['type'] == 'noul': a['noul'] = .1
        else:
            keys = q.get('options', ['0', '1', '2', '3'])
            a['probabilities'] = {key: float(i == 0) for i, key in enumerate(keys)}
            if q['type'] == 'choice': a['choice'] = keys[0]
            else: a.update(level='0', score=0.)
        answers.append(a)
    return {'answers': answers}


async def connection(tmp_path, provider=None, client=None, thinking=True, activity=True, direct=None):
    store = Store(tmp_path / 'state.db')
    feedback = FeedbackStore(tmp_path / 'state.db')
    await feedback.open()
    events = []
    async def send(e): events.append(e)
    async def audio(b): pass
    def notify(session, event): conn.job_event(event)
    jobs = Jobs(store, direct or {}, '', 0, notify)
    conn = Connection(store, jobs, provider or Provider(), 'session', send, audio,
                      decision=client, feedback=feedback, thinking=thinking, activity=activity,
                      enqueue=events.append)
    async def close():
        await conn.close()
        await jobs.close()
        await feedback.close()
        if client: await client.close()
        store.db.close()
    return conn, events, close


@pytest.mark.parametrize('mode', ['ok', 'disabled', 'timeout', 'error', 'skipped'])
def test_exactly_one_decision_all_statuses_and_reply_order(tmp_path, mode):
    async def scenario():
        requests = []
        async def handler(request):
            assert any(e['type'] == 'assistant_delta' for e in events)
            requests.append(request)
            if mode == 'timeout': await asyncio.sleep(1)
            if mode == 'error': raise httpx.ConnectError('private token')
            return httpx.Response(200, json=payload())
        client = DecisionClient('' if mode == 'disabled' else 'http://engine', 20, httpx.MockTransport(handler))
        conn, events, close = await connection(tmp_path, client=client)
        try:
            await conn.start(text='hello', speak=False, internal=mode == 'skipped')
            await conn.task
            await asyncio.sleep(.04)
            decisions = [e for e in events if e['type'] == 'decision']
            assert len(decisions) == 1
            d = decisions[0]
            assert d['engine_status'] == mode
            assert ('answers' in d) == (mode == 'ok')
            assert d['turn'] == conn.epoch and d['mode'] == 'shadow'
            assert d['elapsed_ms'] >= 0
            if mode != 'ok': assert d['detail'] and 'private' not in d['detail']
            assert len(requests) == (0 if mode in ('disabled', 'skipped') else 1)
            phases = [e['phase'] for e in events if e['type'] == 'activity']
            assert phases == (['planning', 'planned', 'done'] if mode == 'skipped' else ['heard', 'planning', 'planned', 'done'])
            activity = [e for e in events if e['type'] == 'activity']
            assert [e['seq'] for e in activity] == list(range(1, len(activity)+1))
            assert all(isinstance(e['ts'], int) and e['ts'] > 0 for e in activity)
        finally: await close()
    asyncio.run(scenario())


@pytest.mark.parametrize('thinking,activity', [(False, False), (False, True), (True, False)])
def test_independent_opt_ins_and_no_disabled_engine_writes(tmp_path, thinking, activity):
    async def scenario():
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=payload())
        client = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        conn, events, close = await connection(tmp_path, client=client, thinking=thinking, activity=activity)
        try:
            await conn.start(text='hi', speak=False)
            await conn.task
            await asyncio.sleep(.03)
            if conn.shadow:
                await conn.shadow.close()
                await conn.shadow.feedback.flush()
            assert bool(requests) == thinking
            assert bool([e for e in events if e['type'] == 'activity']) == activity
            assert len([e for e in events if e['type'] == 'decision']) == int(thinking)
            assert conn.store.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == int(thinking)
        finally: await close()
    asyncio.run(scenario())


def test_interruption_before_dispatch_still_emits_one_skipped_decision(tmp_path):
    async def scenario():
        class Waiting(Provider):
            async def chat(self, *args, **kwargs): await asyncio.Event().wait()
        async def handler(request): pytest.fail('Engine must not be called before dispatch')
        client = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        conn, events, close = await connection(tmp_path, provider=Waiting(), client=client)
        try:
            await conn.start(text='hello', speak=False)
            await asyncio.sleep(.01)
            turn = conn.epoch
            await conn.interrupt()
            await asyncio.sleep(.01)
            decisions = [e for e in events if e['type'] == 'decision']
            assert len(decisions) == 1 and decisions[0]['engine_status'] == 'skipped'
            assert decisions[0]['turn'] == turn
            assert not conn.shadow.requests
            assert any(e.get('phase') == 'done' and e['status'] == 'cancelled' for e in events)
        finally: await close()
    asyncio.run(scenario())


def test_capacity_limit_is_visible_skipped_not_silence(tmp_path):
    async def scenario():
        client = DecisionClient('http://engine', transport=httpx.MockTransport(lambda r: pytest.fail('No engine request at capacity')))
        conn, events, close = await connection(tmp_path, client=client)
        try:
            for _ in range(8): conn.shadow._track(asyncio.sleep(1))
            await conn.start(text='hello', speak=False)
            await conn.task
            assert len([e for e in events if e['type'] == 'decision']) == 1
            assert next(e for e in events if e['type'] == 'decision')['engine_status'] == 'skipped'
        finally: await close()
    asyncio.run(scenario())


def test_tool_heartbeats_continue_after_done_then_readable_failure(tmp_path, monkeypatch):
    assert Activity.heartbeat_seconds == 2
    monkeypatch.setattr(Activity, 'heartbeat_seconds', .02)
    async def scenario():
        release = asyncio.Event()
        class ToolProvider(Provider):
            async def chat(self, messages, on_clause, reply_only=False):
                return '', [{'function': {'name': 'rook_read', 'arguments': json.dumps({'worker': 'kaiju', 'cap': 'info.uptime'})}}]
        async def failed(args):
            await release.wait()
            raise ValueError("no Rook worker named 'missing'; available: kaiju")
        conn, events, close = await connection(tmp_path, provider=ToolProvider(), direct={'rook_read': failed})
        conn.drain_results = lambda: None  # inspect job lifecycle independently of automatic narration
        try:
            await conn.start(text='check uptime', speak=False)
            await conn.task
            await asyncio.sleep(.065)
            waits = [e for e in events if e.get('phase') == 'tool_wait']
            assert len(waits) >= 2
            assert all(e['turn'] == 1 and e['worker'] == 'kaiju' and e['cap'] == 'info.uptime' for e in waits)
            assert waits[1]['elapsed_ms'] >= waits[0]['elapsed_ms'] + 15
            assert events.index(waits[0]) > next(i for i,e in enumerate(events) if e.get('phase') == 'done')
            release.set()
            await asyncio.gather(*list(conn.jobs.tasks.values()))
            result = next(e for e in events if e.get('phase') == 'tool_result')
            assert result['status'] == 'failed' and 'available: kaiju' in result['detail']
            await asyncio.sleep(.04)
            assert len([e for e in events if e.get('phase') == 'tool_wait']) == len(waits)
            phases = [e['phase'] for e in events if e['type'] == 'activity']
            assert phases.index('planned') < phases.index('tool_start') < phases.index('tool_result')
        finally: await close()
    asyncio.run(scenario())


def test_retry_fallback_events_never_include_raw_model_prose(tmp_path, monkeypatch):
    async def scenario():
        provider = providers.Provider.__new__(providers.Provider)
        provider.default_voice = 'test'
        async def handler(request):
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Private invented status'}}]})
        provider.chat_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(providers, 'log_rejected_plan', lambda *args: None)
        conn, events, close = await connection(tmp_path, provider=provider)
        try:
            await conn.start(text='check status', speak=False)
            await conn.task
            phases = [e['phase'] for e in events if e['type'] == 'activity']
            assert phases == ['heard', 'planning', 'retry', 'fallback', 'done']
            assert 'Private invented status' not in json.dumps(events)
            assert [e['text'] for e in events if e['type'] == 'assistant_delta'] == [providers.SAFE_FALLBACK]
        finally:
            await close()
            await provider.close()
    asyncio.run(scenario())


def test_voice_tts_activity_follows_plan(tmp_path):
    async def scenario():
        conn, events, close = await connection(tmp_path)
        try:
            await conn.start(pcm=b'audio', speak=True)
            await conn.task
            phases = [e['phase'] for e in events if e['type'] == 'activity']
            assert phases == ['heard', 'planning', 'planned', 'speaking', 'done']
            assert next(e for e in events if e['type'] == 'decision')['source'] == 'voice'
        finally: await close()
    asyncio.run(scenario())


def test_feedback_sqlite_batch_runs_off_event_loop(tmp_path):
    import threading
    async def scenario():
        feedback = FeedbackStore(tmp_path / 'thread.db')
        await feedback.open()
        main = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        def slow_batch(batch):
            assert threading.get_ident() != main
            entered.set()
            release.wait(1)
        feedback._batch = slow_batch
        feedback.submit('prune')
        try:
            async with asyncio.timeout(.2):
                while not entered.is_set(): await asyncio.sleep(.001)
                # Timers and the event loop keep moving with a blocked writer.
                await asyncio.sleep(.01)
                assert not release.is_set()
        finally:
            release.set()
            await feedback.close()
    asyncio.run(scenario())
