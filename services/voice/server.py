"""Run with python -m services.voice.server; runtime secrets stay in environment."""
import asyncio
from collections import deque
import contextlib
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from .jobs import Jobs
from .thinking import ThinkingAgent
from .runtime import Connection
from .state import Store
from .decision import DecisionClient, gate_threshold
from .feedback import FeedbackStore
from .identity import with_hello_device, configured_identities, identity_for
from .admin import AdminStore, router as admin_router
from . import modes
from . import pipeline
from .tts import resolve_voice

VERSION = '2.0.0'
# End of turn, in 20 ms frames of silence: Smart Turn scores the utterance at
# ANALYZE, a "complete" verdict ends it at COMPLETE, and MAX_SILENCE ends it
# regardless (2026-10-23: 400 ms / 2 s, were 600 ms / 2.5 s; Smart Turn takes
# ~65 ms on kaiju's CPU).
TURN_ANALYZE = int(os.environ.get('VOICE_TURN_ANALYZE_MS', '300')) // 20
TURN_COMPLETE = int(os.environ.get('VOICE_TURN_COMPLETE_MS', '400')) // 20
TURN_MAX_SILENCE = int(os.environ.get('VOICE_TURN_MAX_SILENCE_MS', '2000')) // 20
ROOT = Path(os.environ.get('VOICE_MODEL_DIR', '.'))
TOKEN = os.environ.get('VOICE_TOKEN', '')


def guest_allowed(supplied):
    """Keyless connections are guests when VOICE_ALLOW_ANONYMOUS=1, even with keys
    configured. A supplied key that isn't valid (revoked, mistyped) is refused,
    never downgraded to guest."""
    return not supplied and os.environ.get('VOICE_ALLOW_ANONYMOUS') == '1'
IDENTITIES = configured_identities()
if TOKEN:
    IDENTITIES.setdefault(hashlib.sha256(TOKEN.encode()).hexdigest(),
                          {'principal': os.environ.get('ROOK_VOICE_OWNER') or 'Owner', 'owner': True})
ADMIN = AdminStore(os.environ.get('VOICE_ADMIN_DB', str(ROOT / 'voice-admin.sqlite3')))
ADMIN.import_legacy(IDENTITIES)
connections = {}
credential_sockets = {}
credential_sessions = {}

@contextlib.asynccontextmanager
async def lifespan(app):
    from .providers import Provider, DIRECT_TOOLS, ACP_HOST, ACP_PORT
    from .workers import inventory
    # A host that loaded the models already (the hub's voice plugin builds the
    # Provider in a thread, so the hub's loop never blocks on it) hands it over.
    app.state.provider = getattr(app.state, 'preloaded_provider', None) or Provider()
    async def maintain_workers():
        while True:
            with contextlib.suppress(Exception):
                await inventory.refresh()
            await asyncio.sleep(30)
    async def maintain_schemas():
        while True:
            with contextlib.suppress(Exception):
                await inventory.refresh_schemas()
            await asyncio.sleep(60)
    worker_maintenance = asyncio.create_task(maintain_workers())
    schema_maintenance = asyncio.create_task(maintain_schemas())
    app.state.store = Store(os.environ.get('VOICE_STATE_DB', str(ROOT / 'voice-state.sqlite3')))
    app.state.decision = DecisionClient()
    app.state.feedback = None
    maintenance = None
    if app.state.decision.url:
        app.state.feedback = FeedbackStore(os.environ.get('VOICE_STATE_DB', str(ROOT / 'voice-state.sqlite3')))
        async def maintain_decisions():
            while True:
                await asyncio.sleep(60)
                if app.state.feedback.db is not None:
                    app.state.feedback.submit('prune')
                    await app.state.decision.refresh_info()
        maintenance = asyncio.create_task(maintain_decisions())
    def notify(session, event):
        current = connections.get(session)
        if current:
            conn, queue = current
            conn.job_event(event)
            conn.queue_event(event)
        elif not any(job['status'] == 'running' for job in app.state.store.jobs(session)):
            for credential, sessions in list(credential_sessions.items()):
                sessions.discard(session)
                if not sessions:
                    credential_sessions.pop(credential, None)
    app.state.jobs = Jobs(app.state.store, DIRECT_TOOLS, ACP_HOST, ACP_PORT, notify,
                          agent=ThinkingAgent(direct=DIRECT_TOOLS))
    # Speaker identification (shadow labels, echo guard, opt-in enrollment): only when
    # a model is configured; never a reason for the service not to start.
    app.state.speakers = None
    if os.environ.get('VOICE_SPEAKER_MODEL', '').strip():
        try:
            from .speaker import Speakers
            app.state.speakers = await asyncio.to_thread(
                Speakers, os.environ.get('VOICE_SPEAKER_DB', str(ROOT / 'voice-speakers.sqlite3')))
        except Exception as error:
            import logging
            logging.warning('Speaker identification not started: %s', type(error).__name__)
    learning = None
    speakers, provider = app.state.speakers, app.state.provider
    if speakers is not None and speakers.available and not speakers.has_assistant_voice(provider.default_voice):
        # Once per voice (cached in the store): before any session needs the echo guard.
        async def synthesize(text):
            return await provider.synthesize(text, provider.default_voice)
        learning = asyncio.create_task(speakers.learn_assistant_voice(provider.default_voice, synthesize))
        learning.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    yield
    if learning is not None:
        learning.cancel()
    if app.state.speakers is not None:
        app.state.speakers.close()
    worker_maintenance.cancel()
    schema_maintenance.cancel()
    await asyncio.gather(worker_maintenance, schema_maintenance, return_exceptions=True)
    if maintenance:
        maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance
    await app.state.decision.close()
    if app.state.feedback:
        await app.state.feedback.close()
    await app.state.jobs.close()
    if hasattr(app.state.provider, 'close'):
        await app.state.provider.close()
    app.state.store.db.close()

