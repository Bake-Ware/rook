"""House engine (VOICE_ENGINE=house): the house agent's voice session on the
session host answers front_background turns over a LAN websocket lane
(docs/design/persistent-agents.md 3.4). A fake lane server stands in for the
session host; Front and Background are mocked as in test_voice_front_background."""
import asyncio
import json
import logging
import socket
import time
import uuid

import pytest

pytest.importorskip("numpy")
pytest.importorskip("websockets")

from services.voice import house as house_mod
from services.voice.identity import Identity
from tests.test_voice_front_background import (  # noqa: F401 - server is a fixture
    DEVICE, GUEST, OWNER, Reads, Script, call, capture_timing, kinds, server, shutdown, wait_for)
from tests.test_voice_front_background import connection as fb_connection

TOKEN = 'house-secret-token-0123456789'


class FakeLane:
    """The session host's voice lane: checks the bearer token, says ready, records
    every client message and answers user turns with ``on_user(lane, message)``."""

    def __init__(self, on_user=None, token=TOKEN):
        self.on_user, self.token = on_user, token
        self.received, self.auth, self.sockets = [], [], []
        quiet = logging.getLogger('test.fake-lane')
        quiet.setLevel(logging.WARNING)     # websockets logs headers (the token) at debug
        self.quiet = quiet

    async def __aenter__(self):
        from websockets.asyncio.server import serve

        def process_request(conn, request):
            self.auth.append(request.headers.get('Authorization'))
            if request.headers.get('Authorization') != 'Bearer ' + self.token:
                return conn.respond(401, 'no\n')
            return None
        self.server = await serve(self.handle, '127.0.0.1', 0, process_request=process_request, logger=self.quiet)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}/voice/house'
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, ws):
        self.sockets.append(ws)
        await ws.send(json.dumps({'t': 'ready', 'role': 'house', 'session': 'voice-session-1',
                                  **getattr(self, 'config', {})}))
        async for raw in ws:
            message = json.loads(raw)
            self.received.append(message)
            if message.get('t') == 'user' and self.on_user is not None:
                task = asyncio.create_task(self.on_user(self, message))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def send(self, **message):
        for ws in self.sockets:
            await ws.send(json.dumps(message))

    def kinds(self):
        return [(m['t'], m.get('turn')) for m in self.received]


class FakeFront:
    """The local Front model (the fallback path)."""

    def __init__(self, reply='Local reply.'):
        self.reply, self.calls = reply, []

    async def __call__(self, messages, on_clause, on_token=None):
        self.calls.append(messages)
        if on_token:
            on_token()
        await on_clause(self.reply)
        return self.reply


def connection(tmp_path, **kw):
    """A front_background connection with no device reads (prefetch fails fast, offline)."""
    kw.setdefault('read', Reads({}))
    return fb_connection(tmp_path, **kw)


def house_env(monkeypatch, url, token=TOKEN, scope=None):
    monkeypatch.setenv('VOICE_ENGINE', 'house')
    monkeypatch.setenv('VOICE_HOUSE_URL', url)
    monkeypatch.setenv('VOICE_HOUSE_TOKEN', token)
    if scope is None:
        monkeypatch.delenv('VOICE_HOUSE_FOR', raising=False)
    else:
        monkeypatch.setenv('VOICE_HOUSE_FOR', scope)


def closed_port_url():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    return f'ws://127.0.0.1:{port}/voice/house'


def said(events):
    return [e['text'] for e in events if e.get('type') == 'assistant_delta']


async def idle(conn, extra=.05):
    house = conn.fb.house
    await wait_for(lambda: not conn.fb.backgrounds and (conn.task is None or conn.task.done())
                   and (house is None or not house.push_tasks))
    await asyncio.sleep(extra)


