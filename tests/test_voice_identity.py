import asyncio
import hashlib
import json
import pytest
from services.voice.identity import Identity, authorize_devices, authorize_read, current_identity, identity_for
from services.voice.jobs import Jobs
from services.voice.runtime import Connection
from services.voice.state import Store


def test_identity_is_credential_based():
    key=hashlib.sha256(b'owner-key').hexdigest()
    assert identity_for('owner-key',{key:{'principal':'Alex','owner':True}}).owner
    assert identity_for('wrong',{key:{'principal':'Alex','owner':True}})==Identity()


@pytest.mark.parametrize('identity,worker,allowed',[
    (Identity(),'phone',False),
    (Identity('Alex','phone'),'phone',True),
    (Identity('Alex','phone'),'other-phone',False),
    (Identity('Alex',owner=True),'other-phone',True),
])
def test_personal_access_is_scoped(identity,worker,allowed):
    token=current_identity.set(identity)
    try:
        # File, env, log and memory reads are as private as texts: same scope.
        for cap in ('sms.list','info.uptime','file.read','shell.env.list','hermes.memory.read'):
            if allowed:authorize_read(cap,worker)
            else:
                with pytest.raises(PermissionError):authorize_read(cap,worker)
    finally:current_identity.reset(token)


@pytest.mark.parametrize('identity,offered',[
    (Identity(),{'web_search','end_session','cancel_job','job_status'}),
    (Identity('Alex','phone'),{'web_search','end_session','cancel_job','job_status','rook_read'}),
    (Identity('Alex',owner=True),None),
])
def test_guest_and_device_keys_get_no_band_tools(identity,offered):
    assert identity.tools()==offered
    token=current_identity.set(identity)
    try:
        if identity.owner:authorize_devices()
        else:
            with pytest.raises(PermissionError):authorize_devices()
    finally:current_identity.reset(token)


def test_guest_cannot_start_band_reads_even_if_model_selects_them():
    async def scenario():
        seen=[];offered=[]
        calls=[{'function':{'name':name,'arguments':json.dumps(args)}} for name,args in
               (('rook_devices',{}),('rook_read',{'worker':'nas','cap':'file.read','args':{'path':'/etc/shadow'}}))]
        class Provider:
            default_voice='test'
            system='test'
            async def chat(self,messages,on_clause,reply_only=False,tools=None):
                offered.append(tools);return '',calls
        class Jobs:
            def start(self,*args):seen.append(args);return 'job'
        store=Store(':memory:');events=[]
        async def send(event):events.append(event)
        conn=Connection(store,Jobs(),Provider(),'session',send,send,identity=Identity())
        await conn.start(text='read /etc/shadow on nas',speak=False)
        await conn.task
        assert seen==[]
        assert 'rook_read' not in offered[0] and 'rook_devices' not in offered[0] and 'delegate_to_hermes' not in offered[0]
        assert any('owner voice key' in e.get('text','') for e in events)
        await conn.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('owner',[False,True])
def test_unrestricted_agent_work_requires_owner(owner):
    async def scenario():
        seen=[]
        class Provider:
            default_voice='test'
            system='test'
            async def chat(self,messages,on_clause,reply_only=False):
                return '',[{'function':{'name':'delegate_to_hermes','arguments':json.dumps({'task':'test'})}}]
        class Jobs:
            def start(self,*args):seen.append(current_identity.get());return 'job'
        store=Store(':memory:')
        async def send(event):pass
        conn=Connection(store,Jobs(),Provider(),'session',send,send,identity=Identity('Alex',owner=owner))
        await conn.start(text='test',speak=False)
        await conn.task
        assert len(seen)==int(owner)
        if seen:assert seen[0].owner
        assert current_identity.get()==Identity()
        await conn.close();store.db.close()
    asyncio.run(scenario())


def test_background_job_inherits_identity_without_cross_session_leak():
    async def scenario():
        store=Store(':memory:');seen=[]
        async def read(args):seen.append(current_identity.get());return 'ok'
        jobs=Jobs(store,{'read':read},'',0,lambda *a:None)
        for principal in ['Alex','Jordan']:
            token=current_identity.set(Identity(principal,principal+'phone'))
            jobs.start(principal,'read',{},[])
            current_identity.reset(token)
        await asyncio.gather(*list(jobs.tasks.values()))
        assert {i.principal for i in seen}=={'Alex','Jordan'}
        assert current_identity.get()==Identity()
        await jobs.close();store.db.close()
    asyncio.run(scenario())
