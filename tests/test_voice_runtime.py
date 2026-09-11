import asyncio
import json
import uuid
import pytest
from services.voice.acp import ACPClient
from services.voice.jobs import Jobs
from services.voice.runtime import Connection
from services.voice.state import Store


def run(fn):
    return asyncio.run(fn())


def test_history_survives_reopen_and_is_scoped(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    cid = str(uuid.uuid4())
    a, b = store.key('alice', cid), store.key('bob', cid)
    store.append(a, 'user', {'text': 'Remember the blue machine'})
    store.append(a, 'tool', {'id': 'tool1', 'name': 'lookup', 'result': '42'})
    store.db.close()
    store = Store(path)
    assert store.messages(b) == []
    messages = store.messages(a)
    assert messages[0]['content'] == 'Remember the blue machine'
    assert messages[1]['tool_calls'][0]['id'] == messages[2]['tool_call_id']
    assert path.stat().st_mode & 0o777 == 0o600


def test_context_truncation_keeps_tool_pairs():
    store = Store(':memory:')
    for i in range(100):
        store.append('s','tool', {'id':str(i),'name':'lookup','result':'x'*1000})
    messages = store.messages('s')
    assert len(messages) < 160
    for parent, child in zip(messages[::2],messages[1::2]):
        assert parent['tool_calls'][0]['id'] == child['tool_call_id']


def test_restart_marks_running_jobs_unknown(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    jid = store.create_job('s','delegate_to_hermes',{})
    store.db.close()
    store = Store(path)
    assert store.job(jid,'s')['status'] == 'unknown'
    assert store.job(jid,'other') is None


def test_acp_eof_fails_prompt_without_waiting_for_timeout():
    async def scenario():
        async def handler(reader, writer):
            while line := await reader.readline():
                msg = json.loads(line)
                if msg['method'] == 'session/prompt':
                    writer.close(); return
                result = {'sessionId':'x'} if msg['method']=='session/new' else {}
                writer.write((json.dumps({'id':msg['id'],'result':result})+'\n').encode())
                await writer.drain()
        server = await asyncio.start_server(handler,'127.0.0.1',0)
        client = ACPClient('127.0.0.1',server.sockets[0].getsockname()[1],lambda e:None)
        try:
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(client.run('test'),1)
            assert not client.pending
        finally:
            await client.close(); server.close(); await server.wait_closed()
    run(scenario)


def test_acp_rpc_error_is_not_success():
    async def scenario():
        async def handler(reader,writer):
            msg=json.loads(await reader.readline())
            writer.write((json.dumps({'id':msg['id'],'error':{'code':-1}})+'\n').encode())
            await writer.drain(); writer.close()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        client=ACPClient('127.0.0.1',server.sockets[0].getsockname()[1],lambda e:None)
        try:
            with pytest.raises(RuntimeError): await client.run('test')
        finally:
            await client.close(); server.close(); await server.wait_closed()
    run(scenario)


class Provider:
    default_voice='test'
    system='test'
    async def transcribe(self, pcm): return 'hello'
    async def chat(self, messages, on_clause, reply_only=False):
        await on_clause('Hello.')
        return 'Hello.',[]
    async def synthesize(self,text,voice): return b'\0'*3200,16000


def test_interrupt_discards_old_audio_without_cancelling_job():
    async def scenario():
        store=Store(':memory:'); gate=asyncio.Event(); packets=[]; events=[]
        async def tool(args): await gate.wait(); return 'the answer is 42'
        jobs=Jobs(store,{'lookup':tool},'',0,lambda s,e:events.append(e))
        async def send(e): events.append(e)
        async def audio(b): packets.append(b)
        conn=Connection(store,jobs,Provider(),'s',send,audio)
        jid=jobs.start('s','lookup',{},[])
        await conn.start(text='hello')
        await asyncio.sleep(.02)
        await conn.interrupt()
        count=len(packets)
        await asyncio.sleep(.05)
        assert len(packets)==count
        assert store.job(jid,'s')['status']=='running'
        gate.set()
        await asyncio.gather(*list(jobs.tasks.values()))
        assert store.job(jid,'s')['status']=='completed'
        assert any('42' in m.get('content','') for m in store.messages('s') if m.get('content'))
        assert all(p.startswith(b'RK2A') for p in packets)
        await conn.close(); await jobs.close()
    run(scenario)


def test_failed_and_timed_out_tools_are_terminal_not_success():
    async def scenario():
        store=Store(':memory:')
        async def fail(args): raise ValueError('private tool response')
        async def hang(args): await asyncio.Event().wait()
        jobs=Jobs(store,{'fail':fail,'hang':hang},'',0,lambda s,e:None,read_timeout=.02)
        ids=[jobs.start('s',name,{},[]) for name in ['fail','hang']]
        await asyncio.gather(*list(jobs.tasks.values()))
        assert all(store.job(jid,'s')['status']=='failed' for jid in ids)
        assert all('private tool response' not in store.job(jid,'s')['result'] for jid in ids)
        await jobs.close()
    run(scenario)


def test_model_failure_does_not_leave_tts_consumer_waiting():
    async def scenario():
        class Broken(Provider):
            async def chat(self,messages,on_clause,reply_only=False): raise ValueError('bad model')
        events=[]
        async def send(e): events.append(e)
        async def audio(b): pass
        store=Store(':memory:'); jobs=Jobs(store,{},'',0,lambda s,e:None)
        conn=Connection(store,jobs,Broken(),'s',send,audio)
        await conn.start(text='hi')
        await asyncio.wait_for(conn.task,.5)
        assert events[-2]['state']=='listening'
        assert any(e['type']=='error' for e in events)
        await conn.close()
    run(scenario)


def test_cancel_job_cannot_cross_sessions():
    store=Store(':memory:')
    jid=store.create_job('alice','lookup',{})
    jobs=Jobs(store,{},'',0,lambda s,e:None)
    with pytest.raises(ValueError): jobs.cancel('bob',jid)


def test_planner_retries_missing_call_before_speaking_or_starting_work():
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    source=ast.parse(Path('services/voice/providers.py').read_text())
    provider=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='Provider')
    chat=next(n for n in provider.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='chat')
    requests=[];spoken=[]
    class Response:
        def __init__(self,index):self.index=index
        def raise_for_status(self):pass
        def json(self):
            message={'content':'Let me check that.'} if self.index==1 else {'tool_calls':[{'function':{'name':'rook_read','arguments':'{"worker":"kaiju","cap":"info.uptime"}'}}]}
            return {'choices':[{'message':message}]}
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,json):
            requests.append(json)
            return Response(len(requests))
    namespace={'httpx':SimpleNamespace(AsyncClient=Client),'VLLM_URL':'local','VLLM_MODEL':'model','TOOLS':[], 'json':json,'split_sentences':lambda t:([],t)}
    exec(compile(ast.Module(body=[chat],type_ignores=[]),'planner-test','exec'),namespace)
    async def scenario():
        async def on_clause(text):spoken.append(text)
        text,calls=await namespace['chat'](None,[{'role':'system','content':'policy'},{'role':'user','content':'Check uptime'}],on_clause)
        assert text=='' and calls[0]['function']['name']=='rook_read'
        assert not spoken and len(requests)==2
        assert all(m['role']!='system' for m in requests[0]['messages'][1:])
        assert requests[0]['tool_choice']=='required'
    run(scenario)
