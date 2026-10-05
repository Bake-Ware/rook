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
import uvicorn
import webrtcvad
from .jobs import Jobs
from .runtime import Connection
from .state import Store
from .identity import configured_identities, identity_for
from .admin import AdminStore, router as admin_router
from . import modes

VERSION = '2.0.0'
ROOT = Path(os.environ.get('VOICE_MODEL_DIR', '.'))
TOKEN = os.environ.get('VOICE_TOKEN', '')
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
    app.state.provider = Provider()
    app.state.store = Store(os.environ.get('VOICE_STATE_DB', str(ROOT / 'voice-state.sqlite3')))
    def notify(session, event):
        current = connections.get(session)
        if current:
            conn, queue = current
            conn.job_event(event)
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        elif not any(job['status'] == 'running' for job in app.state.store.jobs(session)):
            for credential, sessions in list(credential_sessions.items()):
                sessions.discard(session)
                if not sessions:
                    credential_sessions.pop(credential, None)
    app.state.jobs = Jobs(app.state.store, DIRECT_TOOLS, ACP_HOST, ACP_PORT, notify)
    yield
    await app.state.jobs.close()
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

@app.get('/health')
async def health():
    return {'ok': True, 'version': VERSION, 'protocol': 2}

@app.get('/voices')
async def voices():
    return {'voices': app.state.provider.voices, 'default': app.state.provider.default_voice}

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
    if not accepted and not (not TOKEN and os.environ.get('VOICE_ALLOW_ANONYMOUS') == '1'):
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
        queue = asyncio.Queue(maxsize=32)
        lock = asyncio.Lock()
        async def send_json(event):
            async with lock:
                await ws.send_json(event)
        async def send_bytes(data):
            async with lock:
                await ws.send_bytes(data)
        live_identities = ADMIN.mappings(IDENTITIES)
        if credential_id not in live_identities and not (not TOKEN and os.environ.get('VOICE_ALLOW_ANONYMOUS') == '1'):
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
        conn = Connection(app.state.store, app.state.jobs, app.state.provider, key, send_json, send_bytes, protocol,
                          identity=identity_for(supplied, live_identities))
        conn.full_duplex = protocol == 2 and hello.get('aec') is True
        conn.mode = mode
        connections[key] = conn, queue
        credential_sockets.setdefault(credential_id, []).append((ws, conn))
        credential_sessions.setdefault(credential_id, set()).add(key)
        await conn.emit('session', conversation=conversation, protocol=protocol, version=VERSION, full_duplex=conn.full_duplex,
                        mode=conn.mode.id, custom_mode=conn.mode.custom)
        await conn.emit('state', state='listening', turn=conn.epoch)
        for job in app.state.store.jobs(key):
            await conn.emit('tool', id=job['id'], title=job['name'], status=job['status'])
        async def progress():
            while True:
                event = await queue.get()
                await send_json({k: v for k, v in event.items() if k != 'result'})
                conn.drain_results()
        sender = asyncio.create_task(progress())
        vad = webrtcvad.Vad(3)
        preroll = deque(maxlen=10)
        utterance = bytearray()
        speech = silence = 0
        speech_permission_until = 0.0
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
                if playing and (not conn.full_duplex or time.monotonic() > speech_permission_until):
                    preroll.clear(); utterance.clear(); speech = silence = 0
                    continue
                sp = vad.is_speech(data, 16000)
                if sp:
                    if analysis is not None:
                        analysis.cancel(); analysis = None
                    if not utterance:
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
                if utterance and silence == 20 and speech >= 10:
                    analysis = asyncio.create_task(app.state.provider.turn_complete(bytes(utterance)))
                    analysis.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                complete = False
                if analysis and analysis.done() and not analysis.cancelled():
                    with contextlib.suppress(Exception):
                        complete = analysis.result()
                # Semantic completion, with a finite fallback for model uncertainty.
                if utterance and ((silence >= 30 and complete) or silence >= 125 or len(utterance) >= 16000 * 2 * 30):
                    if analysis is not None:
                        analysis.cancel(); analysis = None
                    pcm = bytes(utterance)
                    enough = speech >= 10
                    utterance.clear(); speech = silence = 0
                    conn.receiving_speech = False
                    if enough:
                        await conn.start(pcm=pcm)
                    else:
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
                        await conn.interrupt()
                    elif kind == 'speech_start' and conn.full_duplex:
                        speech_permission_until = time.monotonic() + 35
                        conn.receiving_speech = True
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
                    elif kind == 'voice' and msg.get('voice') in app.state.provider.voices:
                        conn.voice = msg['voice']
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
    uvicorn.run(app, host=os.environ.get('VOICE_BIND', '127.0.0.1'), port=int(os.environ.get('VOICE_PORT', '8900')),
                ssl_keyfile=os.environ.get('VOICE_TLS_KEY'), ssl_certfile=os.environ.get('VOICE_TLS_CERT'),
                log_level='warning', ws_max_size=8*1024*1024)
