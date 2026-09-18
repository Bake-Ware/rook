import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
import time
import uuid

import httpx
import pytest

from services.voice.decision import DecisionClient, normalize, questions
from services.voice.feedback import FeedbackStore, export_examples
from services.voice.jobs import Jobs
from services.voice.runtime import Connection
from services.voice.state import Store


def response(source='voice', correction=0):
    answers = []
    for q in questions(source):
        a = {'id': q['id'], 'type': q['type'], 'confidence': .9}
        if q['type'] == 'noul':
            a['noul'] = correction if q['id'] == 'is_correction' else .1
        else:
            keys = q.get('options', ['0', '1', '2', '3'])
            a['probabilities'] = {k: 1.0 if i == 0 else 0.0 for i, k in enumerate(keys)}
            if q['type'] == 'choice':
                a['choice'] = keys[0]
            else:
                a.update(level='0', score=0.0)
        answers.append(a)
    return {'answers': answers, 'model': 'test-model', 'calibration': 'test-only'}


class Provider:
    default_voice = 'test'
    voices = ['test']
    system = 'test'

    async def transcribe(self, pcm):
        return 'Rook turn off the lights'

    async def chat(self, messages, on_clause, reply_only=False):
        await on_clause('Hello.')
        return 'Hello.', []

    async def synthesize(self, text, voice):
        return b'\0' * 3200, 16000


def test_client_disabled_never_uses_network():
    async def scenario():
        client = DecisionClient(url='')
        assert (await client.decide({}, 'text', 1))['status'] == 'disabled'
        assert client.http is None
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['timeout', 'down', 'http', 'malformed'])
def test_client_failures_are_bounded_and_sanitized(failure):
    async def handler(request):
        if failure == 'timeout':
            await asyncio.sleep(10)
        if failure == 'down':
            raise httpx.ConnectError('secret address and transcript')
        if failure == 'http':
            return httpx.Response(503, text='private server error')
        return httpx.Response(200, json={'answers': []})

    async def scenario():
        client = DecisionClient('http://engine', 20, httpx.MockTransport(handler))
        start = time.monotonic()
        event = await client.decide({'text': 'private'}, 'voice', 7)
        assert time.monotonic() - start < .2
        assert event['status'] == ('timeout' if failure == 'timeout' else 'error')
        assert event['turn'] == 7 and event['answers'] == []
        assert 'private' not in event['error'] and 'secret' not in event['error']
        await client.close()
    asyncio.run(scenario())


def test_client_exact_contract_and_training_questions():
    async def handler(request):
        if request.url.path == '/info':
            return httpx.Response(200, json={'model': 'test-model', 'lora_path': 'adapter',
                                            'calibration_details': {'adapter_sha256': 'hash'}})
        payload = json.loads(request.content)
        assert payload['state']['source'] == 'text'
        assert payload['questions'] == questions('text')
        return httpx.Response(200, json=response('text'))

    async def scenario():
        client = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        await client.refresh_info()
        event = await client.decide({'source': 'text'}, 'text', 42)
        assert set(event) == {'type', 'turn', 'source', 'mode', 'status', 'latency_ms', 'engine', 'answers', 'error'}
        assert event['status'] == 'ok' and event['engine']['adapter'] == 'adapter'
        assert set(event['engine']) == {'model', 'adapter', 'calibration'}
        answers = {a['id']: a for a in event['answers']}
        assert 'needs_response' not in answers
        assert answers['needs_confirmation']['p'] == .1
        assert answers['urgency'] == {'id': 'urgency', 'type': 'score', 'level': 0, 'expected': 0.0,
                                      'probabilities': {'0': 1., '1': 0., '2': 0., '3': 0.}}
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 1.1, True, '0.5'])
def test_malformed_probabilities_rejected(bad):
    payload = response()
    payload['answers'][0]['noul'] = bad
    with pytest.raises(ValueError):
        normalize(payload, 'voice')


@pytest.mark.parametrize('hello_thinking,protocol,expected', [(True, 2, True), (False, 2, False),
                                                           (None, 2, False), ('true', 2, False), (True, 1, False)])