# --- selection ---------------------------------------------------------------
def test_engine_off_by_default(tmp_path, monkeypatch):
    for name in ('VOICE_ENGINE', 'VOICE_HOUSE_URL', 'VOICE_HOUSE_TOKEN', 'VOICE_HOUSE_FOR'):
        monkeypatch.delenv(name, raising=False)
    assert house_mod.settings() is None
    assert house_mod.settings({'VOICE_ENGINE': 'front_background', 'VOICE_HOUSE_URL': 'ws://x'}) is None
    assert house_mod.settings({'VOICE_ENGINE': 'house'}) is None          # no URL: stays off
    front = FakeFront()

    async def scenario():
        conn, store, jobs, events, provider = connection(tmp_path, front=front, complete=Script(call('no_action')))
        try:
            assert conn.fb.house is None
            await conn.start(text='hello', speak=False)
            await idle(conn)
        finally:
            await shutdown(conn, store, jobs)
        assert said(events) == ['Local reply.'] and len(front.calls) == 1 and provider.chats == 0
    asyncio.run(scenario())


def test_engine_on_for_owner_only_unless_all(tmp_path, monkeypatch):
    house_env(monkeypatch, 'ws://127.0.0.1:9/voice/house')
    config = house_mod.settings()
    assert config.scope == 'owner' and TOKEN not in repr(config)
    assert house_mod.applies(config, OWNER)
    assert not house_mod.applies(config, DEVICE) and not house_mod.applies(config, GUEST)
    assert house_mod.applies(config._replace(scope='all'), GUEST)

    async def scenario():
        for identity, expected in ((OWNER, True), (DEVICE, False), (GUEST, False)):
            conn, store, jobs, _, _ = connection(tmp_path, identity=identity, front=FakeFront())
            try:
                assert (conn.fb.house is not None) is expected, identity
            finally:
                await shutdown(conn, store, jobs)
        monkeypatch.setenv('VOICE_HOUSE_FOR', 'all')
        conn, store, jobs, _, _ = connection(tmp_path, identity=GUEST, front=FakeFront())
        try:
            assert conn.fb.house is not None
        finally:
            await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_non_agent_mode_keeps_the_old_path(tmp_path, monkeypatch):
    async def scenario():
        async with FakeLane() as lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, mode_id='conversation',
                                                      complete=Script(call('no_action')))
            try:
                await conn.start(text='tell me a story', speak=False)
                await idle(conn)
            finally:
                await shutdown(conn, store, jobs)
            assert said(events) == ['Local reply.'] and lane.received == []
    asyncio.run(scenario())


# --- a turn --------------------------------------------------------------------
def test_deltas_become_spoken_clauses_with_history_echo_and_timing(tmp_path, monkeypatch):
    lines = capture_timing(monkeypatch)

    async def answer(lane, message):
        turn = message['turn']
        await lane.send(t='delta', turn=turn, text='Sure, I can look ')
        await lane.send(t='delta', turn=turn, text='into that for you. Checking')
        await lane.send(t='tool', turn=turn, name='status')
        await asyncio.sleep(.2)                      # the conductor's tool call
        await lane.send(t='delta', turn=turn, text='The build is green, all tests passed. ')
        await lane.send(t='end', turn=turn, text='', ok=True, first_ms=5, total_ms=300)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, provider = connection(tmp_path, front=front, background_events=True,
                                                             complete=Script())
            try:
                conn.last_speech = time.monotonic() - .1
                await conn.start(text='how is the build', speak=True)
                await idle(conn)
                history = [m['content'] for m in store.messages('session')]
                echo = conn.echo_text('the build is green all tests passed')
            finally:
                await shutdown(conn, store, jobs)
            assert lane.auth == ['Bearer ' + TOKEN]
            user = [m for m in lane.received if m['t'] == 'user']
            assert user == [{'t': 'user', 'turn': 1, 'text': 'how is the build',
                             'identity': {'owner': True, 'name': 'Alex'}}]
            assert 'cancel' not in [m['t'] for m in lane.received]
            assert said(events) == ['Sure, I can look into that for you.', 'Checking',
                                    'The build is green, all tests passed.']
            assert front.calls == [] and provider.chats == 0 and not conn.fb.backgrounds
            # History is what was heard; the echo guard knows what was just said.
            assert history == ['how is the build', 'Sure, I can look into that for you. Checking '
                                                   'The build is green, all tests passed.']
            assert echo
            # Front's transcript has the exchange too (a later fallback extends it).
            assert conn.fb.transcript.messages[-1] == {'role': 'assistant', 'content': history[-1]}
            tool = [e for e in events if e.get('type') == 'background' and e['kind'] == 'tool_call']
            assert [e['tool'] for e in tool] == ['status']
            assert len(lines) == 1 and lines[0]['engine'] == 'house' and lines[0]['status'] == 'ok'
            t = lines[0]
            assert t['front_first_token_ms'] <= t['front_first_audio_ms'] == t['first_audio_ms'] <= t['total_ms']
            assert 'house_fallback' not in t and 'background_ms' not in t
    asyncio.run(scenario())


