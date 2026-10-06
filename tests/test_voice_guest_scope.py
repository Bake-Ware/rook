"""Non-owner keys see only their own device, lookup jobs never get write tools,
and guests get short, few jobs (PR #48 review)."""
import asyncio
import json
import time

import httpx
import pytest

from services.voice import providers
from services.voice.identity import Identity, PolicyRefusal, current_identity
from services.voice.jobs import GUEST_JOB_TIMEOUT, Jobs
from services.voice.state import Store
from services.voice.thinking import READONLY_TOOLS, ThinkingAgent
from services.voice.workers import WorkerInventory


def call(name, args):
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


ROWS = [{'name': 'phone', 'caps': ['sms.list', 'battery.status']},
        {'name': 'nas', 'caps': ['file.read', 'info.uptime']},
        {'name': 'gpu-box', 'caps': ['info.uptime', 'shell.exec', 'file.write']}]


def live_inventory():
    inventory = WorkerInventory()
    inventory.rows, inventory.updated = [dict(r) for r in ROWS], time.monotonic()
    inventory.schemas = {'nas': {'file.read': {'params': [{'name': 'path', 'required': True}]}}}
    return inventory


class Devices:
    def __init__(self): self.inventory = live_inventory(); self.rows = self.inventory.rows
    async def refresh(self): return self.rows
    async def validate(self, name): return await self.inventory.validate(name)


class MCP:
    def __init__(self): self.calls = []
    async def call(self, name, args):
        self.calls.append((name, args))
        if args.get('cap') == 'caps.describe':
            return json.dumps({'ok': True, 'result': {
                'info.uptime': {'params': []}, 'sms.list': {'params': []}, 'battery.status': {'params': []},
                'file.write': {'params': [{'name': 'path', 'required': True}, {'name': 'content', 'required': True}]},
                'shell.exec': {'params': [{'name': 'cmd', 'required': True}]}}})
        return json.dumps({'ok': True, 'id': 'journal-id', 'result': {'ok': True}})
    async def list_tools(self):
        return [{'name': 'rook_knowledge', 'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string'}}}}]


class Script:
    def __init__(self, messages): self.responses = iter(messages); self.offered = []; self.requests = []
    async def __call__(self, messages, tools, effort):
        self.offered.append({t['function']['name'] for t in tools})
        self.requests.append(list(messages))
        return next(self.responses)


def run_as(identity, coro):
    async def scenario():
        current_identity.set(identity)
        return await coro()
    return asyncio.run(scenario())


# --- inventory filtering -----------------------------------------------------

def test_read_catalog_filters_to_given_workers():
    inventory = live_inventory()
    caps, text = inventory.read_catalog(providers.READ_CAPS, {'phone'})
    assert set(caps) == {'sms.list', 'battery.status'}
    assert 'nas' not in text and 'gpu-box' not in text and 'phone' in text
    caps, text = inventory.read_catalog(providers.READ_CAPS)
    assert 'nas' in text and 'gpu-box' in text


@pytest.mark.parametrize('owner', [False, True])
def test_unknown_worker_error_lists_others_only_to_owner(owner):
    inventory = live_inventory()
    inventory.ttl = 3600
    async def check():
        with pytest.raises(ValueError) as error:
            await inventory.validate('mystery')
        return str(error.value)
    message = run_as(Identity('Alex', 'phone', owner=owner), check)
    assert ('available' in message and 'nas' in message) is owner
    assert owner or ('nas' not in message and 'gpu-box' not in message)


def planner_request(monkeypatch, identity):
    sent = []
    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': call('respond', {'text': 'Hello.'})}]})
    monkeypatch.setattr(providers, 'inventory', live_inventory())
    monkeypatch.setattr(providers, 'PRIMARY_URL', '')
    async def scenario():
        current_identity.set(identity)
        provider = providers.Provider.__new__(providers.Provider)
        provider.chat_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def clause(text): pass
        await provider.chat([{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'hi'}], clause)
        await provider.close()
    asyncio.run(scenario())
    body = sent[0]
    variant = next(v for v in body['response_format']['json_schema']['schema']['anyOf']
                   if v['properties']['name']['const'] == 'rook_read')
    return body, variant['properties']['arguments']['properties']['worker'].get('enum')