app = FastAPI(lifespan=lifespan)

async def invalidate_credential(credential):
    for session in list(credential_sessions.get(credential, set())):
        for job in app.state.store.jobs(session):
            if job['status'] == 'running':
                app.state.jobs.cancel(session, job['id'])
    for ws, conn in list(credential_sockets.get(credential, [])):
        await conn.close()
        with contextlib.suppress(Exception):
            await ws.close(code=4401)

app.include_router(admin_router(ADMIN, IDENTITIES, invalidate_credential))

from .pianobar_tts import install as install_pianobar_tts


def _pianobar_voice_authorized(supplied):
    """Same rule as the chat socket: a valid key, or no key at all when guests are allowed."""
    if not supplied:
        return guest_allowed(supplied)
    return hashlib.sha256(supplied.encode()).hexdigest() in ADMIN.mappings(IDENTITIES)


install_pianobar_tts(app, _pianobar_voice_authorized)

from .transcribe_api import install as install_transcribe

install_transcribe(app, _pianobar_voice_authorized)

@app.get('/health')
async def health():
    return {'ok': True, 'version': VERSION, 'protocol': 2}

@app.get('/voices')
async def voices():
    provider = app.state.provider
    tts = getattr(provider, 'tts', None)
    if tts is not None:
        # voices: bare ids, unique across engines (af_heart, sojourn); catalog adds engine + label.
        return tts.describe()
    return {'voices': provider.voices, 'default': provider.default_voice}

@app.get('/modes')
async def voice_modes():
    return modes.catalog()

@app.get('/')
async def index():
    custom = ROOT / 'static' / 'index.html'
    return FileResponse(custom if custom.exists() else Path(__file__).parent / 'static' / 'index.html')