def test_whole_reply_in_end_and_speaker_name(tmp_path, monkeypatch):
    async def answer(lane, message):
        await lane.send(t='end', turn=message['turn'], text='It is seven. Anything else?', ok=True)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url, scope='all')
            conn, store, jobs, events, _ = connection(tmp_path, identity=Identity('Kid', worker='kidphone'),
                                                      front=FakeFront())
            try:
                await conn.start(text='what time is it', speak=False)
                await idle(conn)
            finally:
                await shutdown(conn, store, jobs)
            assert said(events) == ['It is seven.', 'Anything else?']
            assert lane.received[0]['identity'] == {'owner': False, 'name': 'Kid'}
    asyncio.run(scenario())
    engine = house_mod.HouseVoice.__new__(house_mod.HouseVoice)
    engine.conn = type('C', (), {'identity': OWNER})()
    # The key names its person; speaker ID says who was heard (the hub decides what it means).
    timing = type('T', (), {'extra': {'speaker': 'Sam', 'speaker_score': 0.42}})()
    assert engine.identity(timing) == {'owner': True, 'name': 'Alex',
                                       'speaker': {'label': 'Sam', 'score': 0.42, 'sure': False}}
    # Any match the server names (>= 0.45) is sure: "more than half sure" counts (Bake, 2026-10-25).
    timing.extra.update(speaker='Alex', speaker_score=0.52)
    assert engine.identity(timing)['speaker'] == {'label': 'Alex', 'score': 0.52, 'sure': True}
    timing.extra.update(speaker='Alex', speaker_score=0.731)
    assert engine.identity(timing)['speaker'] == {'label': 'Alex', 'score': 0.731, 'sure': True}
    timing.extra.update(speaker='guest', speaker_score=0.2)
    assert engine.identity(timing)['speaker'] == {'label': 'guest', 'score': 0.2, 'sure': False}
    timing.extra['speaker'] = 'assistant'                         # our own echo: no speaker
    assert engine.identity(timing) == {'owner': True, 'name': 'Alex'}
    engine.conn.identity = GUEST
    assert engine.identity(None) == {'owner': False, 'name': 'guest'}


def test_barge_in_and_newer_turn_send_cancel(tmp_path, monkeypatch):
    async def answer(lane, message):
        await lane.send(t='delta', turn=message['turn'], text='Let me think about that for a while. ')
        # ... and never ends: the client interrupts.

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront())
            try:
                await conn.start(text='first question', speak=True)
                await wait_for(lambda: conn.played_samples(conn.epoch) > 0)
                first = conn.epoch
                await conn.interrupt()               # barge-in
                await wait_for(lambda: ('cancel', first) in lane.kinds())
                await conn.start(text='second question', speak=True)
                second = conn.epoch
                await wait_for(lambda: ('user', second) in lane.kinds())
                await conn.start(text='third question', speak=False)   # a newer turn
                third = conn.epoch
                await wait_for(lambda: ('cancel', second) in lane.kinds() and ('user', third) in lane.kinds())
                history = [m['content'] for m in store.messages('session')]
            finally:
                await shutdown(conn, store, jobs)
            assert lane.kinds()[:2] == [('user', first), ('cancel', first)]
            assert lane.kinds().index(('cancel', second)) < lane.kinds().index(('user', third))
            # What was heard of the interrupted reply, nothing more.
            reply = 'Let me think about that for a while.'
            assert history[0] == 'first question' and reply not in history
            cut = [h for h in history if h.endswith('[cut off]')]
            assert cut and all(reply.startswith(h[:-len(' [cut off]')]) for h in cut)
    asyncio.run(scenario())