def test_mapped_key_planner_sees_only_its_own_device(monkeypatch):
    body, enum = planner_request(monkeypatch, Identity('Sam', 'phone'))
    assert enum == ['phone']
    text = json.dumps(body)
    assert 'nas' not in text and 'gpu-box' not in text


def test_owner_planner_sees_every_device(monkeypatch):
    body, enum = planner_request(monkeypatch, Identity('Alex', 'phone', owner=True))
    assert enum == ['gpu-box', 'nas', 'phone']


# --- thinking agent as guest / mapped key ------------------------------------

@pytest.mark.parametrize('identity', [Identity(), Identity('Sam', 'phone')], ids=['guest', 'mapped'])
@pytest.mark.parametrize('tool,args', [('rook_devices', {}), ('rook_mcp_describe', {}),
                                       ('rook_mcp', {'tool': 'rook_knowledge', 'args': {'action': 'search'}})])
def test_thinking_agent_refuses_band_tools_to_non_owner(identity, tool, args):
    mcp = MCP()
    script = Script([call(tool, args)])
    agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
    async def scenario():
        with pytest.raises(PolicyRefusal):
            await agent.run('Look around the band', [], lambda e: None)
    run_as(identity, scenario)
    assert not mcp.calls


# --- lookup jobs are read-only -----------------------------------------------

def jobs_with(agent, store, **kwargs):
    async def web_search(args): return 'results'
    return Jobs(store, {'web_search': web_search}, '', 0, lambda *a: None, agent=agent, **kwargs)


@pytest.mark.parametrize('identity', [Identity('Alex', owner=True), Identity('Sam', 'phone')], ids=['owner', 'mapped'])
def test_web_search_job_cannot_call_rook_call_or_escalate_its_tools(identity):
    mcp = MCP()
    # Injected web text "tells" the agent to write a file; the job's loop has no such tool.
    script = Script([call('rook_call', {'worker': 'gpu-box', 'cap': 'file.write', 'args': {'path': '/x', 'content': 'pwned'}}),
                     call('rook_mcp', {'tool': 'rook_knowledge', 'args': {'action': 'create'}}),
                     call('finish', {'text': 'Here is what I found.'})])
    agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
    async def scenario():
        store = Store(':memory:')
        jobs = jobs_with(agent, store)
        jid = jobs.start('s', 'web_search', {'query': 'news'}, [])
        await jobs.tasks[jid]
        return store.job(jid, 's')
    job = run_as(identity, scenario)
    assert job['status'] == 'completed' and job['result'] == 'Here is what I found.'
    assert all(offered == READONLY_TOOLS for offered in script.offered)
    assert 'rook_call' not in script.offered[0] and 'rook_mcp' not in script.offered[0]
    assert 'unavailable tool: rook_call' in script.requests[1][-1]['content']
    assert not mcp.calls


def test_owner_escalation_keeps_the_full_toolset():
    script = Script([call('rook_call', {'worker': 'gpu-box', 'cap': 'file.write', 'args': {'path': '/x', 'content': '1'}}),
                     call('finish', {'text': 'Saved.'})])
    mcp = MCP()
    agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
    async def scenario():
        store = Store(':memory:')
        jobs = jobs_with(agent, store)
        jid = jobs.start('s', 'escalate', {'task': 'save 1 to /x'}, [])
        await jobs.tasks[jid]
        return store.job(jid, 's')
    job = run_as(Identity('Alex', owner=True), scenario)
    assert job['status'] == 'completed'
    assert {'rook_call', 'rook_mcp'} <= script.offered[0]
    assert any(a.get('cap') == 'file.write' for _, a in mcp.calls)


# --- guest limits ------------------------------------------------------------

