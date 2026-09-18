"""Bounded, advisory-only decision-engine client and protocol-2 translation."""
import asyncio
import math
import os
import time

import httpx


INTENTS = ['device_control', 'question', 'chit_chat', 'task_request', 'not_addressed']
SOURCES = ['none', 'hermes_memory', 'obsidian', 'device_state']
LEVELS = ['background or casual conversation', 'routine request',
          'explicit time pressure', 'immediate danger or emergency']


def questions(source):
    result = [
        {'id': 'needs_response', 'type': 'noul',
         'instructions': 'Does this utterance need a response from the assistant?'},
        {'id': 'intent', 'type': 'choice', 'instructions': "Classify the user's intent.", 'options': INTENTS},
        {'id': 'needs_confirmation', 'type': 'noul',
         'instructions': 'Must the assistant request confirmation before acting?'},
        {'id': 'context_source', 'type': 'choice',
         'instructions': 'Which context source is needed to handle the request?', 'options': SOURCES},
        {'id': 'urgency', 'type': 'score', 'instructions': 'How urgent is this utterance?', 'levels': LEVELS},
        {'id': 'is_correction', 'type': 'noul', 'instructions':
         "Is the user correcting or objecting to the assistant's previous response/action, e.g. 'not you', 'I wasn't talking to you', 'cancel that'?"},
    ]
    return [q for q in result if source == 'voice' or q['id'] != 'needs_response']


def number(value, low=0, high=1):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError('Invalid number')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Number out of range')
    return float(value)


def normalize(payload, source):
    raw = payload['answers']
    if not isinstance(raw, list):
        raise ValueError('Invalid answers')
    by_id = {a['id']: a for a in raw}
    expected = questions(source)
    if len(by_id) != len(raw) or set(by_id) != {q['id'] for q in expected}:
        raise ValueError('Missing or duplicate answers')
    answers = []
    for q in expected:
        a = by_id[q['id']]
        if a['type'] != q['type']:
            raise ValueError('Invalid answer type')
        out = {'id': q['id'], 'type': q['type']}
        if q['type'] == 'noul':
            out.update(p=number(a['noul']), confidence=number(a['confidence']))
        else:
            keys = q.get('options', ['0', '1', '2', '3'])
            probs = a['probabilities']
            if not isinstance(probs, dict) or set(probs) != set(keys):
                raise ValueError('Invalid distribution')
            out['probabilities'] = {k: number(probs[k]) for k in keys}
            if abs(sum(out['probabilities'].values()) - 1) > .01:
                raise ValueError('Unnormalized distribution')
            if q['type'] == 'choice':
                if a['choice'] not in keys:
                    raise ValueError('Invalid choice')
                out.update(choice=a['choice'], confidence=number(a['confidence']))
            else:
                if str(a['level']) not in keys:
                    raise ValueError('Invalid level')
                out.update(level=int(a['level']), expected=number(a['score'], 0, 3))
        answers.append(out)
    return answers


class DecisionClient:
    def __init__(self, url=None, timeout_ms=None, transport=None):
        self.url = (os.environ.get('DECISION_URL', '') if url is None else url).rstrip('/')
        self.timeout = float(timeout_ms if timeout_ms is not None else os.environ.get('DECISION_TIMEOUT_MS', '150')) / 1000
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('DECISION_TIMEOUT_MS must be positive')
        self.info = {}
        self.http = httpx.AsyncClient(timeout=self.timeout, transport=transport, trust_env=False,
                                      limits=httpx.Limits(max_connections=8, max_keepalive_connections=4)) if self.url else None

    async def refresh_info(self):
        if not self.http:
            return
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.http.get(self.url + '/info')
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    return
                self.info = {k: data.get(k) for k in ('model', 'lora_path', 'calibration')
                             if isinstance(data.get(k), str)}
                details = data.get('calibration_details') or {}
                self.info['adapter_sha256'] = details.get('adapter_sha256')
        except Exception:
            pass

    def event(self, source, turn, status='disabled'):
        return {'type': 'decision', 'turn': turn, 'source': source, 'mode': 'shadow',
                'status': 'disabled' if status == 'skipped' else status, 'engine_status': status,
                'latency_ms': None, 'elapsed_ms': 0,
                'engine': {'model': self.info.get('model'), 'adapter': self.info.get('lora_path'),
                           'calibration': self.info.get('calibration')},
                'model': self.info.get('model'), 'adapter': self.info.get('lora_path'), 'error': None}

    async def decide(self, state, source, turn):
        event = self.event(source, turn)
        if not self.http:
            return event
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.http.post(self.url + '/decide', json={'state': state, 'questions': questions(source)})
                response.raise_for_status()
                payload = response.json()
                answers = normalize(payload, source)
                # Response metadata takes precedence over the cached /info snapshot.
                for key in ('model', 'calibration'):
                    value = payload.get(key)
                    if value is not None and not isinstance(value, str):
                        raise ValueError('Invalid engine metadata')
                    event['engine'][key] = value
                event.update(status='ok', engine_status='ok', answers=answers)
        except (TimeoutError, httpx.TimeoutException):
            event.update(status='timeout', engine_status='timeout', error='Decision deadline exceeded')
        except Exception:
            # Do not leak URLs, response bodies, or user text through error strings.
            event.update(status='error', engine_status='error', error='Decision engine unavailable or invalid response')
        event['latency_ms'] = (time.monotonic() - started) * 1000
        event['elapsed_ms'] = event['latency_ms']
        event['model'], event['adapter'] = event['engine'].get('model'), event['engine'].get('adapter')
        if event['error']:
            event['detail'] = event['error']
        return event

    async def close(self):
        if self.http:
            await self.http.aclose()