# --- pushed back-end results --------------------------------------------------------
def test_push_is_spoken_as_a_followup_after_the_reply(tmp_path, monkeypatch):
    async def answer(lane, message):
        turn = message['turn']
        await lane.send(t='delta', turn=turn, text='On it. ')
        await lane.send(t='end', turn=turn, text='On it.', ok=True)
        # The back end finished: an unsolicited turn, while the reply may still be playing.
        await lane.send(t='push', turn='p1')
        await lane.send(t='delta', turn='p1', text='Your deploy finished. ')
        await lane.send(t='delta', turn='p1', text='All checks passed.')
        await lane.send(t='end', turn='p1', text='Your deploy finished. All checks passed.', ok=True)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), background_events=True)
            try:
                await conn.start(text='deploy it', speak=True)
                await wait_for(lambda: len(said(events)) >= 3)
                await idle(conn)
                history = [m['content'] for m in store.messages('session')]
            finally:
                await shutdown(conn, store, jobs)
            assert said(events) == ['On it.', 'Your deploy finished.', 'All checks passed.']
            assert history == ['deploy it', 'On it.', 'Your deploy finished. All checks passed.']
            assert 'followup' in kinds(events)
            assert ('cancel', 'p1') not in lane.kinds()
            assert conn.fb.transcript.messages[-1]['content'].endswith('All checks passed.')
    asyncio.run(scenario())


def test_push_not_spoken_after_stop_is_reported(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger='voice.house')

    async def scenario():
        async with FakeLane() as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), background_events=True)
            try:
                await conn.fb.house.lane.ensure()
                conn.receiving_speech = True         # the user is talking: the result waits
                await lane.send(t='push', turn=7)
                await lane.send(t='delta', turn=7, text='The backup is done.')
                await lane.send(t='end', turn=7, ok=True)
                await asyncio.sleep(.1)
                assert not said(events)
                conn.fb.stop()                        # the client said stop
                await wait_for(lambda: 'dropped' in kinds(events))
            finally:
                await shutdown(conn, store, jobs)
            dropped = next(e for e in events if e.get('type') == 'background' and e['kind'] == 'dropped')
            assert dropped['result'] == 'The backup is done.' and not said(events)
            assert 'not spoken (stopped)' in caplog.text and 'The backup is done' not in caplog.text
    asyncio.run(scenario())


# --- fallback ------------------------------------------------------------------------
def test_unreachable_lane_falls_back_to_front_then_backs_off(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    lines = capture_timing(monkeypatch)
    house_env(monkeypatch, closed_port_url())
    front = FakeFront('One moment.')

    async def scenario():
        conn, store, jobs, events, _ = connection(tmp_path, front=front, background_events=True,
                                                  complete=Script(call('no_action'), call('no_action')))
        try:
            started = time.monotonic()
            await conn.start(text='hello there', speak=False)
            await wait_for(lambda: len(lines) == 1)
            await conn.start(text='still there?', speak=False)
            await wait_for(lambda: len(lines) == 2)
            elapsed = time.monotonic() - started
        finally:
            await shutdown(conn, store, jobs)
        assert said(events) == ['One moment.', 'One moment.'] and len(front.calls) == 2
        assert [line['engine'] for line in lines] == ['front_background'] * 2
        assert [line['house_fallback'] for line in lines] == ['unreachable', 'backoff']
        assert all('background_ms' in line for line in lines)      # the old path ran: Front + Background
        assert elapsed < 3
        assert 'unreachable' in caplog.text and TOKEN not in caplog.text
        assert any(e.get('kind') == 'error' and 'House voice unavailable' in e['text'] for e in events)
    asyncio.run(scenario())


def test_host_that_accepts_and_says_nothing_costs_one_connect_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(house_mod, 'CONNECT_S', .3)
    lines = capture_timing(monkeypatch)

    async def scenario():
        held = []

        async def accept(reader, writer):
            held.append(writer)                 # TCP open, never an HTTP answer
        tcp = await asyncio.start_server(accept, '127.0.0.1', 0)
        house_env(monkeypatch, f'ws://127.0.0.1:{tcp.sockets[0].getsockname()[1]}/voice/house')
        front = FakeFront()
        conn, store, jobs, events, _ = connection(tmp_path, front=front,
                                                  complete=Script(call('no_action'), call('no_action')))
        try:
            started = time.monotonic()
            await conn.start(text='one', speak=False)
            await wait_for(lambda: len(lines) == 1)
            front.reply = 'Another local reply.'      # not a repeat (Front would ask again)
            first = time.monotonic() - started
            await conn.start(text='two', speak=False)
            await wait_for(lambda: len(lines) == 2)
        finally:
            await shutdown(conn, store, jobs)
            for writer in held:
                writer.close()
            tcp.close()
        assert [line['house_fallback'] for line in lines] == ['timeout', 'backoff']
        assert first < 2 and len(front.calls) == 2
    asyncio.run(scenario())


def test_no_first_delta_falls_back_and_cancels(tmp_path, monkeypatch):
    monkeypatch.setattr(house_mod, 'FIRST_DELTA_S', .3)
    lines = capture_timing(monkeypatch)

    async def silent(lane, message):
        pass

    async def scenario():
        async with FakeLane(silent) as lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, complete=Script(call('no_action')))
            try:
                await conn.start(text='are you there', speak=False)
                await wait_for(lambda: lines)
                await wait_for(lambda: ('cancel', 1) in lane.kinds())
            finally:
                await shutdown(conn, store, jobs)
            assert lane.kinds() == [('user', 1), ('cancel', 1)]
            assert said(events) == ['Local reply.'] and len(front.calls) == 1
            assert lines[0]['house_fallback'] == 'no_first_delta'
    asyncio.run(scenario())


