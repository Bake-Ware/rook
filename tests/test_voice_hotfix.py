import asyncio
import copy
import json
import time

import httpx
import pytest

from services.voice import providers
from services.voice.workers import WorkerInventory
from services.voice.jobs import Jobs
from services.voice.state import Store


def call(name='respond', arguments=None):
    return {'tool_calls': [{'id': 'plan', 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments if arguments is not None else {'text': 'Hello.'})}}]}


@pytest.mark.parametrize('bad', [
    {'content': "Soundwave is online. I checked it."}, {},
    {'tool_calls': [call()['tool_calls'][0]] * 2},
    call(arguments={'text': ''}), call(arguments={'text': 5}),
    call('unknown'), call('rook_read', {'worker': 'kaiju'}),
    {'tool_calls': [{'function': {'name': 'respond', 'arguments': '{bad'}}]},
])
def test_invalid_plans_retry_once_then_speak_only_safe_fallback(monkeypatch, bad):
    sent, spoken, logged = [], [], []
    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': bad}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: client_type(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(providers, 'VLLM_URL', 'http://model/chat')
    monkeypatch.setattr(providers, 'log_rejected_plan', lambda raw, attempt: logged.append((raw, attempt)))
    original = [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'Checking on Soundwave?'}]
    saved = copy.deepcopy(original)
    async def scenario():
        async def clause(text): spoken.append(text)
        result, calls = await providers.Provider.chat(None, original, clause)
        assert result == providers.SAFE_FALLBACK and calls == []
    asyncio.run(scenario())
    assert spoken == [providers.SAFE_FALLBACK]
    assert len(sent) == 2 and len(logged) == 2
    assert sent[1]['messages'][-2]['role'] == 'assistant'
    assert json.loads(sent[1]['messages'][-2]['content'])['choices'][0]['message'] == bad
    assert 'REJECTED' in sent[1]['messages'][-1]['content']
    assert original == saved


def test_reply_only_cannot_retry_a_job(monkeypatch):
    responses = [call('rook_read', {'worker': 'kaiju', 'cap': 'info.uptime'}), call(arguments={'text': 'The lookup failed.'})]
    spoken = []
    async def handler(request):
        payload = json.loads(request.content)
        assert [v['properties']['name']['const'] for v in payload['response_format']['json_schema']['schema']['anyOf']] == ['respond']
        return httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: client_type(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(providers, 'VLLM_URL', 'http://model/chat')
    monkeypatch.setattr(providers, 'log_rejected_plan', lambda *args: None)
    async def scenario():
        async def clause(text): spoken.append(text)
        text, calls = await providers.Provider.chat(None, [{'role': 'system', 'content': 'policy'}], clause, reply_only=True)
        assert text == 'The lookup failed.' and not calls
    asyncio.run(scenario())
    assert spoken == ['The lookup failed.']


def test_worker_cache_refresh_validation_and_no_handoff_to_invented_host(monkeypatch):
    from services.voice import workers
    requests = []
    roster = [{'name': 'kaiju'}, {'name': 'cachyrig'}]
    class MCP:
        async def call(self, tool, args):
            requests.append((tool, args))
            return json.dumps(roster)
    monkeypatch.setattr(workers, 'RookMCP', MCP)
    cache = WorkerInventory(ttl=60)
    monkeypatch.setattr(providers, 'inventory', cache)
    async def scenario():
        assert 'kaiju' in await providers.tool_rook_devices({})
        assert await cache.validate('kaiju') == 'kaiju'
        with pytest.raises(providers.Handoff, match="no Rook worker named 'soundwave'; available: cachyrig, kaiju"):
            await providers.tool_rook_read({'worker': 'soundwave', 'cap': 'battery.status'})
        assert len(requests) == 1  # fails before rook_call
        cache.updated = time.monotonic() - 61
        roster.append({'name': 'new-phone'})
        await cache.validate('new-phone')
        assert len(requests) == 2
    asyncio.run(scenario())


def test_failed_job_preserves_readable_message():
    async def scenario():
        store = Store(':memory:')
        async def fail(args):
            raise providers.Handoff("no Rook worker named 'soundwave'; available: kaiju")
        jobs = Jobs(store, {'rook_read': fail}, '', 0, lambda *args: None)
        jid = jobs.start('s', 'rook_read', {}, [])
        await jobs.tasks[jid]
        result = store.job(jid, 's')
        assert result['status'] == 'failed'
        assert "no Rook worker named 'soundwave'; available: kaiju" in result['result']
        assert 'Handoff' not in result['result']
        await jobs.close()
        store.db.close()
    asyncio.run(scenario())


def test_planner_reuses_client_across_turns_and_closes_it():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': call()}]})
    async def scenario():
        provider = providers.Provider.__new__(providers.Provider)
        provider.chat_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def clause(text): assert text == 'Hello.'
        for _ in range(2):
            await provider.chat([{'role': 'system', 'content': 'policy'}], clause)
            assert not provider.chat_http.is_closed
        await provider.close()
        assert provider.chat_http.is_closed
        assert len(requests) == 2
    asyncio.run(scenario())


def test_native_gemma_parse_error_uses_bounded_retry(monkeypatch):
    sent, spoken = [], []
    async def handler(request):
        sent.append(request)
        return httpx.Response(500, json={'error': {'message': 'The model produced output that does not match the expected peg-gemma4 format'}})
    monkeypatch.setattr(providers, 'log_rejected_plan', lambda *a: None)
    async def scenario():
        provider = providers.Provider.__new__(providers.Provider)
        provider.chat_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def clause(text): spoken.append(text)
        try:
            text, calls = await provider.chat([{'role': 'system', 'content': 'test'}], clause)
            assert text == providers.SAFE_FALLBACK and not calls
            assert len(sent) == 2 and spoken == [providers.SAFE_FALLBACK]
        finally: await provider.close()
    asyncio.run(scenario())


def test_constrained_json_plan_reuses_runtime_validation(monkeypatch):
    replies = [json.dumps({'name': 'rook_read', 'arguments': {'worker': 'kaiju'}}),
               json.dumps({'name': 'respond', 'arguments': {'text': 'Hello.'}})]
    spoken = []
    async def handler(request):
        payload = json.loads(request.content)
        assert 'tools' not in payload and 'tool_choice' not in payload
        assert payload['response_format']['json_schema']['strict'] is True
        return httpx.Response(200, json={'choices': [{'message': {'content': replies.pop(0)}}]})
    monkeypatch.setattr(providers, 'log_rejected_plan', lambda *a: None)
    async def scenario():
        provider = providers.Provider.__new__(providers.Provider)
        provider.chat_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def clause(text): spoken.append(text)
        try:
            text, calls = await provider.chat([{'role':'system','content':'test'}], clause)
            assert text == 'Hello.' and spoken == ['Hello.'] and not calls and not replies
        finally: await provider.close()
    asyncio.run(scenario())
