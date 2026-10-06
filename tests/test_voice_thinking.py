import asyncio
import json
import time
import pytest
from services.voice.thinking import ThinkingAgent, UncertainToolOutcome
from services.voice.identity import Identity, current_identity
from services.voice.jobs import Jobs
from services.voice.state import Store


def call(name, args):
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class Devices:
    rows = [{'name': 'gpu-box', 'caps': ['info.uptime', 'file.write', 'sms.list', 'shell.exec', 'hermes.run']}]
    async def refresh(self): return self.rows
    async def validate(self, name):
        if name != 'gpu-box': raise ValueError('Unknown worker')


class MCP:
    def __init__(self): self.calls = []; self.fail_write = False
    async def call(self, name, args):
        self.calls.append((name, args))
        if args.get('cap') == 'caps.describe':
            return json.dumps({'ok': True, 'result': {
                'info.uptime': {'params': []}, 'sms.list': {'params': []},
                'file.write': {'params': [{'name': 'path', 'type': 'str', 'required': True}, {'name': 'content', 'type': 'str', 'required': True}]},
                'shell.exec': {'params': [{'name': 'cmd', 'type': 'str', 'required': True}]}}})
        if args.get('cap') == 'file.write' and self.fail_write:
            raise ConnectionError('disconnected after worker accepted the request')
        return json.dumps({'ok': True, 'id': 'journal-id', 'result': {'ok': True, 'uptime': 42}})
    async def list_tools(self):
        return [{'name': 'rook_knowledge', 'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string'}}, 'required': ['action']}}]


class Script:
    def __init__(self, messages): self.responses = iter(messages); self.requests = []
    async def __call__(self, messages, tools, effort):
        self.requests.append((list(messages), effort))
        return next(self.responses)


def test_same_agent_reads_then_writes_with_thinking_and_schema_validation():
    async def scenario():
        mcp = MCP()
        script = Script([call('rook_call', {'worker': 'gpu-box', 'cap': 'info.uptime'}),
            call('rook_call', {'worker': 'gpu-box', 'cap': 'file.write', 'args': {'path': '/tmp/test', 'content': '42'}}),
            call('finish', {'text': 'I read the uptime and saved it.'})])
        agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
        token = current_identity.set(Identity('Alex', owner=True))
        try:
            answer = await agent.run('Save the uptime', [], lambda e: None)
        finally: current_identity.reset(token)
        assert 'saved' in answer
        assert [r[1] for r in script.requests] == ['high'] * 3
        assert [a['cap'] for _, a in mcp.calls] == ['caps.describe', 'info.uptime', 'file.write']
        assert script.requests[-1][0][-1]['role'] == 'tool'
    asyncio.run(scenario())