def test_lane_error_before_words_falls_back(tmp_path, monkeypatch):
    async def fail(lane, message):
        await lane.send(t='error', turn=message['turn'], error='voice lane is busy')

    async def scenario():
        async with FakeLane(fail) as lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, complete=Script(call('no_action')))
            try:
                await conn.start(text='hi', speak=False)
                await wait_for(lambda: front.calls)
                await idle(conn)
            finally:
                await shutdown(conn, store, jobs)
            assert said(events) == ['Local reply.'] and ('cancel', 1) not in lane.kinds()
    asyncio.run(scenario())


def test_token_never_in_logs_or_events(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    lines = capture_timing(monkeypatch)

    async def answer(lane, message):
        await lane.send(t='delta', turn=message['turn'], text='Hello. ')
        await lane.send(t='error', turn=None, error='bad bearer ' + TOKEN)
        await lane.send(t='end', turn=message['turn'], ok=True)

    async def scenario():
        everything = []
        # A refusing lane (it expects another token), then a working one.
        async with FakeLane(token='another-token') as refusing:
            house_env(monkeypatch, refusing.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), background_events=True,
                                                      complete=Script(call('no_action')))
            try:
                await conn.start(text='hi', speak=False)
                await wait_for(lambda: lines)
            finally:
                await shutdown(conn, store, jobs)
            everything += events
            assert refusing.auth == ['Bearer ' + TOKEN]
            assert lines[0]['house_fallback'] == 'refused'
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), background_events=True)
            try:
                await conn.start(text='hi', speak=False)
                await wait_for(lambda: len(lines) == 2)
                assert TOKEN not in repr(conn.fb.house.lane) and TOKEN not in repr(house_mod.settings())
            finally:
                await shutdown(conn, store, jobs)
            everything += events
        return everything

    events = asyncio.run(scenario())
    assert 'refused (HTTP 401)' in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(events) and TOKEN not in json.dumps(lines)
    assert 'bad bearer ***' in caplog.text


# --- the server ------------------------------------------------------------------------
def test_server_session_reports_engine_and_falls_back(server, monkeypatch):
    from fastapi.testclient import TestClient
    house_env(monkeypatch, closed_port_url())
    with TestClient(server.app) as client:
        with client.websocket_connect('/ws', headers={'Authorization': 'Bearer test-token'}) as ws:
            ws.send_json({'type': 'hello', 'protocol': 2, 'conversation': str(uuid.uuid4()),
                          'pipeline': 'front_background'})
            session = ws.receive_json()
            assert session['pipeline'] == 'front_background' and session['engine'] == 'house'
            ws.send_json({'type': 'text', 'text': 'hello', 'speak': False})
            events = []
            while (event := ws.receive_json())['type'] != 'assistant_delta':
                events.append(event)
            assert event['text'] == 'One moment.'
            ws.send_json({'type': 'stop'})
        for extra in ({}, {'pipeline': 'classic'}):
            with client.websocket_connect('/ws', headers={'Authorization': 'Bearer test-token'}) as ws:
                ws.send_json({'type': 'hello', 'protocol': 2, 'conversation': str(uuid.uuid4()), **extra})
                assert 'engine' not in ws.receive_json()


