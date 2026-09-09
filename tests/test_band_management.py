"""Account isolation, band lifecycle, and the web migration controller."""
import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rook.remote.account_web import AccountWeb
from rook.remote.enrollment import EnrollmentStore
from rook.remote.migration import proof_message
from rook.worker.device_key import certificate_request


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_SETUP_PATH',str(tmp_path/'setup.json'))
    monkeypatch.delenv('ROOK_GOOGLE_CLIENT_FILE',raising=False)
    enrollment=EnrollmentStore(tmp_path/'enrollment.db')
    source=enrollment.register('Home','original-key','hub.example.com:443',primary=True)
    server=SimpleNamespace(_enrollment=enrollment,web_user='operator',web_pass='operator-password',
                           domain='rook.example.com',hub_public='hub.example.com:443',_band=None,
                           _ban_match=lambda *args:False,_sync_enrollment=AsyncMock())
    account=AccountWeb(server)
    app=web.Application();account.install(app)
    uid=account.bootstrap_id;token=account.store.new_session(uid)
    user=account.store.session(token)
    return SimpleNamespace(app=app,server=server,account=account,store=account.store,uid=uid,
        source=source,csrf=user['csrf'],headers={'Cookie':'rook_account='+token,'Origin':account.origin})


@pytest.mark.asyncio
async def test_band_crud_membership_and_csrf(portal):
    p=portal
    async with TestClient(TestServer(p.app)) as client:
        r=await client.get('/account/bands/api',allow_redirects=False)
        assert r.status==302
        r=await client.get('/account/bands',headers=p.headers)
        assert r.status==200 and 'Move to band' in await r.text()
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'create','name':'New'})
        assert r.status==403
        r=await client.post('/account/bands/api',headers={**p.headers,'Origin':'https://evil.example'},json={'op':'create','name':'New','csrf':p.csrf})
        assert r.status==403
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'create','name':'<New>','csrf':p.csrf})
        assert r.status==200;bid=(await r.json())['id']
        guest=p.store.create_local('guest','long enough test password')
        with p.store.db() as db:db.execute("INSERT INTO memberships VALUES(?,?,'member')",(bid,guest))
        guest_token=p.store.new_session(guest);guest_user=p.store.session(guest_token)
        for op in ('rename','delete','prepare'):
            r=await client.post('/account/bands/api',headers={'Cookie':'rook_account='+guest_token},json={'op':op,'name':'stolen','band_id':bid,'csrf':guest_user['csrf'],'confirm':True})
            assert r.status==403
        r=await client.get('/account/bands/api',headers={'Cookie':'rook_account='+guest_token})
        assert [b['id'] for b in (await r.json())['bands']]==[bid]
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'rename','band_id':bid,'name':'Renamed','csrf':p.csrf})
        assert r.status==200
        old_key=next(b['psk'] for b in p.store.bands(p.uid,configs=True) if b['id']==bid)
        p.server._enrollment.issue(bid)
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'delete','band_id':bid,'csrf':p.csrf})
        assert r.status==400
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'delete','band_id':bid,'csrf':p.csrf,'confirm':True})
        assert r.status==200
        assert bid not in {b['id'] for b in p.store.bands(p.uid)}
        assert bid not in {b['id'] for b in p.server._enrollment.bands()}
        with pytest.raises(ValueError):p.server._enrollment.register('resurrect',old_key)
        with pytest.raises(ValueError):p.server._enrollment.rotate(bid)
        r=await client.post('/account/bands/api',headers=p.headers,json={'op':'delete','band_id':p.source['id'],'csrf':p.csrf,'confirm':True})
        assert r.status==400


def add_worker(p,wid='worker1',move=True):
    key,csr=certificate_request()
    issued=p.account.devices.enroll(p.source['id'],p.uid,csr,wid)
    caps=['worker.enrollment_prepare','worker.enrollment_finish','worker.enrollment_prove']
    if move:caps.append('worker.enrollment_move_prepare')
    worker={'worker_id':wid,'name':wid,'caps':caps,'band':p.source['psk_hash'][:8],'last_seen':time.time()}
    async def call(cap,args,target,timeout):
        return {'ok':True,'from':wid,'result':{'enrolled':True,'worker_id':wid,'device_id':issued['device_id'],'band_id':p.source['id']}}
    source=SimpleNamespace(label=worker['band'],workers={wid:worker},call=AsyncMock(side_effect=call))
    p.server._band=SimpleNamespace(workers={wid:worker},_clients=[source])
    return key,issued,worker,source


def device_proof(p,key,issued,purpose):
    challenge=p.account.devices.challenge(issued['device_id'],purpose)
    return {'challenge':challenge['challenge'],'certificate':issued['certificate'],
            'signature':base64.b64encode(key.sign(challenge['message'].encode()).signature).decode()}