@app.websocket('/ws')
async def websocket(ws: WebSocket):
    supplied = ws.headers.get('authorization', '').removeprefix('Bearer ') or ws.query_params.get('token', '')
    credential_id = hashlib.sha256(supplied.encode()).hexdigest()
    accepted = bool(supplied) and credential_id in ADMIN.mappings(IDENTITIES)
    if not accepted and not guest_allowed(supplied):
        await ws.close(code=4401)
        return
    await ws.accept()
    conn = None
    sender = None
    key = None
    analysis = None
    try:
        first = await asyncio.wait_for(ws.receive(), 10)
        hello = {}
        if first.get('text'):
            with contextlib.suppress(ValueError):
                hello = json.loads(first['text'])
        if not isinstance(hello, dict):
            hello = {}
        protocol = 2 if hello.get('type') == 'hello' and hello.get('protocol') == 2 else 1
        conversation = hello.get('conversation') if protocol == 2 else str(uuid.uuid4())
        principal = credential_id
        try:
            key = Store.key(principal, conversation)
        except (ValueError, TypeError, AttributeError):
            await ws.close(code=4400)
            return
        old = connections.get(key)
        if old:
            # Refuse overlapping ownership instead of letting an old socket mutate
            # the active conversation. Client reconnect must close its predecessor.
            await ws.send_json({'type': 'error', 'msg': 'Conversation already connected. Retry shortly.'})
            await ws.close(code=4409)
            return
        queue = asyncio.Queue(maxsize=256)
        lock = asyncio.Lock()
        async def send_json(event):
            async with lock:
                await ws.send_json(event)
        async def send_bytes(data):
            async with lock:
                await ws.send_bytes(data)
        live_identities = ADMIN.mappings(IDENTITIES)
        if credential_id not in live_identities and not guest_allowed(supplied):
            await ws.close(code=4401)
            return
        try:
            # Absent mode keeps old clients on assistant; an unknown one is refused
            # rather than silently granted the most capable mode.
            mode = modes.resolve(hello.get('mode'), hello.get('mode_prompt'))
        except modes.UnknownMode as error:
            await ws.send_json({'type': 'error', 'code': 'unknown_mode', 'msg': str(error)})
            await ws.close(code=4400)
            return
        # The reply gate records every voice turn, not only clients that opted into thinking events.
        if (app.state.feedback and app.state.feedback.db is None and
                ((protocol == 2 and hello.get('thinking') is True) or gate_threshold() is not None)):
            with contextlib.suppress(Exception):
                await app.state.feedback.open()
                await app.state.decision.refresh_info()
        def enqueue(event):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Never silently discard a decision or reorder activity seq.
                # A stalled consumer must reconnect instead of losing turn status.
                if conn and not conn.closed:
                    conn.closed = True
                    task = asyncio.create_task(ws.close(code=1013))
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        conn = Connection(app.state.store, app.state.jobs, app.state.provider, key, send_json, send_bytes, protocol,
                          app.state.decision, app.state.feedback, hello.get('thinking') is True, conversation,
                          activity=hello.get('activity') is True, enqueue=enqueue,
                          progress_updates=hello.get('progress_updates'), identity=identity_for(supplied, live_identities))
        conn.full_duplex = protocol == 2 and hello.get('aec') is True
        # Confirmed barge-in: the client pauses (not flushes) playback on local speech
        # and waits for "interrupt" (real words) or "resume" (it was nothing).
        conn.barge_confirm = protocol == 2 and hello.get('barge') == 'confirm'
        conn.speakers = getattr(app.state, 'speakers', None)
        # An owner key may name the device it is talking from (the app sends its worker
        # name), so "my battery / where am I / my calendar" default to that device. Never
        # for other keys: their device comes only from the key's server-side mapping.
        if protocol == 2:
            conn.identity = with_hello_device(conn.identity, hello.get('device'))
        conn.mode = mode
        # Opt-in per connection; anything but an exact "front_background" stays classic.
        conn.pipeline = pipeline.resolve(hello.get('pipeline')) if protocol == 2 else 'classic'
        if conn.pipeline == 'front_background':
            conn.fb = pipeline.FrontBackground(conn, background_events=hello.get('background') is True,
                                               timers=hello.get('timers') is True)
        connections[key] = conn, queue
        credential_sockets.setdefault(credential_id, []).append((ws, conn))
        credential_sessions.setdefault(credential_id, set()).add(key)
        extra = {'pipeline': conn.pipeline} if conn.fb else {}
        if conn.fb and conn.fb.house is not None:
            extra['engine'] = 'house'
        if conn.barge_confirm:
            extra['barge'] = 'confirm'
        await conn.emit('session', conversation=conversation, protocol=protocol, version=VERSION, full_duplex=conn.full_duplex,
                        mode=conn.mode.id, custom_mode=conn.mode.custom, thinking=conn.thinking,
                        activity=conn.activity_enabled, **extra)
        await conn.emit('state', state='listening', turn=conn.epoch)
        for job in app.state.store.jobs(key):
            await conn.emit('tool', id=job['id'], title=job['name'], status=job['status'])
            if job['status'] == 'running':
                timeout = app.state.jobs.timeout(job['name'], conn.identity.owner)
                elapsed = max(0, int((time.time()-job['updated'])*1000))
                conn.progress.start(job['id'], conn.epoch, job['name'], json.loads(job['args']), elapsed)
                if conn.activity:
                    conn.activity.start_job(job['id'], conn.epoch, job['name'], json.loads(job['args']),
                                            int(timeout * 1000), elapsed)
        async def progress():
            while True:
                event = await queue.get()
                try:
                    await asyncio.wait_for(send_json({k: v for k, v in event.items() if k != 'result'}), 5)
                except Exception:
                    await ws.close(code=1013)
                    return
                if event.get('type') == 'tool':
                    conn.drain_results()
        sender = asyncio.create_task(progress())
        if conn.fb:
            conn.fb.resend_timers()
            conn.fb.prefetch('session')
            conn.fb.load_house_context()
            conn.fb.warm()
        import webrtcvad   # here, not at import: a hub mounting this app imports it first
        vad = webrtcvad.Vad(3)
        preroll = deque(maxlen=10)
        utterance = bytearray()
        speech = silence = 0
        message = None if hello.get('type') == 'hello' else first
        while True:
            if message is None:
                message = await ws.receive()
            if message.get('type') == 'websocket.disconnect':
                break
            data = message.get('bytes')
            text = message.get('text')
            message = None
            if data is not None:
                if len(data) != 640:
                    continue
                playing = time.monotonic() < conn.play_until + .4
                if playing and (not conn.full_duplex or time.monotonic() > conn.speech_permission_until):
                    preroll.clear(); utterance.clear(); speech = silence = 0
                    continue
                if conn.held_since is not None:
                    resumed = conn.check_hold(bool(utterance))
                    if resumed is not None:
                        await resumed
                sp = vad.is_speech(data, 16000)
                if sp:
                    conn.last_speech = time.monotonic()
                    conn.progress.cancel_speech()
                    conn.shadow_hook('activity')
                    if analysis is not None:
                        analysis.cancel(); analysis = None
                    if not utterance:
                        if conn.fb:
                            conn.fb.prefetch('speech')
                        utterance.extend(b''.join(preroll))
                        preroll.clear()
                    utterance.extend(data)
                    speech += 1
                    silence = 0
                    conn.receiving_speech = True
                elif utterance:
                    utterance.extend(data)
                    silence += 1
                else:
                    preroll.append(data)
                if utterance and silence == TURN_ANALYZE and speech >= 10:
                    analysis = asyncio.create_task(app.state.provider.turn_complete(bytes(utterance)))
                    analysis.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                complete = False
                if analysis and analysis.done() and not analysis.cancelled():
                    with contextlib.suppress(Exception):
                        complete = analysis.result()
                # Semantic completion, with a finite fallback for model uncertainty.
                if utterance and ((silence >= TURN_COMPLETE and complete) or silence >= TURN_MAX_SILENCE
                                  or len(utterance) >= 16000 * 2 * 30):
                    if analysis is not None:
                        analysis.cancel(); analysis = None
                    pcm = bytes(utterance)
                    enough = speech >= 10
                    utterance.clear(); speech = silence = 0
                    conn.receiving_speech = False
                    if enough:
                        # Transcribed first; only real words may interrupt or start a turn.
                        conn.heard(pcm, conn.last_speech, held=conn.held_since is not None)
                    else:
                        if conn.held_since is not None and not conn.intake_pending:
                            await conn.release_hold(resume=True)
                        conn.drain_results()
            elif text:
                if len(text) > 8 * 1024 * 1024:
                    await conn.emit('error', msg='Message too large')
                    continue
                try:
                    msg = json.loads(text)
                    if not isinstance(msg, dict):
                        raise ValueError('Expected object')
                    kind = msg.get('type')
                    if kind == 'stop':
                        utterance.clear(); preroll.clear(); speech = silence = 0
                        conn.receiving_speech = False
                        if conn.fb:
                            # Stop means stop: results still on their way stay silent.
                            # Before interrupt, so no waiting follow-up starts in between.
                            conn.fb.stop()
                        await conn.interrupt()
                    elif kind == 'client_state':
                        was_sleeping = conn.sleeping
                        conn.sleeping = msg.get('mode') in ('sleep', 'off')
                        if conn.sleeping:
                            conn.progress.cancel_speech()
                        elif was_sleeping and conn.fb:
                            conn.fb.prefetch('wake')
                    elif kind == 'timer' and conn.fb:
                        conn.fb.client_timer(msg)
                    elif kind == 'speech_start' and conn.full_duplex:
                        conn.last_speech = time.monotonic()
                        conn.speech_permission_until = time.monotonic() + 35
                        conn.receiving_speech = True
                        if msg.get('hold') is True and conn.barge_confirm:
                            # Paused, not stopped: the transcript decides (Connection.heard).
                            conn.hold()
                        else:
                            if conn.fb:
                                conn.fb.drop_followups()
                            await conn.interrupt()
                    elif kind == 'audio_config':
                        conn.full_duplex = protocol == 2 and msg.get('aec') is True
                    elif kind == 'playback':
                        epoch, frames = int(msg.get('turn', -1)), int(msg.get('frames', 0))
                        if epoch in conn.audio_sent:
                            conn.audio_played[epoch] = min(max(0, frames), conn.audio_sent[epoch])
                    elif kind == 'text' and str(msg.get('text', '')).strip():
                        await conn.start(text=str(msg['text'])[:16000], speak=bool(msg.get('speak')))
                    elif kind == 'image' and msg.get('data'):
                        await conn.start(text=str(msg.get('text', ''))[:16000], image=msg['data'], speak=bool(msg.get('speak')))
                    elif kind == 'mode':
                        # Never fall back to assistant on a bad switch; keep the current mode.
                        try:
                            if msg.get('mode') is None:
                                raise modes.UnknownMode('Mode switch needs a mode.')
                            mode = modes.resolve(msg['mode'], msg.get('prompt'))
                        except modes.UnknownMode as error:
                            await conn.emit('error', code='unknown_mode', msg=f'{error} Mode unchanged: {conn.mode.id}.')
                        else:
                            await conn.set_mode(mode)
                    elif kind in ('models_get', 'models_set'):
                        # The app's Voice models screen: only through the house voice.
                        house = conn.fb.house if conn.fb else None
                        if house is None:
                            await conn.emit('error', code='models_unavailable',
                                            msg='Voice models need the house voice on this connection.')
                        else:
                            args = msg.get('args') if isinstance(msg.get('args'), dict) else {}
                            await house.control(kind, {k: str(v)[:80] for k, v in list(args.items())[:6]})
                    elif kind == 'voice':
                        # Accepts engine:name ids and legacy bare Kokoro ids; unknown ids are ignored.
                        voice = resolve_voice(app.state.provider, msg.get('voice'))
                        if voice is not None:
                            conn.voice = voice
                except (ValueError, TypeError):
                    await conn.emit('error', msg='Invalid voice message')
    except (WebSocketDisconnect, ConnectionError, TimeoutError):
        pass
    finally:
        if analysis is not None:
            analysis.cancel()
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
        if conn:
            sockets = credential_sockets.get(credential_id, [])
            if (ws, conn) in sockets:
                sockets.remove((ws, conn))
            if not sockets:
                credential_sockets.pop(credential_id, None)
            if not any(job['status'] == 'running' for job in app.state.store.jobs(key)):
                sessions = credential_sessions.get(credential_id, set())
                sessions.discard(key)
                if not sessions:
                    credential_sessions.pop(credential_id, None)
            await conn.close()
            if connections.get(key, (None,))[0] is conn:
                connections.pop(key, None)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('VOICE_BIND', '127.0.0.1'), port=int(os.environ.get('VOICE_PORT', '8900')),
                ssl_keyfile=os.environ.get('VOICE_TLS_KEY'), ssl_certfile=os.environ.get('VOICE_TLS_CERT'),
                log_level='warning', ws_max_size=8*1024*1024)