# --- listening modes switched by the voice, routing lines --------------------------
def test_the_voice_switches_to_dictation_and_the_text_comes_back(tmp_path, monkeypatch):
    async def answer(lane, message):
        turn = message['turn']
        if message['text'].startswith('[dictation]'):
            await lane.send(t='delta', turn=turn, text='Got it. Save it as a note?')
        else:
            await lane.send(t='delta', turn=turn, text='Okay, dictation.')
        await lane.send(t='end', turn=turn, text='', ok=True)
        if 'dictation' in message['text'] and not message['text'].startswith('[dictation]'):
            await lane.send(t='mode', turn=turn, mode='dictate', by='voice')

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
            try:
                await conn.start(text='switch to dictation', speak=False)
                await wait_for(lambda: conn.mode.id == 'dictate')
                assert {'type': 'mode', 'mode': 'dictate', 'custom': False, 'by': 'voice'} in events
                await idle(conn)
                await conn.start(text='buy milk and eggs', speak=False)
                await idle(conn)
                await conn.start(text="I'm done", speak=False)
                await wait_for(lambda: any(m.get('text') == '[dictation] buy milk and eggs'
                                           for m in lane.received))
                await idle(conn, .2)
                assert conn.mode.id == 'assistant' and conn.mode_return is None
                modes_seen = [(e['mode'], e.get('by')) for e in events if e.get('type') == 'mode']
                assert modes_seen == [('dictate', 'voice'), ('assistant', 'voice')]
                assert 'Got it. Save it as a note?' in ' '.join(said(events))
                users = [m['text'] for m in lane.received if m['t'] == 'user']
                assert 'buy milk and eggs' not in users              # dictation never reached the model
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_voice_mode_switches_never_touch_the_childrens_modes(tmp_path, monkeypatch):
    async def scenario():
        async with FakeLane() as lane:
            house_env(monkeypatch, lane.url)
            for start, target, by in (('conversation', 'assistant', 'voice'),
                                      ('assistant', 'roleplay', 'voice'),
                                      ('assistant', 'dictate', None)):
                conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), mode_id=start)
                try:
                    await conn.fb.house._switch(target, by)
                    assert conn.mode.id == start and not [e for e in events if e.get('type') == 'mode']
                finally:
                    await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_active_listening_is_served_by_the_house_voice_and_says_its_mode(tmp_path, monkeypatch):
    async def answer(lane, message):
        # Not addressed: the conductor's whole reply was a quiet routing line (cut by the host).
        await lane.send(t='end', turn=message['turn'], text='', ok=True)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, mode_id='listen',
                                                      complete=Script())
            try:
                await conn.start(text='so then we went to the store', speak=False)
                await idle(conn)
            finally:
                await shutdown(conn, store, jobs)
            (user,) = [m for m in lane.received if m['t'] == 'user']
            assert user['mode'] == 'listen' and said(events) == [] and front.calls == []
    asyncio.run(scenario())


def test_a_routing_line_that_reaches_the_voice_server_is_never_spoken(tmp_path, monkeypatch):
    async def answer(lane, message):
        turn = message['turn']
        await lane.send(t='delta', turn=turn, text='On it. <<flow {"op":')
        await lane.send(t='delta', turn=turn, text=' "queue", "request": "x"}>>')
        await lane.send(t='end', turn=turn, text='', ok=True)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
            try:
                await conn.start(text='check the backups', speak=False)
                await idle(conn)
            finally:
                await shutdown(conn, store, jobs)
            text = ' '.join(said(events))
            assert 'On it.' in text and '<<' not in text and 'queue' not in text
    asyncio.run(scenario())