@pytest.mark.asyncio
async def test_inline_create_move_persists_and_verifies_on_destination(portal):
    p=portal;key,issued,worker,source=add_worker(p)
    async with TestClient(TestServer(p.app)) as client:
        data={'op':'prepare','mode':'move','band_id':p.source['id'],'workers':['worker1'],
              'new_band_name':'Work','confirm':True,'csrf':p.csrf}
        r=await client.post('/account/bands/api',headers=p.headers,json=data)
        assert r.status==200,await r.text()
        migration=await r.json();mid=migration['id'];target_id=migration['target_band_id']
        assert migration['phase']=='prepared' and 'new_psk' not in migration
        advance={'op':'advance','migration_id':mid,'csrf':p.csrf}
        r=await client.post('/account/bands/api',headers=p.headers,json=advance)
        assert (await r.json())['phase']=='prepared'
        config=p.account.devices.config(device_proof(p,key,issued,'config'))
        target=config['migration']['band']
        ack=device_proof(p,key,issued,'stage:'+mid);ack['migration_id']=mid;p.account.devices.staged(ack)
        # A different connection provides the signed proof from the target band.
        async def prove(cap,args,target,timeout):
            assert cap=='worker.enrollment_prove' and target=='worker1'
            message=proof_message(mid,issued['device_id'],target,args['epoch'],args['nonce'])
            return {'ok':True,'from':'worker1','result':{'nonce':args['nonce'],'certificate':issued['certificate'],
                'signature':base64.b64encode(key.sign(message).signature).decode()}}
        destination=SimpleNamespace(label=hashlib.sha256(target['psk'].encode()).hexdigest()[:8],workers={'worker1':worker},call=AsyncMock(side_effect=prove))
        p.server._band._clients.append(destination)
        # Recreate the controller to model a server restart after staging.
        from rook.remote.band_web import BandWeb
        resumed=BandWeb(p.account)
        result=await resumed.advance(p.uid,mid)
        assert result['phase']=='complete'
        assert source.call.await_count==1 and destination.call.await_count==1
        assert p.account.devices.config(device_proof(p,key,issued,'config'))['band']['id']==target_id
        assert p.source['psk'] in p.server._enrollment.transport_psks()
        r=await client.get('/account/bands/api',headers=p.headers)
        text=await r.text()
        assert p.source['psk'] not in text and target['psk'] not in text
        assert 'new_psk' not in text and 'new_hash' not in text


@pytest.mark.asyncio
async def test_all_migration_blocks_missing_devices_and_unsupported_workers(portal):
    p=portal;key,issued,worker,source=add_worker(p,move=False)
    target=p.store.create_band(p.uid,'Target',p.server.hub_public)
    data={'mode':'move','band_id':p.source['id'],'workers':['worker1'],'target_band_id':target}
    with pytest.raises(ValueError,match='compatible'):await p.account.band_web.prepare(p.uid,data)
    source.call.assert_not_awaited()
    key,csr=certificate_request();p.account.devices.enroll(p.source['id'],p.uid,csr,'offline')
    with pytest.raises(ValueError,match='offline'):await p.account.band_web.prepare(p.uid,{**data,'mode':'psk'})
    with p.store.db() as db:assert db.execute('SELECT count(*) FROM band_migrations').fetchone()[0]==0


@pytest.mark.asyncio
async def test_empty_psk_rotation_and_offline_device_guard(portal):
    p=portal
    data={'band_id':p.source['id'],'mode':'psk','workers':[]}
    result=await p.account.band_web.prepare(p.uid,data)
    assert result['phase']=='complete' and 'psk' not in result
    key,csr=certificate_request();p.account.devices.enroll(p.source['id'],p.uid,csr,'offline')
    with pytest.raises(ValueError,match='Enrolled'):await p.account.band_web.prepare(p.uid,data)


def test_existing_migration_database_is_upgraded_without_losing_progress(tmp_path):
    import sqlite3
    path=tmp_path/'enrollment.db'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE band_migrations (
            id TEXT PRIMARY KEY,band_id TEXT NOT NULL,old_epoch INTEGER NOT NULL,
            new_psk TEXT,new_hash TEXT NOT NULL,phase TEXT NOT NULL,
            created REAL NOT NULL,deadline REAL NOT NULL,owner TEXT NOT NULL)''')
        db.execute("INSERT INTO band_migrations VALUES('existing','band',3,'pending-key','hash','prepared',1,2,'owner')")
    EnrollmentStore(path)
    EnrollmentStore(path)  # Repeated startup is idempotent.
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT * FROM band_migrations WHERE id='existing'").fetchone()
        assert row['new_psk']=='pending-key' and row['phase']=='prepared'
        assert row['target_band_id'] is None and row['full_inventory']==1
        assert 'deleted' in {r[1] for r in db.execute('PRAGMA table_info(bands)')}
