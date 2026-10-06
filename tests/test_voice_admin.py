import hashlib
import pytest
pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.voice.admin import AdminStore, router


@pytest.fixture
def setup(tmp_path):
    store = AdminStore(tmp_path / 'admin.sqlite3')
    store.bootstrap('admin', 'test-password')
    legacy = {hashlib.sha256(b'old-key').hexdigest(): {'principal': 'Alex', 'owner': True}}
    store.import_legacy(legacy)
    invalidated = []
    async def invalidate(key):
        invalidated.append(key)
    app = FastAPI()
    app.include_router(router(store, legacy, invalidate))
    with TestClient(app, base_url='https://voice.test') as client:
        yield store, legacy, invalidated, client


def login(client):
    r = client.post('/admin/api/login', json={'username': 'admin', 'password': 'test-password'},
                    headers={'X-Voice-Admin': '1', 'Origin': 'https://voice.test'})
    assert r.status_code == 200
    assert all(flag in r.headers['set-cookie'].lower() for flag in ['secure', 'httponly', 'samesite=strict'])
    return {'X-Voice-Admin': '1', 'X-CSRF-Token': r.json()['csrf']}


def test_login_csrf_and_no_credentials_leaked(setup):
    store, legacy, invalidated, c = setup
    assert c.get('/admin/api/keys').status_code == 401
    assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'test-password'}).status_code == 403
    assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'test-password'}, headers={'X-Voice-Admin': '1', 'Origin': 'https://evil.test'}).status_code == 403
    assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'wrong'}, headers={'X-Voice-Admin': '1'}).status_code == 401
    headers = login(c)
    assert c.post('/admin/api/keys', json={'label': 'x', 'principal': 'x'}, headers={'X-Voice-Admin': '1'}).status_code == 403
    assert c.get('/admin/api/keys').headers['cache-control'] == 'no-store'
    with store.db() as db:
        row = db.execute('SELECT * FROM users').fetchone()
    assert 'test-password' not in row
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert 'old-key' not in c.get('/admin/api/keys').text


def test_create_edit_revoke_live_mapping(setup):
    store, legacy, invalidated, c = setup
    headers = login(c)
    r = c.post('/admin/api/keys', json={'label': 'Phone', 'principal': 'Sam', 'worker': 'Samphone'}, headers=headers)
    assert r.status_code == 200
    token, key = r.json()['token'], r.json()['id']
    assert store.mappings(legacy)[key]['worker'] == 'Samphone'
    assert token not in c.get('/admin/api/keys').text
    assert c.put('/admin/api/keys/'+key, json={'label': 'Owner', 'principal': 'Alex', 'owner': True}, headers=headers).status_code == 200
    assert store.mappings(legacy)[key]['owner'] is True
    assert invalidated == [key]
    assert c.delete('/admin/api/keys/'+key, headers=headers).status_code == 200
    assert key not in store.mappings(legacy)
    old = next(iter(legacy))
    assert c.delete('/admin/api/keys/'+old, headers=headers).status_code == 200
    store.import_legacy(legacy)
    assert old not in store.mappings(legacy)
    assert c.put('/admin/api/keys/'+key, json={'label': 'Revoked', 'principal': 'Alex', 'owner': True}, headers=headers).status_code == 404


def test_password_change_and_logout_invalidate_sessions(setup):
    store, legacy, invalidated, c = setup
    headers = login(c)
    assert c.post('/admin/api/password', json={'current': 'wrong', 'password': 'new'}, headers=headers).status_code == 403
    assert c.post('/admin/api/password', json={'current': 'test-password', 'password': 'new'}, headers=headers).status_code == 200
    assert c.get('/admin/api/session').status_code == 401
    assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'test-password'}, headers={'X-Voice-Admin': '1'}).status_code == 401
    r = c.post('/admin/api/login', json={'username': 'admin', 'password': 'new'}, headers={'X-Voice-Admin': '1'})
    assert r.status_code == 200
    assert c.post('/admin/api/logout', headers={'X-Voice-Admin': '1', 'X-CSRF-Token': r.json()['csrf']}).status_code == 200
    assert c.get('/admin/api/session').status_code == 401


def test_login_throttled_and_bootstrap_preserves_password(setup):
    store, legacy, invalidated, c = setup
    store.bootstrap('admin', 'different')
    login(c)
    for _ in range(9):
        assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'wrong'}, headers={'X-Voice-Admin': '1'}).status_code == 401
    assert c.post('/admin/api/login', json={'username': 'admin', 'password': 'wrong'}, headers={'X-Voice-Admin': '1'}).status_code == 429
