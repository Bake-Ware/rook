"""Regression coverage for session ownership and failed/cancelled calls."""
import asyncio
import gc
import json
import secrets
import weakref
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from rook.band_mcp.client import BandClient
from rook.band_mcp.http_sessions import BoundedSessionManager


@asynccontextmanager
async def server(idle=0.05, capacity=4):
    mcp=FastMCP('regression')
    started=asyncio.Event()
    @mcp.tool()
    async def slow():
        started.set()
        await asyncio.sleep(.15)
        return 'done'
    manager=BoundedSessionManager(mcp._mcp_server,idle_seconds=idle,max_sessions=capacity)
    mcp._session_manager=manager
    app=mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',headers={'Accept':'application/json, text/event-stream'}) as http:
        yield manager,http,started


async def initialize(http):
    r=await http.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
    if r.status_code!=200:
        return r
    sid=r.headers['mcp-session-id']
    await http.post('/mcp',headers={'mcp-session-id':sid},json={'jsonrpc':'2.0','method':'notifications/initialized'})
    return sid


@pytest.mark.asyncio
async def test_delete_releases_transport_and_tasks():
    async with server() as (manager,http,_):
        for _ in range(10):
            sid=await initialize(http)
            ref=weakref.ref(manager._server_instances[sid])
            assert (await http.delete('/mcp',headers={'mcp-session-id':sid})).status_code==200
            await asyncio.sleep(.01)
            gc.collect()
            assert not manager._server_instances
            assert not manager._owners
            assert ref() is None


@pytest.mark.asyncio
async def test_abandoned_sessions_expire_and_old_id_is_404():
    async with server() as (manager,http,_):
        sid=await initialize(http)
        ref=weakref.ref(manager._server_instances[sid])
        await asyncio.sleep(.12)
        gc.collect()
        assert not manager._server_instances
        assert not manager._owners
        assert ref() is None
        assert (await http.post('/mcp',headers={'mcp-session-id':sid},json={})).status_code==404
        assert isinstance(await initialize(http),str)


@pytest.mark.asyncio
async def test_capacity_rejects_new_without_evicting_active_session():
    async with server(idle=10,capacity=2) as (manager,http,_):
        first=await initialize(http)
        await initialize(http)
        r=await initialize(http)
        assert r.status_code==503
        assert len(manager._server_instances)==2
        await http.delete('/mcp',headers={'mcp-session-id':first})
        assert isinstance(await initialize(http),str)


@pytest.mark.asyncio
async def test_active_call_outlives_idle_deadline_then_expires():
    async with server() as (manager,http,started):
        sid=await initialize(http)
        task=asyncio.create_task(http.post('/mcp',headers={'mcp-session-id':sid},json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'slow','arguments':{}}}))
        await started.wait()
        await asyncio.sleep(.08)
        assert sid in manager._server_instances
        assert (await task).status_code==200
        await asyncio.sleep(.12)
        assert not manager._server_instances


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['failure','cancel','timeout','reply','serialize'])
async def test_pending_futures_always_released(mode):
    client=BandClient(secrets.token_hex(32))
    entered=asyncio.Event()
    async def send(payload):
        entered.set()
        if mode=='failure': raise OSError('synthetic')
        if mode=='reply':
            msg=json.loads(payload)
            client._handle_reply({'id':msg['id'],'from':'test','ok':True})
        else: await asyncio.sleep(10)
    client.transport.send=send
    task=asyncio.create_task(client.call('test',args={'bad':object()} if mode=='serialize' else {},timeout=.03))
    if mode=='cancel':
        await entered.wait()
        task.cancel()
    result=await asyncio.gather(task,return_exceptions=True)
    assert not client._pending
    if mode=='reply': assert result[0]['ok']
    else: assert isinstance(result[0],BaseException)


@pytest.mark.asyncio
async def test_http_send_failure_releases_session():
    async with server(idle=10) as (manager,http,_):
        sid=await initialize(http)
        scope={'type':'http','method':'POST','path':'/mcp','headers':[(b'mcp-session-id',sid.encode()),(b'content-type',b'application/json'),(b'accept',b'application/json, text/event-stream')],'query_string':b'','http_version':'1.1','scheme':'http','server':('localhost',80)}
        async def receive():
            await asyncio.sleep(.001)
            return {'type':'http.request','body':b'{"jsonrpc":"2.0","id":3,"method":"ping"}','more_body':False}
        async def send(message): raise OSError('synthetic HTTP send failure')
        await manager.handle_request(scope,receive,send)
        await asyncio.sleep(.02)
        assert not manager._server_instances
        assert not manager._owners


@pytest.mark.asyncio
async def test_oauth_codes_bounded_and_expired_collected(monkeypatch):
    import rook.band_mcp.oauth_shim as module
    from rook.band_mcp.tokens import TokenStore
    from starlette.applications import Starlette
    monkeypatch.setattr(module,'_MAX_CODES',3)
    shim=module.OAuthShim(Starlette(),TokenStore(),'https://example.test')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=shim),base_url='https://example.test') as http:
        q={'response_type':'code','redirect_uri':'http://localhost/callback','code_challenge':'synthetic'}
        for _ in range(3): assert (await http.get('/authorize',params=q)).status_code==302
        assert (await http.get('/authorize',params=q)).status_code==503
        for record in shim._codes.values(): record['exp']=0
        assert (await http.get('/authorize',params=q)).status_code==302
        assert len(shim._codes)==1


def test_admin_sessions_bounded_and_expired_collected():
    from rook.band_mcp.tokens import TokenStore
    password=secrets.token_hex(32)
    store=TokenStore(admin_password=password)
    for _ in range(300): store.admin_login(password)
    assert len(store._admin_sessions)==256
    for sid in store._admin_sessions: store._admin_sessions[sid]=0
    store.admin_login(password)
    assert len(store._admin_sessions)==1


def test_console_unterminated_output_is_bounded_and_preserved(tmp_path):
    from rook.band_mcp.console_rooms import ConsoleStore, MAX_LINE
    store=ConsoleStore(str(tmp_path/'console.db'))
    room=store.open(title='synthetic',worker='w',worker_name='w',handle='h',
                    cmd='synthetic',pty=False,opened_by='synthetic')['room']
    chunk='x'*8192
    for _ in range(100):
        store.append(room,chunk)
        assert len(store._pending.get(room,'')) < MAX_LINE
    store.append(room,'',flush=True)
    assert room not in store._pending
    assert ''.join(line['text'] for line in store.read(room,limit=1000)['lines']) == chunk*100
    store.close()