def test_uncertain_mutation_stops_without_a_model_or_tool_retry():
    async def scenario():
        mcp = MCP(); mcp.fail_write = True
        script = Script([call('rook_call', {'worker': 'gpu-box', 'cap': 'file.write', 'args': {'path': '/tmp/test', 'content': '42'}})])
        agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
        token = current_identity.set(Identity('Alex', owner=True))
        try:
            with pytest.raises(UncertainToolOutcome): await agent.run('Save the file', [], lambda e: None)
        finally: current_identity.reset(token)
        assert len(script.requests) == 1
        assert sum(a.get('cap') == 'file.write' for _, a in mcp.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('cap', ['sms.list', 'shell.exec'])
def test_identity_denial_is_terminal_before_any_mcp(cap):
    async def scenario():
        mcp = MCP(); script = Script([call('rook_call', {'worker': 'gpu-box', 'cap': cap, 'args': {'cmd': 'true'} if cap == 'shell.exec' else {}})])
        agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
        with pytest.raises(PermissionError): await agent.run('Do the lookup', [], lambda e: None)
        assert not mcp.calls and len(script.requests) == 1
    asyncio.run(scenario())


def test_invalid_arguments_corrected_before_dispatch_and_duplicate_write_blocked():
    async def scenario():
        args = {'worker': 'gpu-box', 'cap': 'file.write', 'args': {'path': '/tmp/test', 'content': '42'}}
        script = Script([call('rook_call', {**args, 'args': {'path': '/tmp/test', 'contents': '42'}}),
                         call('rook_call', args), call('rook_call', args), call('finish', {'text': 'Saved once.'})])
        mcp = MCP(); agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
        token = current_identity.set(Identity('Alex', owner=True))
        try: assert await agent.run('Save the file once', [], lambda e: None) == 'Saved once.'
        finally: current_identity.reset(token)
        assert sum(a.get('cap') == 'file.write' for _, a in mcp.calls) == 1
        assert 'already dispatched' in script.requests[-1][0][-1]['content']
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['escalate', 'delegate_to_hermes', 'rook_read'])
def test_all_production_tool_jobs_use_own_agent_and_preserve_unknown_outcomes(name):
    async def scenario():
        class Agent:
            async def run(self, task, context, on_event, initial=None):
                on_event({'trace': [{'operation': 'write', 'status': 'dispatched'}]})
                raise UncertainToolOutcome('check journal-id')
        async def direct(args): pytest.fail('Bypassed thinking mode')
        store = Store(':memory:')
        jobs = Jobs(store, {'rook_read': direct}, 'unreachable-hermes', 1, lambda *a: None, agent=Agent())
        jid = jobs.start('s', name, {'task': 'work'}, [])
        await jobs.tasks[jid]
        result = store.job(jid, 's')
        assert result['status'] == 'unknown' and 'journal-id' in result['result']
        assert 'dispatched' in result['result']
        await jobs.close()
    asyncio.run(scenario())


def test_escalated_image_is_preserved_as_native_image_not_truncated_json():
    async def scenario():
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'a' * 40000}}
        script = Script([call('finish', {'text': 'Image inspected.'})])
        agent = ThinkingAgent(complete=script, mcp=MCP(), devices=Devices())
        context = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Inspect this'}, image]}]
        assert await agent.run('Inspect the image', context, lambda e: None) == 'Image inspected.'
        messages = script.requests[0][0]
        assert messages[-1]['content'][-1] == image
        assert 'a' * 100 not in messages[1]['content']
    asyncio.run(scenario())


def test_mouthpiece_forces_thinking_off_and_tool_adapter_turns_it_on(monkeypatch):
    from services.voice import providers
    body = providers.mouthpiece_body({}, {'reasoning_effort': 'high'}, False)
    assert body['reasoning_effort'] == 'none' and body['chat_template_kwargs']['enable_thinking'] is False
    requests = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'message': call('finish', {'text': 'done'})}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json): requests.append(json); return Response()
    monkeypatch.setattr(providers.httpx, 'AsyncClient', Client)
    asyncio.run(providers.thinking_chat([], [], 'high'))
    assert requests[0]['reasoning_effort'] == 'high'
    assert 'enable_thinking' not in requests[0]['chat_template_kwargs']


def test_parallel_plan_is_rejected_before_dispatch_then_corrected():
    async def scenario():
        first = call('rook_devices', {})
        first['tool_calls'] += call('rook_call', {'worker': 'gpu-box', 'cap': 'info.uptime'})['tool_calls']
        mcp = MCP()
        script = Script([first, call('rook_call', {'worker': 'gpu-box', 'cap': 'info.uptime'}), call('finish', {'text': 'Uptime is 42.'})])
        agent = ThinkingAgent(complete=script, mcp=mcp, devices=Devices())
        current_identity.set(Identity('Alex', owner=True))
        assert await agent.run('Read uptime', [], lambda e: None) == 'Uptime is 42.'
        assert [a['cap'] for _, a in mcp.calls] == ['caps.describe', 'info.uptime']
        assert all('No tools' in m['content'] for m in script.requests[1][0][-2:])
    asyncio.run(scenario())