def test_websocket_protocol_opt_in(tmp_path, monkeypatch, hello_thinking, protocol, expected):
    from fastapi.testclient import TestClient
    from services.voice import server

    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response('text'))

    @asynccontextmanager
    async def lifespan(app):
        app.state.provider = Provider()
        app.state.store = Store(tmp_path / 'state.db')
        app.state.jobs = Jobs(app.state.store, {}, '', 0, lambda s, e: None)
        app.state.decision = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        app.state.feedback = FeedbackStore(tmp_path / 'state.db')
        await app.state.feedback.open()
        yield
        await app.state.jobs.close()
        await app.state.decision.close()
        await app.state.feedback.close()
        app.state.store.db.close()

    monkeypatch.setattr(server.app.router, 'lifespan_context', lifespan)
    monkeypatch.setattr(server, 'TOKEN', 'test-token')
    with TestClient(server.app) as client:
        with client.websocket_connect('/ws', headers={'Authorization': 'Bearer test-token'}) as ws:
            hello = {'type': 'hello', 'protocol': protocol, 'conversation': str(uuid.uuid4())}
            if hello_thinking is not None:
                hello['thinking'] = hello_thinking
            ws.send_json(hello)
            session = ws.receive_json()
            assert session['thinking'] is expected
            ws.send_json({'type': 'text', 'text': 'hello', 'speak': False})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'metrics':
                    break
            # Synchronize with shadow completion, then use a second turn as a
            # receive barrier to detect forbidden late events from the first.
            time.sleep(.05)
            ws.send_json({'type': 'text', 'text': 'second', 'speak': False})
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'metrics':
                    break
            decisions = [e for e in events if e['type'] == 'decision']
            assert bool(decisions) is expected
            assert len({e['turn'] for e in decisions}) == len(decisions)
            assert any(e['type'] == 'assistant_delta' for e in events)
            assert all(e['status'] == 'ok' and e['mode'] == 'shadow' for e in decisions)
    assert len(requests) == (2 if expected else 0)
    db = sqlite3.connect(tmp_path / 'state.db')
    assert db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == (2 if expected else 0)
    db.close()


def test_shadow_timeout_does_not_delay_reply_and_captures_voice_context(tmp_path):
    async def handler(request):
        await asyncio.sleep(10)

    async def scenario():
        store = Store(tmp_path / 'state.db')
        feedback = FeedbackStore(tmp_path / 'state.db')
        await feedback.open()
        client = DecisionClient('http://engine', 300, httpx.MockTransport(handler))
        jobs = Jobs(store, {}, '', 0, lambda s, e: None)
        events = []
        async def send(e): events.append(e)
        async def audio(b): pass
        conn = Connection(store, jobs, Provider(), 'session', send, audio, decision=client,
                          feedback=feedback, thinking=True, conversation='conversation')
        start = time.monotonic()
        await conn.start(pcm=b'test', speak=False)
        await asyncio.wait_for(conn.task, .15)
        assert time.monotonic() - start < .15
        assert not any(e['type'] == 'decision' for e in events)
        await asyncio.sleep(.32)
        event = next(e for e in events if e['type'] == 'decision')
        assert event['status'] == 'timeout' and event['source'] == 'voice'
        await feedback.flush()
        row = store.db.execute('SELECT state FROM decisions').fetchone()
        state = json.loads(row['state'])
        assert state['contains_wake_word_or_assistant_name'] is True
        assert 'addressed' not in state and state['text'] == 'Rook turn off the lights'
        await conn.close()
        await client.close()
        await feedback.close()
        await jobs.close()
        store.db.close()
    asyncio.run(scenario())


def test_feedback_links_outcomes_across_reconnect_and_prunes_raw_text(tmp_path):
    async def scenario():
        path = tmp_path / 'state.db'
        store = Store(path)
        feedback = FeedbackStore(path, retention_days=30)
        await feedback.open()
        now = time.time()
        def begin(did, text, created=now, session='s'):
            feedback.submit('begin', did, session, 'conversation', 1, 'text',
                            {'text': text, 'previous_assistant_reply': 'private context'}, created)
        begin('a', 'turn off the lights')
        feedback.submit('reply', 'a', 'Should I proceed?')
        feedback.submit('completed', 'a')
        begin('b', 'yes')
        feedback.submit('reply', 'b', 'Done.')
        begin('c', "I wasn't talking to you")
        event = {'answers': normalize(response('text', .9), 'text'), 'engine': {}, 'latency_ms': 3., 'status': 'ok'}
        feedback.submit('finish', 'c', event, {})
        feedback.submit('reply', 'c', 'Okay.')
        begin('d', "I wasn't talking to you")
        feedback.submit('signal', 'c', 'reply_interrupted', {'playback': True})
        feedback.submit('signal', 'c', 'no_followup', {'approval': None})
        begin('other', 'yes', session='other-session')
        begin('old', 'private old text', created=now - 31 * 86400)
        feedback.submit('reply', 'old', 'private old reply')
        feedback.submit('signal', 'old', 'no_followup', {})
        feedback.submit('prune')
        await feedback.flush()
        rows = store.db.execute('SELECT decision_id,observed_decision_id,kind FROM decision_outcomes').fetchall()
        signals = {tuple(r) for r in rows}
        assert ('a', 'b', 'confirmation_answer') in signals
        assert ('b', 'c', 'explicit_correction') in signals
        assert ('b', 'c', 'model_correction') in signals
        assert ('c', 'd', 'repeated_command') in signals
        assert ('c', '', 'reply_interrupted') in signals
        assert not any(r[1] == 'other' for r in signals)
        old = store.db.execute("SELECT state,reply FROM decisions WHERE id='old'").fetchone()
        assert tuple(old) == (None, None)
        examples = list(export_examples(path))
        assert {e['decision_id'] for e in examples} == {'a', 'b', 'c'}
        assert all(e['label_kind'] == 'weak_outcome_signals' for e in examples)
        await feedback.close()
        # Migration is idempotent and doesn't reset decisions.
        feedback = FeedbackStore(path)
        await feedback.open()
        assert store.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == 6
        await feedback.close()
        store.db.close()
    asyncio.run(scenario())