def test_guest_jobs_are_short_and_few():
    async def scenario():
        gate = asyncio.Event()
        async def wait(args): await gate.wait(); return 'done'
        store = Store(':memory:')
        jobs = Jobs(store, {'web_search': wait}, '', 0, lambda *a: None)
        assert jobs.timeout('escalate', owner=True) == 600
        assert jobs.timeout('escalate', owner=False) == GUEST_JOB_TIMEOUT
        assert jobs.timeout('web_search') == 45   # read path is already shorter
        jobs.start('a', 'web_search', {'query': 'x'}, [])
        jobs.start('a', 'web_search', {'query': 'x'}, [])
        with pytest.raises(RuntimeError):
            jobs.start('a', 'web_search', {'query': 'x'}, [])   # 2 per guest session
        for session in 'bcd':
            jobs.start(session, 'web_search', {'query': 'x'}, [])
            jobs.start(session, 'web_search', {'query': 'x'}, [])
        with pytest.raises(RuntimeError):
            jobs.start('e', 'web_search', {'query': 'x'}, [])   # 8 guest jobs in total
        # An owner is not limited by guest load.
        token = current_identity.set(Identity('Alex', owner=True))
        try:
            jobs.start('owner', 'web_search', {'query': 'x'}, [])
        finally:
            current_identity.reset(token)
        gate.set()
        await asyncio.gather(*list(jobs.tasks.values()))
        assert not jobs.guest_tasks
        jobs.start('e', 'web_search', {'query': 'x'}, [])      # slots free again
        await asyncio.gather(*list(jobs.tasks.values()))
    asyncio.run(scenario())


def test_guest_agent_job_uses_the_guest_timeout():
    seen = {}
    class Agent:
        async def run(self, task, context, on_event, initial=None, tools=None):
            seen['tools'] = tools
            return 'ok'
    async def scenario():
        store = Store(':memory:')
        jobs = Jobs(store, {'web_search': None}, '', 0, lambda *a: None, agent=Agent())
        real_wait_for = asyncio.wait_for
        timeouts = []
        async def wait_for(coro, timeout):
            timeouts.append(timeout); return await real_wait_for(coro, timeout)
        asyncio.wait_for = wait_for
        try:
            jid = jobs.start('s', 'web_search', {'query': 'x'}, [])
            await jobs.tasks[jid]
        finally:
            asyncio.wait_for = real_wait_for
        return timeouts
    timeouts = run_as(Identity('Sam', 'phone'), scenario)
    assert timeouts == [GUEST_JOB_TIMEOUT] and seen['tools'] == READONLY_TOOLS


def test_non_owner_failure_is_generic_but_policy_refusal_is_spoken():
    async def scenario():
        store = Store(':memory:')
        async def fail(args): raise providers.Handoff("no Rook worker named 'x'; available: nas, gpu-box")
        async def refuse(args): raise PolicyRefusal('I can only read texts from your own device.')
        jobs = Jobs(store, {'rook_read': fail, 'web_search': refuse}, '', 0, lambda *a: None)
        a = jobs.start('s', 'rook_read', {}, [])
        b = jobs.start('s', 'web_search', {}, [])
        await asyncio.gather(*list(jobs.tasks.values()))
        return store.job(a, 's')['result'], store.job(b, 's')['result']
    failed, refused = run_as(Identity('Sam', 'phone'), scenario)
    assert failed == "That didn't work."
    assert refused == 'I can only read texts from your own device.'


# --- progress is assistant-mode only -----------------------------------------

def test_progress_is_suppressed_outside_assistant_mode():
    from types import SimpleNamespace
    from services.voice.modes import Mode
    from services.voice.progress import ProgressUpdates
    conn = SimpleNamespace(closed=False, sleeping=False, receiving_speech=False, last_speech=0, play_until=0,
                           task=None, pending_results=[], mode=Mode())
    progress = ProgressUpdates(conn)
    assert not progress.suppressed()
    conn.mode = Mode(id='conversation')
    assert progress.suppressed()
