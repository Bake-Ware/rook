import asyncio
import hashlib
import json
import pytest
from services.voice.identity import (Identity, authorize_devices, authorize_read, current_identity, identity_for,
                                     on_owner_phone)
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


def test_device_mapped_key_reads_only_its_own_device():
    async def scenario():
        offered=[];reads=[];events=[]
        calls=[{'function':{'name':name,'arguments':json.dumps(args)}} for name,args in
               (('rook_devices',{}),('delegate_to_hermes',{'task':'list every device'}),
                ('rook_read',{'worker':'nas','cap':'file.read'}),('rook_read',{'worker':'phone','cap':'sms.list'}))]
        class Provider:
            default_voice='test'
            system='test'
            async def chat(self,messages,on_clause,reply_only=False,tools=None):
                offered.append(tools);return '',calls
        async def rook_read(args):
            # Same check tool_rook_read makes; it runs inside the background job task.
            authorize_read(args['cap'],args['worker']);reads.append(args['worker']);return 'ok'
        async def devices(args):
            authorize_devices();return 'devices'
        store=Store(':memory:')
        async def send(event):events.append(event)
        jobs=Jobs(store,{'rook_read':rook_read,'rook_devices':devices},'',0,lambda *a:None)
        conn=Connection(store,jobs,Provider(),'session',send,send,identity=Identity('Alex','phone'))
        await conn.start(text='read stuff',speak=False)
        await conn.task
        assert offered[0]=={'web_search','end_session','cancel_job','job_status','rook_read'}
        started={j['name']:j for j in store.jobs('session')}
        assert set(started)=={'rook_read'}  # rook_devices and delegate never became jobs
        await asyncio.gather(*list(jobs.tasks.values()))
        results=sorted((json.loads(j['args'])['worker'],j['status'],j['result']) for j in store.jobs('session'))
        assert results[0][:2]==('nas','failed') and 'own device' in results[0][2]
        assert results[1][:2]==('phone','completed')
        assert reads==['phone']
        assert sum('owner voice key' in e.get('text','') for e in events if e['type']=='assistant_delta')==2
        assert current_identity.get()==Identity()
        await jobs.close();await conn.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('owner',[False,True])
def test_unrestricted_agent_work_requires_owner(owner):
    async def scenario():
        seen=[]
        class Provider:
            default_voice='test'
            system='test'
            async def chat(self,messages,on_clause,reply_only=False,tools=None):
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


def heard(label, score, owner='Bake'):
    return {'label': label, 'score': score, 'principal': label if label not in ('guest', 'assistant') else None,
            'candidate': owner, 'scores': {owner: score}}


@pytest.mark.parametrize('result,speech_s,expected', [
    (heard('Bake', 0.62), 2.0, ('Bake', True, 'voice')),
    (heard('guest', 0.30), 2.0, ('Bake', True, 'owner phone')),       # unsure means Bake
    (heard('guest', 0.05), 0.7, ('Bake', True, 'owner phone')),       # too short to say "not him"
    (heard('guest', 0.05), None, ('Bake', True, 'owner phone')),
    (None, 1.5, ('Bake', True, 'owner phone')),                       # no voiceprint at all
    (heard('guest', 0.05), 1.5, ('guest', False, 'voice')),           # a strong mismatch
    ({**heard('Logan', 0.6), 'scores': {'Bake': 0.2, 'Logan': 0.6}}, 0.8, ('Logan', True, 'voice')),
])
def test_on_owner_phone_defaults_to_the_owner(result, speech_s, expected):
    who = on_owner_phone(result, 'Bake', speech_s)
    assert (who['label'], who['sure'], who['basis']) == expected


def test_on_owner_phone_leaves_the_assistant_echo_alone():
    assert on_owner_phone(heard('assistant', 0.1), 'Bake', 2.0) is None


# Replay of real turns from kaiju's voice log (voice_turn_timing: raw label, score against
# Bake's print, speech_s), 2026-10-27 06:55-07:10: every one was Bake on his phone, a third
# of them heard as "guest" before. And Logan in the children's mode, 2026-10-25 18:42-18:48
# and 2026-10-26 23:00 (scores under 0.13, apart from a few lines that were likely Bake).
BAKE_20261027 = [('Bake', 0.4701, 2.0), ('Bake', 0.4658, 1.31), ('guest', 0.2504, 1.36), ('Bake', 0.5272, 1.19),
                 ('Bake', 0.5511, 1.56), ('Bake', 0.6012, 5.22), ('guest', 0.1743, 0.88), ('Bake', 0.562, 2.56),
                 ('Bake', 0.4848, 1.62), ('Bake', 0.541, 1.65), ('guest', 0.2712, 1.66), ('Bake', 0.4785, 1.22),
                 ('Bake', 0.4588, 2.42), ('guest', 0.3349, 1.6), ('guest', 0.3637, 4.34), ('Bake', 0.4543, 1.54),
                 ('Bake', 0.5144, 4.72), ('guest', 0.2242, 1.4), ('guest', 0.4475, 1.96), ('guest', 0.3458, 1.32),
                 ('Bake', 0.4927, 1.68), ('guest', 0.2692, 1.0), ('Bake', 0.5095, 3.5), ('Bake', 0.6698, 8.56),
                 ('guest', 0.4196, 1.58), ('Bake', 0.5919, 4.62)]
LOGAN_20261025 = [(-0.0835, 0.94), (-0.0089, 1.42), (0.0068, 1.6), (0.012, 3.06), (0.0254, 1.01), (0.0289, 1.18),
                  (0.0302, 1.15), (0.0387, 0.94), (0.0389, 0.94), (0.0461, 1.1), (0.0507, 0.82), (0.0523, 3.26),
                  (0.064, 3.74), (0.0647, 3.54), (0.0686, 1.33), (0.0862, 3.11), (0.0895, 2.36), (0.0918, 1.64),
                  (0.0934, 7.45), (0.0962, 3.13), (0.0983, 2.36), (0.1021, 3.28), (0.104, 1.48), (0.1093, 2.59),
                  (0.112, 2.01), (0.1127, 6.16), (0.1131, 2.2), (0.1171, 2.8), (0.1221, 1.12), (0.1227, 2.53),
                  (0.1266, 1.6)]


def test_replay_real_turns_bake_stays_bake():
    labels = [on_owner_phone(heard(label, score), 'Bake', speech)['label'] for label, score, speech in BAKE_20261027]
    assert labels == ['Bake'] * len(BAKE_20261027)
    assert sum(1 for label, _, _ in BAKE_20261027 if label == 'guest') == 10      # what he got before


def test_replay_real_turns_logan_is_not_bake_on_real_speech():
    labels = [on_owner_phone(heard('guest', score), 'Bake', speech)['label'] for score, speech in LOGAN_20261025]
    not_bake = [label for (score, speech), label in zip(LOGAN_20261025, labels) if speech >= 1.0]
    assert not_bake == ['guest'] * len(not_bake) and len(not_bake) >= 25
    # Her lines under a second carry no "definitely not": those count as the owner's phone's owner.
    assert all(label == 'Bake' for (score, speech), label in zip(LOGAN_20261025, labels) if speech < 1.0)