# --- the hub's engine choice and the phone's Voice models screen ---------------------
def test_the_hub_choosing_the_local_engine_sends_turns_down_the_old_path(tmp_path, monkeypatch):
    async def scenario():
        lane = FakeLane()
        lane.config = {'engine': 'local', 'models': {'front': {'model': 'local'}}}
        async with lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, complete=Script(call('no_action')))
            try:
                await conn.start(text='hello', speak=False)
                await idle(conn)
                assert said(events) == ['Local reply.'] and len(front.calls) == 1
                assert not [m for m in lane.received if m['t'] == 'user']
                assert not [e for e in events if e.get('type') == 'error']      # a choice, not a failure
                # The hub switches back to the house voice: the next turn goes to the lane.
                await lane.send(t='config', engine='house', models={})
                await wait_for(lambda: conn.fb.house.lane.config.get('engine') == 'house')
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_the_phones_models_screen_goes_through_the_lane_as_its_identity(tmp_path, monkeypatch):
    async def scenario():
        async with FakeLane() as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
            try:
                await conn.fb.house.control('models_set', {'back': 'sonnet'})
                await wait_for(lambda: any(m['t'] == 'control' for m in lane.received))
                (msg,) = [m for m in lane.received if m['t'] == 'control']
                assert msg['op'] == 'models_set' and msg['args'] == {'back': 'sonnet'}
                assert msg['identity']['owner'] is True
                await lane.send(t='models', turn=None, ok=True, back={'model': 'sonnet'})
                await wait_for(lambda: any(e.get('type') == 'voice_models' for e in events))
                ev = next(e for e in events if e.get('type') == 'voice_models')
                assert ev['ok'] is True and ev['back'] == {'model': 'sonnet'}
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())



# --- expressive voice (sound tags, as Pianobar's DJ) ---------------------------------
def test_sound_tags_are_kept_only_when_offered_and_shown_as_stage_directions(tmp_path, monkeypatch):
    from services.voice import sounds as S
    assert S.keep_sounds('Ha [Laugh] that [dance] one [clear   throat] works.', True) == \
        'Ha [laugh] that one [clear throat] works.'
    assert S.keep_sounds('Ha [laugh] okay.', False) == 'Ha okay.'
    assert S.stage_directions('Ha [laugh] okay [banana].') == 'Ha (laughs) okay.'
    monkeypatch.setattr(house_mod, 'SOUND_EVERY', 1)

    async def answer(lane, message):
        await lane.send(t='delta', turn=message['turn'], text='Oh no [sigh] that is a shame. [wink]')
        await lane.send(t='end', turn=message['turn'], text='', ok=True)

    async def scenario():
        for expressive in (True, False):
            async with FakeLane(answer) as lane:
                house_env(monkeypatch, lane.url)
                conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
                conn.expressive = expressive
                monkeypatch.setattr(house_mod.HouseVoice, 'sounds',
                                    lambda self: () if self.conn.expressive is False else house_mod.HOUSE_TAGS)
                spoken = []
                real_say = conn.say

                async def say(text, epoch, **kw):
                    spoken.append(text)
                    return await real_say(text, epoch, **kw)
                conn.say = say
                try:
                    await conn.start(text='the trip got cancelled', speak=False)
                    await idle(conn)
                finally:
                    await shutdown(conn, store, jobs)
                (user,) = [m for m in lane.received if m['t'] == 'user']
                text = ' '.join(spoken)
                if expressive:
                    assert user['sound_tags'] == list(house_mod.HOUSE_TAGS)
                    assert '[sigh]' in text and '[wink]' not in text
                    assert '(sighs)' in ' '.join(said(events))          # the screen's version
                else:
                    assert 'sound_tags' not in user
                    assert '[' not in text and '(sighs)' not in ' '.join(said(events))
    asyncio.run(scenario())


# --- people by voice, ending listening, scribe ------------------------------------------
class FakeSpeakers:
    available = True

    def __init__(self):
        self.prints, self.samples = {'Alex'}, {}

    def enrollment_sentences(self):
        return ['One two three.', 'Four five six.', 'Seven eight nine.']

    async def enroll_add(self, principal, pcm):
        n = self.samples.get(principal, 0) + 1
        self.samples[principal] = n
        return {'ok': True, 'samples': n, 'needed': max(0, 3 - n)}

    def enroll_finish(self, principal):
        if self.samples.get(principal, 0) >= 3:
            self.prints.add(principal)
            return True
        return False

    def forget(self, principal):
        gone = principal in self.prints
        self.prints.discard(principal)
        return gone

    def enrolled(self):
        return sorted(self.prints)


