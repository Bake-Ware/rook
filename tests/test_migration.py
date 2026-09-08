import base64
import hashlib
from types import SimpleNamespace

import pytest

from rook.remote.enrollment import EnrollmentStore
from rook.remote.accounts import AccountStore
from rook.remote.devices import DeviceStore
from rook.remote.migration import MigrationStore,proof_message
from rook.worker import device_key


@pytest.fixture
def fleet(tmp_path,monkeypatch):
    monkeypatch.setenv('ROOK_SETUP_PATH',str(tmp_path/'setup.json'))
    enrollment=EnrollmentStore(tmp_path/'enrollment.db')
    band=enrollment.register('test','old-permanent-key','hub.example.com:443',primary=True)
    accounts=AccountStore(enrollment)
    owner=accounts.bootstrap('operator','operator-password')
    devices=DeviceStore(accounts);migration=MigrationStore(accounts)
    entries=[]
    for n in range(2):
        key,csr=device_key.certificate_request()
        issued=devices.enroll(band['id'],owner,csr,'worker'+str(n))
        entries.append((key,issued,'worker'+str(n)))
    return SimpleNamespace(enrollment=enrollment,band=band,accounts=accounts,owner=owner,devices=devices,migration=migration,entries=entries)


def proof(fleet,entry,purpose='config'):
    key,issued,_=entry
    challenge=fleet.devices.challenge(issued['device_id'],purpose)
    return {'challenge':challenge['challenge'],'certificate':issued['certificate'],
            'signature':base64.b64encode(key.sign(challenge['message'].encode()).signature).decode()}


def prepare(f):
    return f.migration.prepare(f.owner,f.band['id'],{w:d['device_id'] for _,d,w in f.entries})['id']


def confirm(f,mid,entry):
    key,device,worker=entry
    challenge=f.migration.challenge(f.owner,mid,worker)
    signed={'nonce':challenge['nonce'],'certificate':device['certificate'],
            'signature':base64.b64encode(key.sign(proof_message(mid,device['device_id'],worker,challenge['epoch'],challenge['nonce'])).signature).decode()}
    f.migration.confirm(f.owner,mid,worker,signed)
    return signed


def test_migration_waits_for_every_persisted_device_and_new_channel_proof(fleet):
    f=fleet;mid=prepare(f)
    assert len(f.enrollment.transport_psks())==2
    with pytest.raises(ValueError,match='persist'):f.migration.activate(f.owner,mid)
    first=f.devices.config(proof(f,f.entries[0]))
    assert first['band']['psk']==f.band['psk']
    candidate=first['migration']['band']
    assert candidate['epoch']==2 and candidate['psk']!=f.band['psk']
    with pytest.raises(ValueError,match='persist'):f.migration.activate(f.owner,mid)
    for entry in f.entries:
        staged=proof(f,entry,'stage:'+mid);staged['migration_id']=mid;f.devices.staged(staged)
    f.migration.activate(f.owner,mid)
    assert f.devices.config(proof(f,f.entries[0]))['band']==candidate
    with pytest.raises(ValueError,match='Every'):f.migration.finalize(f.owner,mid)
    signed=confirm(f,mid,f.entries[0])
    with pytest.raises(ValueError,match='already used'):f.migration.confirm(f.owner,mid,f.entries[0][2],signed)
    with pytest.raises(ValueError,match='Every'):f.migration.finalize(f.owner,mid)
    confirm(f,mid,f.entries[1]);f.migration.finalize(f.owner,mid)
    f.migration.finalize(f.owner,mid) # idempotent completion
    assert f.enrollment.transport_psks()==[candidate['psk']]
    assert f.devices.config(proof(f,f.entries[0]))['migration'] is None
    assert f.enrollment.bands(secrets_visible=True)[0]['id']==f.band['id']
    with pytest.raises(ValueError,match='revoked'):f.enrollment.register('stale',f.band['psk'])


