"""Kaiju-only replay and timing gates. Never executes model-selected tools.

Replay reads the production DB in read-only mode; its in-memory copy prevents
startup migrations, job recovery or retention from changing production records.
Only verdicts/timings are printed. Rejected outputs use the private planner log.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import ssl
import statistics
import time
import uuid


async def replay(args):
    from .providers import Provider, SAFE_FALLBACK
    from .state import Store
    from .workers import inventory
    await inventory.refresh()
    source = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    sessions = source.execute('SELECT DISTINCT session FROM events WHERE session LIKE ?',
                              (args.session_prefix + '%',)).fetchall()
    if len(sessions) != 1:
        raise ValueError('Replay session prefix must match exactly one stored session')
    session = sessions[0]['session']
    provider = Provider.__new__(Provider)  # planner only: no STT/TTS/GPU loading
    results = []
    for cutoff in (86, 89):
        rows = source.execute('SELECT * FROM events WHERE session=? AND id<=? ORDER BY id',
                              (session, cutoff)).fetchall()
        assert rows and rows[-1]['id'] == cutoff, 'Replay cutoff is missing'
        store = Store(':memory:')
        for row in rows:
            store.db.execute('INSERT INTO events VALUES(?,?,?,?,?)', tuple(row))
        store.db.commit()
        messages = [{'role': 'system', 'content': provider.system}] + store.messages(session)
        # Only job snapshots available as of the cutoff, never future job outcomes.
        jobs = [dict(r) for r in source.execute('SELECT * FROM jobs WHERE session=? AND updated<=? ORDER BY updated DESC LIMIT 20',
                                               (session, rows[-1]['created']))]
        # Job rows can have been updated since the cutoff. Reconstruct terminal
        # snapshots from the persisted tool result at/before the cutoff as needed.
        by_id = {j['id']: j for j in jobs}
        for row in rows:
            if row['kind'] == 'tool':
                b = json.loads(row['body'])
                outcome = json.loads(b['result'])
                by_id[b['id']] = {'id': b['id'], 'session': session, 'name': b['name'],
                                  'args': json.dumps(b.get('args', {})), **outcome, 'updated': row['created']}
        jobs = sorted(by_id.values(), key=lambda j: j['updated'], reverse=True)[:20]
        if jobs:
            messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'job_state', 'type': 'function', 'function': {'name': 'job_status', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'job_state', 'content': json.dumps(jobs)[:20000]}]
        internal = rows[-1]['kind'] == 'tool'
        if internal:
            b = json.loads(rows[-1]['body'])
            outcome = json.loads(b['result'])
            messages.append({'role': 'user', 'content': "Report the newly finished job's actual outcome briefly: " +
                             b['id'] + ' ' + outcome['status']})
        for repeat in range(args.repeats):
            spoken = []
            async def clause(text): spoken.append(text)
            start = time.monotonic()
            text, calls = await provider.chat(messages, clause, reply_only=internal)
            assert (len(calls) == 1 and not spoken) or (text and spoken)
            assert not (calls and spoken)
            result = {'cutoff': cutoff, 'repeat': repeat, 'reply_only': internal,
                      'outcome': 'safe_fallback' if text == SAFE_FALLBACK else 'function_call',
                      'function': calls[0]['function']['name'] if calls else ('fallback' if text == SAFE_FALLBACK else 'respond'),
                      'duration_ms': (time.monotonic()-start)*1000}
            results.append(result)
            print(json.dumps(result), flush=True)
        store.db.close()
    source.close()
    return {'replays': results, 'historical_jobs': 'reconstructed from cutoff events; no future outcomes'}


async def latency(args):
    import websockets
    results, conversations = [], []
    tls = ssl._create_unverified_context() if args.url.startswith('wss://127.0.0.1:') else None
    for thinking in (False, True):
        for sample in range(args.samples):
            cid = str(uuid.uuid4())
            conversations.append({'conversation': cid, 'thinking': thinking})
            async with websockets.connect(args.url, ssl=tls,
                    additional_headers={'Authorization': 'Bearer ' + os.environ['VOICE_TOKEN']}) as ws:
                async with asyncio.timeout(75):
                    await ws.send(json.dumps({'type': 'hello', 'protocol': 2, 'conversation': cid, 'thinking': thinking}))
                    while (await receive(ws))['type'] != 'session': pass
                    start = time.monotonic()
                    await ws.send(json.dumps({'type': 'text', 'text': 'What is two plus two? Reply in one short sentence.', 'speak': False}))
                    first, done, decisions = None, None, []
                    while done is None or (thinking and args.expect_decisions and not decisions):
                        event = await receive(ws)
                        assert event['type'] != 'error', 'Voice turn emitted an error'
                        if event['type'] == 'assistant_delta' and first is None:
                            first = (time.monotonic()-start)*1000
                        if event['type'] == 'assistant_done':
                            done = (time.monotonic()-start)*1000
                            turn = event['turn']
                        if event['type'] == 'decision': decisions.append(event)
                    try:
                        async with asyncio.timeout(.35):
                            while True:
                                event = await receive(ws)
                                assert event['type'] != 'error'
                                if event['type'] == 'decision': decisions.append(event)
                    except TimeoutError: pass
                    assert first is not None
                    assert len(decisions) == (1 if thinking and args.expect_decisions else 0)
                    if decisions:
                        assert decisions[0]['turn'] == turn
                        assert sample == 0 or decisions[0]['status'] == 'ok', decisions[0]['status']
                        assert decisions[0]['source'] == 'text' and decisions[0]['mode'] == 'shadow'
                    result = {'thinking': thinking, 'sample': sample, 'first_ms': first, 'done_ms': done,
                              'decision_count': len(decisions),
                              'decision_status': decisions[0]['status'] if decisions else None,
                              'decision_ms': decisions[0]['latency_ms'] if decisions else None}
                    results.append(result)
                    print(json.dumps(result), flush=True)
    return {'samples': results, 'conversations': conversations,
            'warm_median_ms': {str(t).lower(): statistics.median(r['done_ms'] for r in results if r['thinking'] == t and r['sample'] > 0)
                               for t in (False, True)}}


async def receive(ws):
    return json.loads(await ws.recv())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['replay', 'latency'])
    p.add_argument('--db', default='/home/bake/voice-agent/voice-state.sqlite3')
    p.add_argument('--session-prefix', default='6dfecb73')
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--samples', type=int, default=7)
    p.add_argument('--url', default='wss://127.0.0.1:8900/ws')
    p.add_argument('--expect-decisions', action='store_true')
    p.add_argument('--output', required=True)
    args = p.parse_args()
    assert args.samples >= 2 and args.repeats > 0
    result = asyncio.run(replay(args) if args.mode == 'replay' else latency(args))
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in ('samples', 'conversations', 'replays')}))


if __name__ == '__main__':
    main()
