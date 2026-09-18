"""Authenticated websocket latency gate; print metadata, never reply bodies."""
import asyncio
import json
import os
from pathlib import Path
import ssl
import statistics
import time
import uuid
import websockets


async def latency(output, expected=None, samples=9, url='wss://127.0.0.1:8900/ws'):
    results, conversations = [], []
    engine_log = Path('/home/bake/decision-engine/logs/decisions.jsonl')
    engine_counts = {}
    def count():
        return sum(json.loads(line).get('path') == '/decide' for line in engine_log.open()) if engine_log.exists() else None
    for opted in (False, True):
        before = count()
        for sample in range(samples):
            cid = str(uuid.uuid4())
            conversations.append({'conversation': cid, 'thinking': opted})
            async with websockets.connect(url, ssl=ssl._create_unverified_context(),
                    additional_headers={'Authorization': 'Bearer ' + os.environ['VOICE_TOKEN']}) as ws:
                await ws.send(json.dumps({'type': 'hello', 'protocol': 2, 'conversation': cid,
                                          'activity': opted, 'thinking': opted}))
                async with asyncio.timeout(90):
                    while json.loads(await ws.recv())['type'] != 'session': pass
                    start = time.monotonic()
                    await ws.send(json.dumps({'type': 'text', 'text': 'What is two plus two? Reply in one short sentence.', 'speak': False}))
                    first = done = None
                    decisions, activities = [], []
                    while done is None or (opted and expected and not decisions):
                        raw = await ws.recv()
                        if not isinstance(raw, str): continue
                        event = json.loads(raw)
                        assert event['type'] != 'error', 'Voice error'
                        if event['type'] == 'assistant_delta':
                            assert "could you say it again" not in event.get('text', ''), 'Planner fallback is not a valid latency reply'
                            if first is None: first = (time.monotonic()-start)*1000
                        if event['type'] == 'assistant_done':
                            done, turn = (time.monotonic()-start)*1000, event['turn']
                        if event['type'] == 'decision': decisions.append(event)
                        if event['type'] == 'activity': activities.append(event)
                    try:
                        async with asyncio.timeout(.4):
                            while True:
                                event = json.loads(await ws.recv())
                                if event['type'] == 'decision': decisions.append(event)
                                if event['type'] == 'activity': activities.append(event)
                    except TimeoutError: pass
                    assert first is not None
                    assert len(decisions) == int(opted and bool(expected))
                    if decisions:
                        assert decisions[0]['turn'] == turn
                        assert decisions[0]['engine_status'] == expected or (sample == 0 and expected == 'ok' and decisions[0]['engine_status'] == 'timeout'), decisions[0]['engine_status']
                    if expected and opted:
                        assert [e['phase'] for e in activities] == ['heard', 'planning', 'planned', 'done']
                        assert [e['seq'] for e in activities] == list(range(1,5))
                    if not opted: assert not activities
                    results.append(dict(thinking=opted, sample=sample, first_ms=first, done_ms=done,
                        decision_status=decisions[0]['engine_status'] if decisions else None))
        after = count()
        engine_counts[str(opted).lower()] = None if before is None else after-before
        if expected and not opted and before is not None:
            assert after == before, 'Unexpected engine log entries during thinking:false batch'
    result = {'engine_log_new_entries': engine_counts, 'samples': results, 'conversations': conversations,
              'warm_reply_ms': {str(t).lower(): statistics.median(r['first_ms'] for r in results if r['thinking']==t and r['sample']>0) for t in (False, True)},
              'warm_done_ms': {str(t).lower(): statistics.median(r['done_ms'] for r in results if r['thinking']==t and r['sample']>0) for t in (False, True)}}
    Path(output).write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('samples','conversations')}), flush=True)
    return result
