"""Shared knowledge records: attribution, revisions, links, search and the
human API. Also pins the 349e3eb regressions: knowledge never sits in the band
call path and never mints a band ID."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from starlette.applications import Starlette

from rook.knowledge.store import KnowledgeStore, Conflict
from rook.knowledge.service import KnowledgeService
from rook.knowledge.maintenance import import_vault
from rook.knowledge.web import routes

AGENT = {'id': 'agent_a', 'kind': 'agent', 'label': 'Browser'}


@pytest.fixture
def work(tmp_path):
    s = KnowledgeStore(tmp_path / 'knowledge.db')
    def create(kind, title, parent=None, **attrs):
        return s.mutate('default', AGENT, uuid.uuid4().hex, 'create',
                        {'kind': kind, 'title': title, 'body': title, 'parent': parent, 'attrs': attrs})
    concept = create('concept', 'Shared memory')
    project = create('project', 'Knowledge service', concept['id'])
    task = create('task', 'Restart test service', project['id'], criteria=['Service responds'], workers=['kaiju'])
    return SimpleNamespace(s=s, create=create, concept=concept, project=project, task=task)


def test_write_retries_and_stale_updates(work):
    w=work;data={'kind':'concept','title':'Repeat me'}
    r=w.s.mutate('default',AGENT,'retry','create',data)
    assert r==w.s.mutate('default',AGENT,'retry','create',data)
    with pytest.raises(Conflict): w.s.mutate('default',AGENT,'retry','create',data|{'title':'different'})
    w.s.mutate('default',AGENT,'edit','update',{'id':r['id'],'revision':r['revision'],'patch':{'body':'More'}})
    with pytest.raises(Conflict): w.s.mutate('default',AGENT,'edit2','update',{'id':r['id'],'revision':r['revision'],'patch':{'body':'Stale'}})
    assert KnowledgeStore(w.s.path).get('default',r['id'])['body']=='More'


def test_task_state_is_plain_bookkeeping_with_attributed_history(work):
    w=work;t=w.task
    for state in ('ready','completed'):
        t=w.s.mutate('default',AGENT,uuid.uuid4().hex,'update',{'id':t['id'],'revision':t['revision'],'patch':{'state':state}})
    record=w.s.get('default',t['id'])
    assert record['state']=='completed' and record['creator']=='agent_a'
    assert [e['action'] for e in record['events']]==['updated','updated','created']
    assert {e['actor'] for e in record['events']}=={'agent_a'}
    assert not {'approvals','attempts','operations','authorization'} & set(record)


def test_dependencies_and_cycles(work):
    w=work;b=w.create('task','B',w.project['id'],dependencies=[w.task['id']])
    with pytest.raises(Conflict):
        w.s.mutate('default',AGENT,'cycle','update',{'id':w.task['id'],'revision':1,'patch':{'attrs':{'dependencies':[b['id']]}}})


def test_supersession_and_safe_import(work,tmp_path):
    w=work;old=w.create('knowledge','Old port')
    new=w.create('knowledge','New port',supersedes=[old['id']],sources=['change:1'])
    assert w.s.get('default',old['id'])['state']=='superseded'
    assert [r['id'] for r in w.s.lexical('default','port')]==[new['id']]
    vault=tmp_path/'vault';vault.mkdir();(vault/'note.md').write_text('Existing memory')
    (vault/'escape.md').symlink_to(tmp_path/'outside.md');(tmp_path/'outside.md').write_text('secret')
    assert import_vault(w.s,'default',vault)==1
    assert import_vault(w.s,'default',vault)==1
    assert len(w.s.lexical('default','Existing'))==1
    assert not w.s.lexical('default','secret')


class Enrollment:
    """The operator's existing bands, as the enrollment registry reports them."""
    def bands(self, active_only=False):
        return [{'id':'26f8c02c','name':'bakenet','label':'7f68c499','is_primary':1},
                {'id':'c8cbc05b','name':'rooknet','label':'6178ba5f','is_primary':0}]


@pytest.mark.asyncio
async def test_bands_are_existing_enrollment_bands_by_id_name_or_label(work):
    s=KnowledgeService(work.s.path,lambda:{'kind':'agent','agent_id':'agent_x','label':'x'},Enrollment())
    assert s.band()=='26f8c02c'
    assert s.band('rooknet')==s.band('6178ba5f')==s.band('c8cbc05b')=='c8cbc05b'
    with pytest.raises(ValueError): s.band('not-a-band')
    created=await s.dispatch('create','rooknet','concept',data={'title':'On rooknet'},request_id='r1')
    assert created['band']=='c8cbc05b' and created['creator']=='agent_x'
    assert {b['id'] for b in await s.dispatch('bands')}=={'26f8c02c','c8cbc05b'}


@pytest.mark.asyncio
async def test_shared_and_unverified_callers_may_write_and_are_labelled(work):
    s=KnowledgeService(work.s.path,lambda:{'kind':'shared','label':'static'})
    r=await s.dispatch('create',None,'knowledge',data={'title':'From static'},request_id='a')
    assert r['creator']=='shared:static'
    s=KnowledgeService(work.s.path,lambda:None)
    r=await s.dispatch('create',None,'knowledge',data={'title':'Unverified'},request_id='b')
    assert r['creator']=='unverified'