def test_the_hub_starts_a_named_enrollment_and_hears_how_it_went(tmp_path, monkeypatch):
    async def scenario():
        async with FakeLane() as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
            conn.speakers = FakeSpeakers()
            try:
                await conn.fb.house.lane.ensure()
                await lane.send(t='enroll_start', name='Robin')
                await wait_for(lambda: conn.enrolling is not None)
                assert conn.enrolling['principal'] == 'Robin' and conn.enrolling['other']
                await wait_for(lambda: any('Please read: One two three.' in t for t in said(events)))
                for _ in range(3):
                    assert await conn._voice_command('reading it', b'\0' * 32000, False)
                await wait_for(lambda: any(m.get('op') == 'enrolled' for m in lane.received))
                (done,) = [m for m in lane.received if m.get('op') == 'enrolled']
                assert done['args'] == {'name': 'Robin', 'samples': '3'} and conn.enrolling is None
                await wait_for(lambda: any("Thanks, Robin" in t for t in said(events)))
                await lane.send(t='voices_list')
                await wait_for(lambda: any(m.get('op') == 'voices' for m in lane.received))
                assert [m for m in lane.received if m.get('op') == 'voices'][0]['args'] == {'names': 'Alex,Robin'}
                await lane.send(t='voice_forget', name='Robin')
                await wait_for(lambda: any(m.get('op') == 'forgot' for m in lane.received))
                assert 'Robin' not in conn.speakers.prints
                await lane.send(t='listen_end', quiet_minutes=10)
                await wait_for(lambda: any(e.get('type') == 'bye' for e in events))
                bye = next(e for e in events if e.get('type') == 'bye')
                assert bye['mode'] == 'sleep' and bye['quiet_ms'] == 600000 and bye['after_ms'] >= 400
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_scribe_sends_lines_and_the_assistants_name_ends_it(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_VOICE_ASSISTANT_NAME', 'Sojourn')

    async def answer(lane, message):
        await lane.send(t='delta', turn=message['turn'], text='Yes?')
        await lane.send(t='end', turn=message['turn'], text='', ok=True)

    async def scenario():
        async with FakeLane(answer) as lane:
            house_env(monkeypatch, lane.url)
            conn, store, jobs, events, _ = connection(tmp_path, front=FakeFront(), complete=Script())
            try:
                await conn.fb.house.lane.ensure()
                await lane.send(t='mode', mode='scribe', by='voice')
                await wait_for(lambda: conn.mode.id == 'scribe')
                await conn.start(text='we should call the plumber', speak=False)
                await idle(conn)
                lines = [m for m in lane.received if m.get('op') == 'scribe']
                assert lines and lines[0]['args']['text'] == 'we should call the plumber'
                assert lines[0]['args']['speaker'] == 'Speaker'
                assert not [m for m in lane.received if m['t'] == 'user']      # no replies while scribing
                await conn.start(text='Sojourn, what did we decide?', speak=False)
                await wait_for(lambda: any(m['t'] == 'user' for m in lane.received))
                await idle(conn, .2)
                assert conn.mode.id == 'assistant'
                ends = [m for m in lane.received if m.get('op') == 'scribe' and m['args'].get('end')]
                assert ends
                (user,) = [m for m in lane.received if m['t'] == 'user']
                assert user['text'] == 'Sojourn, what did we decide?'
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_on_the_local_models_the_way_back_is_caught_here(tmp_path, monkeypatch):
    async def scenario():
        lane = FakeLane()
        lane.config = {'engine': 'local'}
        async with lane:
            house_env(monkeypatch, lane.url)
            front = FakeFront()
            conn, store, jobs, events, _ = connection(tmp_path, front=front, complete=Script())
            try:
                await conn.start(text='switch back to claude please', speak=False)
                await idle(conn)
                (ctl,) = [m for m in lane.received if m['t'] == 'control']
                assert ctl['op'] == 'models_set' and ctl['args'] == {'front': 'claude-haiku-5-5'}
                assert 'Okay, switching back.' in said(events) and front.calls == []
            finally:
                await shutdown(conn, store, jobs)
    asyncio.run(scenario())
