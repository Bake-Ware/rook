"""The phone's cross-band Workers tab: band rosters served with /auth/devices/config.

Authorization is the enrolled device's certificate proof, never a band PSK, and
only a device an account explicitly enrolled sees that account's other bands.
"""
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote.bootstrap import CombinedServer
from tests.test_accounts import accounts, certificate_request, device_proof, new_user  # noqa: F401


def _server():
    return CombinedServer(band_psk='old-key', web_user='operator', web_pass='operator-password',
                          domain='rook.example.com')


def _band(store, uid, name, psk):
    band = store.enrollment.register(name, psk, 'hub.example.com:443')
    store.assign(band['id'], uid)
    return band['id']


def _label(store, uid, bid):
    return next(b['label'] for b in store.bands(uid) if b['id'] == bid)


def _worker(wid, name, label, **extra):
    return {'worker_id': wid, 'name': name, 'band': label, 'last_seen': time.time() - 5,
            'caps': ['a.b', 'c.d', 'e.f'], 'plugins': ['secret-plugin'], 'facts': {'ip': '10.0.0.9'},
            'roles': ['is_hub'], 'description': 'a worker', 'version': '120.x', 'build': 120,
            'app_release': {'platform': 'android', 'version': '0.4.11', 'code': 15, 'extra': 'x'},
            'hb': {'battery': {'percent': 55, 'charging': False, 'temp': 30}, 'load': 1}, **extra}


@pytest.fixture
def fleet(accounts):  # noqa: F811
    server = _server()
    store = server._accounts.store
    alice = new_user(store)
    bob = new_user(store, 'bob')
    home = _band(store, alice, 'Home', 'alice-home-key')
    lab = _band(store, alice, 'Lab', 'alice-lab-key')
    work = _band(store, bob, 'Work', 'bob-work-key')
    labels = {bid: _label(store, uid, bid) for bid, uid in ((home, alice), (lab, alice), (work, bob))}
    server._band = SimpleNamespace(workers={
        'w-home': _worker('w-home', 'desk', labels[home]),
        'w-lab': _worker('w-lab', 'pi', labels[lab]),
        'w-work': _worker('w-work', 'office', labels[work]),
        'w-any': _worker('w-any', 'hub', '*'),
    })
    return SimpleNamespace(server=server, store=store, alice=alice, bob=bob,
                           home=home, lab=lab, work=work)


def _enroll(fleet, band, sponsor):
    key, csr = certificate_request()
    return key, fleet.server._accounts.devices.enroll(band, sponsor, csr, 'phone')


async def _config(client, fleet, enrolled, key, want=('workers',)):
    proof = device_proof(fleet.server._accounts.devices, enrolled, key)
    if want is not None:
        proof['want'] = list(want)
    response = await client.post('/auth/devices/config', json=proof)
    return response.status, await response.json()


@pytest.mark.asyncio
async def test_account_device_sees_only_its_accounts_bands(fleet):
    key, enrolled = _enroll(fleet, fleet.home, fleet.alice)       # account-sponsored
    bkey, benrolled = _enroll(fleet, fleet.work, fleet.bob)
    async with TestClient(TestServer(fleet.server._app)) as client:
        status, body = await _config(client, fleet, enrolled, key)
        assert status == 200 and body['band']['id'] == fleet.home   # config itself unchanged
        roster = body['workers']
        assert roster['scope'] == 'account' and roster['connected'] is True
        assert [(b['id'], b['name'], b['current']) for b in roster['bands']] == [
            (fleet.home, 'Home', True), (fleet.lab, 'Lab', False)]   # current band first
        assert [w['worker_id'] for b in roster['bands'] for w in b['workers']] == ['w-home', 'w-lab']
        text = json.dumps(roster)
        for leak in ('bob', 'Work', 'office', 'w-work', 'w-any', 'alice-lab-key', 'psk',
                     'label', 'secret-plugin', '10.0.0.9', 'is_hub'):
            assert leak not in text

        status, body = await _config(client, fleet, benrolled, bkey)
        assert [b['id'] for b in body['workers']['bands']] == [fleet.work]
        assert [w['name'] for w in body['workers']['bands'][0]['workers']] == ['office']


@pytest.mark.asyncio
async def test_payload_shape(fleet):
    key, enrolled = _enroll(fleet, fleet.home, fleet.alice)
    async with TestClient(TestServer(fleet.server._app)) as client:
        _, body = await _config(client, fleet, enrolled, key)
    band = body['workers']['bands'][0]
    assert set(band) == {'id', 'name', 'role', 'current', 'workers'} and band['role'] == 'owner'
    (worker,) = band['workers']
    assert set(worker) == {'worker_id', 'name', 'description', 'caps', 'version', 'build',
                           'app_release', 'hb', 'last_seen_age_secs'}
    assert worker['caps'] == 3 and worker['build'] == 120 and worker['version'] == '120.x'
    assert worker['app_release'] == {'platform': 'android', 'version': '0.4.11', 'code': 15}
    assert worker['hb'] == {'battery': {'percent': 55, 'charging': False}}
    assert 4 <= worker['last_seen_age_secs'] < 60
    assert isinstance(body['workers']['generated_at'], float)