@pytest.mark.asyncio
async def test_hybrid_search_uses_embeddings_and_filters_bands(work):
    w=work
    r=w.create('knowledge','Automobile repair')
    s=KnowledgeService(w.s.path,lambda:None)
    s.search.url='http://embedding.test'
    s.search.embed=AsyncMock(return_value=[[1.0]+[0.0]*383])
    with w.s.db() as db: db.execute('INSERT INTO embeddings VALUES(?,?,?,?)',(r['id'],r['revision'],s.search.model,json.dumps([1.0]+[0.0]*383)))
    result=await s.search.query('default','fix a car')
    assert result['semantic'] and result['results'][0]['id']==r['id']
    assert not (await s.search.query('another','fix a car'))['results']
    s.search.embed.side_effect=RuntimeError('offline')
    result=await s.search.query('default','Automobile')
    assert not result['semantic'] and result['results'][0]['id']==r['id']


@pytest.mark.asyncio
async def test_human_route_requires_operator_login_and_csrf(work):
    w=work;s=KnowledgeService(w.s.path,lambda:None)
    accounts=SimpleNamespace(session=lambda cookie:{'id':'bake','name':'Bake','csrf':'csrf','admin':cookie=='admin'} if cookie else None)
    app=Starlette(routes=routes(s,accounts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/knowledge/account-api')).status_code==401
        assert (await client.get('/knowledge/account-api',headers={'Cookie':'rook_account=member'})).status_code==403
        headers={'Cookie':'rook_account=admin'}
        body={'action':'create','kind':'concept','request_id':'h1','data':{'title':'Human idea'}}
        assert (await client.post('/knowledge/account-api',headers=headers,json=body)).status_code==403
        r=await client.post('/knowledge/account-api',headers=headers,json=body|{'csrf':'csrf'})
        assert r.status_code==200,r.text
        assert r.json()['result']['creator']=='human:bake'
        for action in ('approve','start','revoke','reconcile'):
            r=await client.post('/knowledge/account-api',headers=headers,json={'action':action,'csrf':'csrf','id':w.task['id'],'request_id':action})
            assert r.status_code==400


# --- MCP integration: additive, opt-in, never in the call path --------------

class FakeBand:
    def __init__(self):
        self.calls=[]
        self.workers={'w1':{'worker_id':'w1','name':'kaiju','band':'deadbeef','caps':['shell.exec','file.list'],'last_seen':0}}
    async def call(self,cap,args=None,target=None,timeout=15.0,identity=None):
        self.calls.append(cap)
        return {'id':'c','from':target,'ok':True,'result':{}}


@asynccontextmanager
async def session(tmp_path, monkeypatch, enabled=True, db=None):
    from rook.band_mcp.server import build_server
    monkeypatch.setenv('ROOK_KNOWLEDGE','1' if enabled else '0')
    if db: monkeypatch.setenv('ROOK_KNOWLEDGE_DB',db)
    band=FakeBand()
    mcp,store=build_server(band,public_url='https://mcp.example.com',persist_path=str(tmp_path/'tokens.json'),
                           static_token='static-token-0123456789abcdef',journal_path=str(tmp_path/'journal.db'))
    token=store.mint_api_token('codex')
    app=mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',
            headers={'Accept':'application/json, text/event-stream','Authorization':'Bearer '+token['token']}) as http:
        r=await http.post('/mcp',json={'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'t','version':'1'}}})
        http.headers['mcp-session-id']=r.headers['mcp-session-id']
        await http.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'})
        async def rpc(method,params):
            r=await http.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
            body=r.json() if r.headers['content-type'].startswith('application/json') else json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith('data:')))
            return body['result']
        yield SimpleNamespace(rpc=rpc,band=band,token=token)


@pytest.mark.asyncio
async def test_mcp_knowledge_is_attributed_and_leaves_rook_call_alone(tmp_path,monkeypatch):
    async with session(tmp_path,monkeypatch) as env:
        tools={t['name'] for t in (await env.rpc('tools/list',{}))['tools']}
        assert {'rook_knowledge','rook_concept','rook_project','rook_task'}<=tools
        assert 'rook_attempt' not in tools
        res=await env.rpc('tools/call',{'name':'rook_concept','arguments':{'action':'create','request_id':'c1','data':{'title':'Idea'}}})
        created=json.loads(res['content'][0]['text'])['result']
        assert created['creator']==env.token['agent_id']
        for cap in ('shell.exec','file.list'):
            res=await env.rpc('tools/call',{'name':'rook_call','arguments':{'cap':cap,'worker':'kaiju'}})
            assert not res.get('isError') and 'attempt' not in res['content'][0]['text']
        assert env.band.calls==['shell.exec','file.list']


@pytest.mark.asyncio
async def test_knowledge_is_off_by_default_and_a_broken_store_never_blocks_the_server(tmp_path,monkeypatch):
    async with session(tmp_path,monkeypatch,enabled=False) as env:
        assert 'rook_knowledge' not in {t['name'] for t in (await env.rpc('tools/list',{}))['tools']}
    bad=tmp_path/'is-a-directory';bad.mkdir()
    async with session(tmp_path,monkeypatch,db=str(bad)) as env:
        assert 'rook_knowledge' not in {t['name'] for t in (await env.rpc('tools/list',{}))['tools']}
        res=await env.rpc('tools/call',{'name':'rook_call','arguments':{'cap':'shell.exec','worker':'kaiju'}})
        assert not res.get('isError')