def test_migration_wrong_device_revoke_and_emergency_rotation(fleet):
    f=fleet;mid=prepare(f)
    for entry in f.entries:
        p=proof(f,entry,'stage:'+mid);p['migration_id']=mid;f.devices.staged(p)
    f.migration.activate(f.owner,mid)
    challenge=f.migration.challenge(f.owner,mid,f.entries[0][2])
    wrong_key,wrong_device,_=f.entries[1]
    bad={'nonce':challenge['nonce'],'certificate':wrong_device['certificate'],'signature':base64.b64encode(wrong_key.sign(b'wrong').signature).decode()}
    with pytest.raises(PermissionError):f.migration.confirm(f.owner,mid,f.entries[0][2],bad)
    f.devices.revoke(f.owner,f.entries[0][1]['device_id'])
    with pytest.raises(PermissionError):f.devices.config(proof(f,f.entries[0]))
    replacement=f.enrollment.rotate(f.band['id'])
    assert f.enrollment.transport_psks()==[replacement['psk']]
    assert f.migration.status(f.owner,mid)['phase']=='superseded'
    with pytest.raises(ValueError):f.migration.activate(f.owner,mid)


def test_prepared_abort_keeps_old_key_and_requires_fresh_migration(fleet):
    f=fleet;mid=prepare(f)
    pending=f.devices.config(proof(f,f.entries[0]))['migration']['band']['psk']
    f.migration.abort(f.owner,mid)
    assert f.enrollment.transport_psks()==[f.band['psk']]
    assert f.devices.config(proof(f,f.entries[0]))['migration'] is None
    with pytest.raises(ValueError):f.enrollment.register('aborted',pending)
    other=prepare(f)
    with f.accounts.db() as db:db.execute('UPDATE band_migrations SET deadline=0 WHERE id=?',(other,))
    with pytest.raises(ValueError,match='expired'):f.migration.activate(f.owner,other)


def test_retried_csr_returns_same_identity_after_lost_enrollment_response(fleet):
    f=fleet
    key,csr=device_key.certificate_request()
    first=f.devices.enroll(f.band['id'],f.owner,csr,'retry')
    retry=f.devices.enroll(f.band['id'],f.owner,csr,'retry')
    assert first['device_id']==retry['device_id']
    assert first['certificate']==retry['certificate']
    f.devices.revoke(f.owner,first['device_id'])
    with pytest.raises(ValueError):f.devices.enroll(f.band['id'],f.owner,csr,'retry revoked')


@pytest.mark.asyncio
async def test_csr_bound_grant_discloses_no_psk_and_needs_private_key(fleet):
    from aiohttp.test_utils import TestClient,TestServer
    from rook.remote.bootstrap import CombinedServer
    f=fleet
    server=CombinedServer(band_psk=f.band['psk'],web_user='operator',web_pass='operator-password',domain='rook.example.com')
    # The HTTP server must share this fixture's already authorized band database.
    server._accounts.store=f.accounts;server._accounts.devices=f.devices
    key,csr=device_key.certificate_request()
    grant=f.accounts.grant('device_enroll',{'user_id':f.owner,'band_id':f.band['id'],'csr_hash':hashlib.sha256(csr.encode()).hexdigest()})
    async with TestClient(TestServer(server._app)) as client:
        response=await client.post('/auth/devices/enroll',json={'enrollment_grant':grant,'csr':csr})
        assert response.status==200
        issued=await response.json()
        assert 'band' not in issued and f.band['psk'] not in str(issued)
        result=await client.post('/auth/devices/config',json=proof(f,(key,issued,'http')))
        assert result.status==200 and (await result.json())['band']['psk']==f.band['psk']
        grant=f.accounts.grant('device_enroll',{'user_id':f.owner,'band_id':f.band['id'],'csr_hash':hashlib.sha256(csr.encode()).hexdigest()})
        _,other=device_key.certificate_request()
        response=await client.post('/auth/devices/enroll',json={'enrollment_grant':grant,'csr':other})
        assert response.status==403
