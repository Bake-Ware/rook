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
    return key, fleet.server._accounts.devices.enroll(band, sponsor, csr, 'phone', account_scope=True)


async def _config(client, fleet, enrolled, key, want=('workers',)):
    proof = device_proof(fleet.server._accounts.devices, enrolled, key)
    if want is not None:
        proof['want'] = list(want)
    response = await client.post('/auth/devices/config', json=proof)
    return response.status, await response.json()


def test_app_release_strings_are_capped():
    from rook.remote.account_web import roster_row
    row = roster_row(_worker('w', 'n', 'x', app_release={'platform': 'p' * 5000, 'version': 'v' * 5000,
                                                         'code': 15, 'extra': 'x'}), time.time())
    assert row['app_release'] == {'platform': 'p' * 40, 'version': 'v' * 40, 'code': 15}


async def _grant_enroll(client, fleet, grant, csr, key):
    response = await client.post('/auth/devices/enroll', json={'enrollment_grant': grant, 'csr': csr})
    assert response.status == 200
    issued = await response.json()
    fleet.server._accounts._roster_served.clear()
    return await _config(client, fleet, issued, key)


def test_pair_code_enrollment_ignores_account_scope_without_a_sponsor(accounts):  # noqa: F811
    from rook.remote.devices import DeviceStore
    owner = new_user(accounts)
    band = accounts.enrollment.bands()[0]['id']
    accounts.assign(band, owner)
    devices = DeviceStore(accounts)
    _, csr = certificate_request()
    device = devices.enroll(band, None, csr, 'paired', account_scope=True)['device_id']
    with accounts.db() as db:
        assert db.execute('SELECT account_scope FROM devices WHERE id=?', (device,)).fetchone()[0] == 0


def test_upgrade_leaves_existing_devices_band_scoped(accounts):  # noqa: F811
    # No inference from migration history: a migration enrolled every worker
    # holding the band key under its owner, so it proves nothing about whose
    # device it is. Existing rows stay band scoped until re-enrolled.
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
    assert scope == {migrated: 0, paired: 0}


def test_moved_device_with_new_sponsor_loses_account_scope(accounts):  # noqa: F811
    from rook.remote.devices import DeviceStore
    member = new_user(accounts)
    owner = new_user(accounts, 'bob')
    band = accounts.enrollment.bands()[0]['id']
    accounts.assign(band, owner)
    accounts.accept_invite(member, accounts.invite(owner, band))
    devices = DeviceStore(accounts)
    _, csr = certificate_request()
    device = devices.enroll(band, member, csr, 'member phone', account_scope=True)['device_id']
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
