"""Live protocol verification. Logs only lifecycle metadata, never tool output."""
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import ssl
import time
import uuid
import websockets


async def turn(text, slow=False, thinking=True):
    events, audio_bytes = [], 0
    async with websockets.connect('wss://127.0.0.1:8900/ws', ssl=ssl._create_unverified_context(),
            additional_headers={'Authorization': 'Bearer '+os.environ['VOICE_TOKEN']}) as ws:
        await ws.send(json.dumps({'type':'hello','protocol':2,'conversation':str(uuid.uuid4()),
                                  'activity':True,'thinking':thinking}))
        while json.loads(await ws.recv())['type'] != 'session': pass
        start = time.monotonic()
        await ws.send(json.dumps({'type':'text','text':text,'speak':False}))
        tool_done = False
        async with asyncio.timeout(230 if slow else 65):
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    audio_bytes += len(raw)-8
                    continue
                e = json.loads(raw)
                kind=e['type']
                # Keep actual progress template, but never assistant/tool result bodies.
                kept={k:e[k] for k in ('type','turn','seq','phase','engine_status','tool','worker','cap','status','elapsed_ms') if k in e}
                kept['at_s']=round(time.monotonic()-start,3)
                if e.get('phase')=='progress': kept['label']=e['label']
                events.append(kept)
                if kind=='error': raise AssertionError('Voice error')
                if e.get('phase')=='tool_result':
                    assert e['status']=='ok', e.get('detail','Tool failed')
                    tool_done=True
                if tool_done and kind=='assistant_done' and e['turn']>1:
                    break
            # Catch delayed shadow decisions and verify no late progress after result.
            try:
                async with asyncio.timeout(.6):
                    while True:
                        raw=await ws.recv()
                        if isinstance(raw,str):
                            e=json.loads(raw)
                            events.append({k:e[k] for k in ('type','turn','phase','engine_status') if k in e})
            except TimeoutError: pass
    decisions=[e for e in events if e['type']=='decision']
    turns={e['turn'] for e in events if e['type']=='assistant_done'}
    assert Counter(e['turn'] for e in decisions)==Counter({t:1 for t in turns}) if thinking else not decisions
    progress=[e for e in events if e.get('phase')=='progress']
    assert not slow or (progress and audio_bytes>0)
    result_index=next(i for i,e in enumerate(events) if e.get('phase')=='tool_result')
    assert not any(e.get('phase')=='progress' for e in events[result_index:])
    phases=[e['phase'] for e in events if e['type']=='activity']
    assert phases.index('heard')<phases.index('planning')<phases.index('planned')<phases.index('tool_start')<phases.index('tool_result')
    waits=[e for e in events if e.get('phase')=='tool_wait']
    if slow:
        assert len(waits)>=10
        assert all(1.5 <= b['at_s']-a['at_s'] <= 3.5 for a,b in zip(waits,waits[1:]))
    return dict(events=events,audio_bytes=audio_bytes,progress_count=len(progress),heartbeat_count=len(waits))


async def run(output):
    result={}
    result['rook_read']=await turn('Use rook_read to check info.uptime on kaiju.')
    print('ROOK_READ_OK',flush=True)
    result['slow']=await turn('On kaiju only, use Rook shell.exec to run sleep 70 with a 100 second timeout. This is an authorized progress test; do no other work and report completion.',slow=True)
    print('SLOW_PROGRESS_OK',flush=True)
    Path(output).write_text(json.dumps(result,indent=2))
    return result