def test_silence_only_after_reply_while_connected_and_cancelled_on_activity(tmp_path, monkeypatch):
    monkeypatch.setenv('DECISION_SILENCE_SECONDS', '.02')
    async def handler(request): return httpx.Response(200, json=response('text'))
    async def scenario():
        store = Store(tmp_path / 'state.db')
        feedback = FeedbackStore(tmp_path / 'state.db')
        await feedback.open()
        client = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        jobs = Jobs(store, {}, '', 0, lambda s, e: None)
        async def send(e): pass
        async def audio(b): pass
        conn = Connection(store, jobs, Provider(), 's', send, audio, decision=client, feedback=feedback, thinking=True)
        await conn.start(text='hello', speak=False)
        await conn.task
        await asyncio.sleep(.04)
        await conn.start(text='second', speak=False)
        await conn.task
        conn.shadow_hook('activity')
        await asyncio.sleep(.04)
        await conn.close()
        await feedback.flush()
        assert store.db.execute("SELECT COUNT(*) FROM decision_outcomes WHERE kind='no_followup'").fetchone()[0] == 1
        await client.close()
        await feedback.close()
        await jobs.close()
        store.db.close()
    asyncio.run(scenario())


def test_shadow_does_not_start_inference_or_writes_until_reply_done(tmp_path):
    async def scenario():
        release = asyncio.Event()
        events, operations, requests = [], [], []
        class SlowProvider(Provider):
            async def chat(self, messages, on_clause, reply_only=False):
                await release.wait()
                return await super().chat(messages, on_clause, reply_only)
        class Feedback:
            def submit(self, name, *args):
                assert conn.task.done()
                operations.append(name)
        async def handler(request):
            assert any(e['type'] == 'assistant_done' for e in events)
            requests.append(request)
            return httpx.Response(200, json=response('text'))
        store = Store(tmp_path / 'state.db')
        client = DecisionClient('http://engine', transport=httpx.MockTransport(handler))
        jobs = Jobs(store, {}, '', 0, lambda *args: None)
        async def send(e): events.append(e)
        async def audio(b): pass
        conn = Connection(store, jobs, SlowProvider(), 's', send, audio, decision=client,
                          feedback=Feedback(), thinking=True)
        await conn.start(text='hello', speak=False)
        await asyncio.sleep(.02)
        assert not requests and not operations
        release.set()
        await conn.task
        await asyncio.sleep(.03)
        assert len(requests) == 1
        assert operations.index('begin') < operations.index('reply') < operations.index('completed')
        assert 'finish' in operations
        assert len([e for e in events if e['type'] == 'decision']) == 1
        await conn.close()
        await client.close()
        await jobs.close()
        store.db.close()
    asyncio.run(scenario())


def test_lifespan_does_no_decision_io_until_first_opt_in(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.voice import server, providers, workers
    requests = []
    async def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={} if request.url.path == '/info' else response('text'))
    async def refresh(): return []
    monkeypatch.setattr(providers, 'Provider', Provider)
    monkeypatch.setattr(workers.inventory, 'refresh', refresh)
    monkeypatch.setattr(server, 'TOKEN', 'test-token')
    monkeypatch.setenv('VOICE_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setattr(server, 'DecisionClient', lambda: DecisionClient('http://engine', transport=httpx.MockTransport(handler)))
    with TestClient(server.app) as client:
        assert server.app.state.feedback.db is None
        with client.websocket_connect('/ws', headers={'Authorization': 'Bearer test-token'}) as ws:
            ws.send_json({'type': 'hello', 'protocol': 2, 'conversation': str(uuid.uuid4())})
            ws.send_json({'type': 'text', 'text': 'hello', 'speak': False})
            while ws.receive_json()['type'] != 'metrics': pass
        assert requests == []
        assert server.app.state.feedback.db is None
        with sqlite3.connect(tmp_path / 'state.db') as db:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='decisions'").fetchall()
        with client.websocket_connect('/ws', headers={'Authorization': 'Bearer test-token'}) as ws:
            ws.send_json({'type': 'hello', 'protocol': 2, 'conversation': str(uuid.uuid4()), 'thinking': True})
            assert ws.receive_json()['thinking'] is True
        assert requests == ['/info']
        assert server.app.state.feedback.db is not None
