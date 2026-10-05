import asyncio
import hashlib
import json
import pytest
from services.voice.identity import Identity, authorize_read, current_identity, identity_for
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
        if allowed:authorize_read('sms.list',worker)
        else:
            with pytest.raises(PermissionError):authorize_read('sms.list',worker)
        authorize_read('info.uptime',worker)
    finally:current_identity.reset(token)


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