@pytest.mark.asyncio
async def test_pair_code_device_gets_only_its_own_band(fleet):
    # A pairing code names the band's first owner as sponsor, but whoever held
    # the code is not necessarily that owner: no cross-band view.
    key, csr = certificate_request()
    devices = fleet.server._accounts.devices
    enrolled = devices.enroll(fleet.home, None, csr, 'paired phone')
    async with TestClient(TestServer(fleet.server._app)) as client:
        _, body = await _config(client, fleet, enrolled, key)
        roster = body['workers']
        assert roster['scope'] == 'band'
        assert [b['id'] for b in roster['bands']] == [fleet.home]
        assert [w['worker_id'] for w in roster['bands'][0]['workers']] == ['w-home']
        # The same key enrolled again through the account itself upgrades it.
        assert devices.enroll(fleet.home, fleet.alice, csr, 'phone')['device_id'] == enrolled['device_id']
        fleet.server._accounts._roster_served.clear()
        _, body = await _config(client, fleet, enrolled, key)
        assert body['workers']['scope'] == 'account' and len(body['workers']['bands']) == 2


@pytest.mark.asyncio
async def test_no_device_proof_no_roster(fleet):
    key, enrolled = _enroll(fleet, fleet.home, fleet.alice)
    async with TestClient(TestServer(fleet.server._app)) as client:
        # A PSK is no credential here, and neither is a bad or replayed proof.
        for body in ({'want': ['workers'], 'psk': 'alice-home-key'},
                     {'want': ['workers'], 'challenge': 'x', 'certificate': enrolled['certificate'], 'signature': 'AA=='}):
            response = await client.post('/auth/devices/config', json=body)
            assert response.status == 403 and 'workers' not in await response.json()
        wrong, _ = certificate_request()
        status, body = await _config(client, fleet, enrolled, wrong)
        assert status == 403 and 'workers' not in body
        fleet.server._accounts.devices.revoke(fleet.alice, enrolled['device_id'])
        status, body = await _config(client, fleet, enrolled, key)
        assert status == 403 and 'workers' not in body


@pytest.mark.asyncio
async def test_opt_in_and_rate_limited_per_device(fleet, monkeypatch):
    import rook.remote.account_web as account_web
    key, enrolled = _enroll(fleet, fleet.home, fleet.alice)
    async with TestClient(TestServer(fleet.server._app)) as client:
        _, body = await _config(client, fleet, enrolled, key, want=None)
        assert 'workers' not in body                                # old clients: unchanged
        _, body = await _config(client, fleet, enrolled, key)
        assert 'workers' in body
        _, body = await _config(client, fleet, enrolled, key)       # burst: config only
        assert 'workers' not in body and body['band']['id'] == fleet.home
        monkeypatch.setattr(account_web, 'ROSTER_MIN_INTERVAL', 0)
        _, body = await _config(client, fleet, enrolled, key)
        assert 'workers' in body


@pytest.mark.asyncio
async def test_hub_without_band_connection_still_configures(fleet):
    fleet.server._band = None
    key, enrolled = _enroll(fleet, fleet.home, fleet.alice)
    async with TestClient(TestServer(fleet.server._app)) as client:
        status, body = await _config(client, fleet, enrolled, key)
    assert status == 200 and body['workers']['connected'] is False
    assert all(b['workers'] == [] for b in body['workers']['bands'])


def test_backfill_marks_only_migration_enrolled_devices(accounts):  # noqa: F811
    from rook.remote.devices import DeviceStore
    owner = new_user(accounts)
    band = accounts.enrollment.bands()[0]['id']
    accounts.assign(band, owner)
    devices = DeviceStore(accounts)
    _, csr1 = certificate_request()
    _, csr2 = certificate_request()
    migrated = devices.enroll(band, owner, csr1, 'migrated')['device_id']
    paired = devices.enroll(band, None, csr2, 'paired')['device_id']
    with accounts.db() as db:
        db.execute('UPDATE devices SET created=created-86400 WHERE id=?', (paired,))
        db.execute("INSERT INTO band_migrations(id,band_id,old_epoch,new_hash,phase,created,deadline,owner) "
                   "VALUES('m1',?,0,'h','complete',?,?,?)", (band, time.time(), time.time() + 60, owner))
        db.executemany('INSERT INTO migration_workers(migration_id,worker_id,device_id) VALUES(?,?,?)',
                       [('m1', 'w1', migrated), ('m1', 'w2', paired)])
        db.execute('ALTER TABLE devices DROP COLUMN account_scope')   # a pre-upgrade database
    DeviceStore(accounts)
    with accounts.db() as db:
        scope = dict(db.execute('SELECT id,account_scope FROM devices').fetchall())
    assert scope == {migrated: 1, paired: 0}


def test_moved_device_with_new_sponsor_loses_account_scope(accounts):  # noqa: F811
    from rook.remote.devices import DeviceStore
    member = new_user(accounts)
    owner = new_user(accounts, 'bob')
    band = accounts.enrollment.bands()[0]['id']
    accounts.assign(band, owner)
    accounts.accept_invite(member, accounts.invite(owner, band))
    devices = DeviceStore(accounts)
    _, csr = certificate_request()
    device = devices.enroll(band, member, csr, 'member phone')['device_id']
    other = accounts.enrollment.register('Other', 'other-key', 'hub.example.com:443')['id']
    accounts.assign(other, owner)
    with accounts.db() as db:
        db.execute("INSERT INTO band_migrations(id,band_id,old_epoch,new_hash,phase,created,deadline,owner,target_band_id,target_epoch) "
                   "VALUES('m2',?,(SELECT epoch FROM bands WHERE id=?),'h','active',?,?,?,?,(SELECT epoch FROM bands WHERE id=?))", (band, band, time.time(), time.time() + 60, owner, other, other))
        db.execute("INSERT INTO migration_workers(migration_id,worker_id,device_id,confirmed) VALUES('m2','w',?,1)", (device,))
    devices.migrations._inventory_unchanged = lambda db, row: None
    devices.migrations.finalize(owner, 'm2')
    with accounts.db() as db:
        row = db.execute('SELECT sponsor,account_scope FROM devices WHERE id=?', (device,)).fetchone()
    assert row['sponsor'] == owner and row['account_scope'] == 0
