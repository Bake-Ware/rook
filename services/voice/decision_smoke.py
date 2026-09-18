"""Read-only shadow deployment checks; only synthetic input and timing are printed."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import ssl
import time
import uuid

import httpx
import websockets

from .decision import DecisionClient


async def check(samples=4):
    url = os.environ.get('VOICE_SMOKE_URL', 'wss://127.0.0.1:8900/ws')
    insecure = os.environ.get('VOICE_SMOKE_INSECURE') == '1'
    ssl_context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    headers = {'Authorization': 'Bearer ' + os.environ['VOICE_TOKEN']} if os.environ.get('VOICE_TOKEN') else {}
    health_url = url.replace('wss://', 'https://').replace('ws://', 'http://').rsplit('/ws', 1)[0] + '/health'
    async with httpx.AsyncClient(verify=not insecure, trust_env=False) as http:
        health = await http.get(health_url)
        health.raise_for_status()
        assert health.json()['ok'] is True
    report = {'health': health.json(), 'samples': [], 'conversations': []}
    for thinking in (True, False):
        for sample in range(samples):
            cid = str(uuid.uuid4())
            report['conversations'].append(cid)
            kw = {'ssl': ssl_context} if url.startswith('wss:') else {}
            async with websockets.connect(url, additional_headers=headers, **kw) as ws:
                await ws.send(json.dumps({'type': 'hello', 'protocol': 2, 'conversation': cid, 'thinking': thinking}))
                session = json.loads(await ws.recv())
                assert session['type'] == 'session' and session['thinking'] is thinking
                started = time.monotonic()
                await ws.send(json.dumps({'type': 'text', 'text': 'What is two plus two? Reply in one short sentence.', 'speak': False}))
                first_ms = done_ms = None
                decision_events = []
                reply = ''
                turn = None
                async with asyncio.timeout(70):
                    while done_ms is None or (thinking and not decision_events):
                        raw = await ws.recv()
                        assert isinstance(raw, str)
                        event = json.loads(raw)
                        assert event['type'] != 'error', 'Voice turn failed'
                        if event['type'] == 'assistant_delta':
                            if first_ms is None:
                                first_ms = (time.monotonic() - started) * 1000
                            reply += event['text']
                            turn = event['turn']
                        elif event['type'] == 'assistant_done':
                            done_ms = (time.monotonic() - started) * 1000
                        elif event['type'] == 'decision':
                            decision_events.append(event)
                # Also check late/duplicate events after the normal reply completes.
                try:
                    async with asyncio.timeout(.35):
                        while True:
                            event = json.loads(await ws.recv())
                            if event['type'] == 'decision':
                                decision_events.append(event)
                except TimeoutError:
                    pass
                assert reply.strip() and ('four' in reply.lower() or '4' in reply), 'Arithmetic smoke reply missing'
                assert len(decision_events) == (1 if thinking else 0)
                if thinking:
                    event = decision_events[0]
                    assert event['status'] == 'ok' and event['mode'] == 'shadow' and event['source'] == 'text'
                    assert event['turn'] == turn and event['engine']['adapter']
                    assert {a['id'] for a in event['answers']} == {'intent', 'needs_confirmation', 'context_source', 'urgency', 'is_correction'}
                result = {'thinking': thinking, 'sample': sample, 'first_ms': first_ms, 'done_ms': done_ms,
                          'decision_count': len(decision_events),
                          'decision_ms': decision_events[0]['latency_ms'] if decision_events else None}
                report['samples'].append(result)
                print(json.dumps(result), flush=True)
    engine = DecisionClient()
    try:
        await engine.refresh_info()
        voice = await engine.decide({'text': 'turn off the living room lights', 'source': 'voice',
                                     'assistant_spoke_recently': False, 'recent_speech_window_seconds': 15,
                                     'contains_wake_word_or_assistant_name': False, 'previous_assistant_reply': '',
                                     'interrupted_playback': False}, 'voice', 1)
        assert voice['status'] == 'ok', voice['status']
        by_id = {a['id']: a for a in voice['answers']}
        assert by_id['needs_response']['p'] > .5
        assert by_id['intent']['choice'] == 'device_control'
        report['engine_smoke'] = voice
        print(json.dumps({'voice_smoke': 'passed', 'latency_ms': voice['latency_ms'],
                          'needs_response': by_id['needs_response']['p'], 'intent': by_id['intent']['choice']}), flush=True)
    finally:
        await engine.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=4)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = asyncio.run(check(args.samples))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
