import io
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image

from rook.remote import setup_store
from rook.remote.accounts import AccountStore, GOOGLE
from rook.remote.enrollment import EnrollmentStore
from rook.remote.bootstrap import CombinedServer
from rook.remote.google_auth import GoogleAuth, avatar_url_ok, normalize_avatar


@pytest.fixture
def accounts(tmp_path,monkeypatch):
    monkeypatch.setenv('ROOK_SETUP_PATH',str(tmp_path/'setup.json'))
    monkeypatch.setenv('ROOK_ENROLLMENT_DB',str(tmp_path/'enrollment.db'))
    monkeypatch.setenv('ROOK_CHAT_DB',str(tmp_path/'chat.db'))
    monkeypatch.delenv('ROOK_GOOGLE_CLIENT_FILE',raising=False)
    setup_store.save({'band_name':'home','band_psk':'old-key','hub_public':'hub.example.com:443','pyz_domain':'rook.example.com'})
    e=EnrollmentStore(); e.import_config()
    return AccountStore(e)


def new_user(store,username='alice'):
    return store.create_local(username,'correct horse battery staple')


def test_bootstrap_not_claimed_by_first_google_login(accounts):
    outsider=accounts.google_identity({'sub':'outsider','email':'operator@example.com'})['user_id']
    admin=accounts.bootstrap('operator','legacy-short')
    assert accounts.user(admin)['admin']
    assert accounts.bands(admin)
    assert not accounts.user(outsider)['admin'] and not accounts.bands(outsider)
    assert accounts.bootstrap('operator','legacy-short')==admin
    assert accounts.login('operator','legacy-short')==admin


def test_local_sessions_password_and_limits(accounts):
    uid=new_user(accounts)
    assert accounts.login('ALICE','correct horse battery staple')==uid
    assert accounts.login('alice','wrong') is None
    assert accounts.login('unknown','wrong') is None
    token=accounts.new_session(uid)
    assert accounts.session(token)['id']==uid
    accounts.set_password(uid,'a different long password')
    assert accounts.session(token) is None
    assert accounts.login('alice','a different long password')==uid
    assert sum(accounts.rate_limit('peer') for _ in range(20))==10


def test_google_email_does_not_link_accounts(accounts):
    local=new_user(accounts)
    first=accounts.google_identity({'sub':'google1','email':'alice@example.com','email_verified':True})['user_id']
    second=accounts.google_identity({'sub':'google2','email':'alice@example.com','email_verified':True})['user_id']
    assert len({first,second,local})==3
    assert accounts.google_identity({'sub':'google1'})['user_id']==first
    result=accounts.google_identity({'sub':'google1'},local)
    assert result=={'merge_from':first,'merge_to':local}
    assert not accounts.user(local)['google_connected']


def test_invites_are_scoped_single_use_and_recheck_owner(accounts):
    owner=new_user(accounts); guest=new_user(accounts,'bob')
    band=accounts.enrollment.bands()[0]['id']; accounts.assign(band,owner)
    other=accounts.enrollment.register('other','other-key','hub.example.com:443')
    invitation=accounts.invite(owner,band)
    accounts.accept_invite(guest,invitation)
    assert [b['id'] for b in accounts.bands(guest,True)]==[band]
    with pytest.raises(ValueError): accounts.accept_invite(guest,invitation)
    with pytest.raises(PermissionError): accounts.invite(guest,band)
    with pytest.raises(PermissionError): accounts.require_band(guest,other['id'])
    with pytest.raises(ValueError): accounts.change_member(owner,band,owner,'remove')
    accounts.change_member(owner,band,guest,'owner')
    pending=accounts.invite(guest,band)
    accounts.change_member(owner,band,guest,'remove')
    with pytest.raises(PermissionError): accounts.accept_invite(guest,pending)
    assert accounts.bands(guest,True)==[]


def test_merge_preserves_ownership_and_avatar_and_revokes_sessions(accounts):
    local=new_user(accounts)
    google=accounts.google_identity({'sub':'google1'})['user_id']
    band=accounts.enrollment.bands()[0]['id'];accounts.assign(band,google)
    accounts.set_avatar(local,'custom',b'custom-picture')
    old=accounts.new_session(google)
    accounts.merge(google,local)
    assert accounts.user(google) is None
    assert accounts.session(old) is None
    assert accounts.bands(local)[0]['role']=='owner'
    assert accounts.user(local)['google_connected']
    assert accounts.avatar(local)[0]==b'custom-picture'
    accounts.unlink_google(local)
    assert not accounts.user(local)['google_connected']
    assert accounts.avatar(local)[0]==b'custom-picture'


def test_cannot_disconnect_only_login_and_google_avatar_cleared(accounts):
    uid=accounts.google_identity({'sub':'google'})['user_id']
    with pytest.raises(ValueError):accounts.unlink_google(uid)
    accounts.add_local(uid,'alice','correct horse battery staple')
    accounts.set_avatar(uid,'google',b'photo')
    accounts.unlink_google(uid)
    assert accounts.avatar(uid) is None


def test_grant_replay_and_expiry(accounts):
    grant=accounts.grant('oauth',{'nonce':'hello'})
    with pytest.raises(ValueError):accounts.consume(grant,'merge')
    assert accounts.consume(grant,'oauth')['nonce']=='hello'
    with pytest.raises(ValueError):accounts.consume(grant,'oauth')
    expired=accounts.grant('oauth',{},-1)
    with pytest.raises(ValueError):accounts.consume(expired,'oauth')


@pytest.mark.asyncio
async def test_google_jwt_validation(accounts):
    google=GoogleAuth(); google.config={'client_secret':'test'};google.client_id='client'
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    google.keys={'test':key.public_key()};google.keys_expire=time.time()+3600
    claims={'sub':'id','iss':GOOGLE,'aud':'client','iat':int(time.time()),'exp':int(time.time())+300,'nonce':'nonce'}
    def token(c):return jwt.encode(c,key,algorithm='RS256',headers={'kid':'test'})
    assert (await google.verify(token(claims),'nonce'))['sub']=='id'
    for overrides in [{'aud':'other'},{'iss':'https://attacker.test'},{'exp':0},{'nonce':'other'},{'azp':'other'}]:
        with pytest.raises(ValueError):await google.verify(token({**claims,**overrides}),'nonce')
    with pytest.raises(ValueError):await google.verify(jwt.encode(claims,'secret',algorithm='HS256',headers={'kid':'test'}),'nonce')


def test_avatar_normalization_and_url_limits():
    assert avatar_url_ok('https://lh3.googleusercontent.com/a/photo')
    for url in ['http://lh3.googleusercontent.com/a','https://localhost/a','https://127.0.0.1/a','https://lh3.googleusercontent.com.attacker.test/a','https://user@lh3.googleusercontent.com/a','https://lh3.googleusercontent.com:8443/a']:
        assert not avatar_url_ok(url)
    raw=io.BytesIO(); Image.new('RGB',(100,200)).save(raw,'PNG')
    image=Image.open(io.BytesIO(normalize_avatar(raw.getvalue())))
    assert image.size==(256,256) and image.format=='JPEG'
    for data in [b'<svg></svg>',b'x'*(2*1024*1024+1)]:
        with pytest.raises(ValueError):normalize_avatar(data)


@pytest.mark.asyncio
async def test_portal_isolation_csrf_and_custom_avatar(accounts):
    server=CombinedServer(band_psk='old-key',web_user='operator',web_pass='operator-password',domain='rook.example.com')
    store=server._accounts.store
    alice=new_user(store);bob=new_user(store,'bob')
    first=store.enrollment.register('alice band','alice-key','hub.example.com:443');store.assign(first['id'],alice)
    second=store.enrollment.register('bob band','bob-key','hub.example.com:443');store.assign(second['id'],bob)
    token=store.new_session(alice);user=store.session(token)
    headers={'Cookie':'rook_account='+token,'Origin':'https://rook.example.com'}
    async with TestClient(TestServer(server._app)) as client:
        response=await client.get('/account/configurations',headers=headers)
        payload=await response.json()
        assert [b['psk'] for b in payload['bands']]==['alice-key']
        response=await client.get('/api/bands',headers=headers)
        assert response.status==401
        response=await client.post('/account/action',headers=headers,data={'op':'invite','band_id':first['id']})
        assert response.status==403
        for op in ['invite','pair','rotate','revoke','member']:
            response=await client.post('/account/action',headers=headers,data={'op':op,'csrf':user['csrf'],'band_id':second['id'],'confirm':'yes'})
            assert response.status==403
        response=await client.get('/account/configurations?band='+second['id'],headers=headers)
        assert response.status in (403,404)
        store.set_avatar(alice,'custom',b'keep')
        server._accounts.google=type('Google',(),{'enabled':True})()
        server._accounts.store.picture=lambda uid:'https://lh3.googleusercontent.com/photo'
        await server._accounts.refresh_avatar(alice)
        assert store.avatar(alice)[0]==b'keep'


@pytest.mark.asyncio
async def test_oauth_callback_binding_and_link_conflict(accounts,monkeypatch):
    server=CombinedServer(band_psk='old-key',web_user='operator',web_pass='operator-password',domain='rook.example.com')
    google=server._accounts.google;google.config={'client_secret':'test'};google.client_id='client'
    google.exchange=AsyncMock(return_value={'sub':'new-google','name':'New user'})
    async with TestClient(TestServer(server._app)) as client:
        start=await client.get('/auth/google',allow_redirects=False)
        state=parse_qs(urlsplit(start.headers['Location']).query)['state'][0]
        binding=start.cookies['rook_oauth'].value
        bad=await client.get('/auth/google/callback?'+urlencode({'state':state,'code':'test'}))
        assert bad.status==400
        google.exchange.assert_not_awaited()
        start=await client.get('/auth/google',allow_redirects=False)
        state=parse_qs(urlsplit(start.headers['Location']).query)['state'][0];binding=start.cookies['rook_oauth'].value
        good=await client.get('/auth/google/callback?'+urlencode({'state':state,'code':'test'}),headers={'Cookie':'rook_oauth='+binding},allow_redirects=False)
        assert good.status==302 and 'rook_account' in good.cookies
        replay=await client.get('/auth/google/callback?'+urlencode({'state':state,'code':'test'}),headers={'Cookie':'rook_oauth='+binding})
        assert replay.status==400


from urllib.parse import urlencode


def test_headless_login_is_one_use_and_memberships_rechecked(accounts,monkeypatch):
    uid=new_user(accounts)
    band=accounts.enrollment.bands()[0]['id'];accounts.assign(band,uid)
    grant=accounts.device_login_start()
    assert len(grant['user_code'])==8
    assert accounts.device_login_poll(grant['device_code'])['error']=='authorization_pending'
    assert accounts.device_login_poll(grant['device_code'])['error']=='slow_down'
    accounts.device_login_approve(uid,grant['user_code'])
    with accounts.db() as db:db.execute('UPDATE device_logins SET last_poll=0')
    result=accounts.device_login_poll(grant['device_code'])
    assert result['bands'][0]['id']==band
    assert accounts.device_login_poll(grant['device_code'])['error']=='expired_token'


def certificate_request():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key=ec.generate_private_key(ec.SECP256R1())
    csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'test')])).sign(key,hashes.SHA256())
    return key,csr.public_bytes(serialization.Encoding.PEM).decode()


def device_proof(devices,enrolled,key,purpose='config'):
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    challenge=devices.challenge(enrolled['device_id'],purpose)
    return {'challenge':challenge['challenge'],'certificate':enrolled['certificate'],
            'signature':base64.b64encode(key.sign(challenge['message'].encode(),ec.ECDSA(hashes.SHA256()))).decode()}


def test_device_certificates_scoped_replay_revocation_and_renewal(accounts):
    from rook.remote.devices import DeviceStore
    uid=new_user(accounts);other=new_user(accounts,'bob')
    band=accounts.enrollment.bands()[0]['id'];accounts.assign(band,uid)
    devices=DeviceStore(accounts);key,csr=certificate_request()
    with pytest.raises(PermissionError):devices.enroll(band,other,csr,'bad')
    enrolled=devices.enroll(band,uid,csr,'laptop')
    proof=device_proof(devices,enrolled,key)
    assert devices.config(proof)['band']['id']==band
    with pytest.raises(ValueError):devices.config(proof)
    wrong,_=certificate_request()
    with pytest.raises(ValueError):devices.config(device_proof(devices,enrolled,wrong))
    renewed=devices.renew(device_proof(devices,enrolled,key,'renew'))
    assert renewed['certificate']!=enrolled['certificate']
    # Retry using a still-valid older certificate survives a lost renewal response.
    assert devices.config(device_proof(devices,enrolled,key))['band']['id']==band
    with pytest.raises(PermissionError):devices.revoke(other,enrolled['device_id'])
    devices.revoke(uid,enrolled['device_id'])
    with pytest.raises(PermissionError):devices.config(device_proof(devices,enrolled,key))
    with pytest.raises(ValueError):devices.enroll(band,uid,csr,'reuse revoked key')


def test_removed_member_device_cannot_revive_when_reinvited(accounts):
    from rook.remote.devices import DeviceStore
    owner=new_user(accounts);member=new_user(accounts,'bob')
    band=accounts.enrollment.bands()[0]['id'];accounts.assign(band,owner)
    accounts.accept_invite(member,accounts.invite(owner,band))
    devices=DeviceStore(accounts);key,csr=certificate_request();enrolled=devices.enroll(band,member,csr,'member laptop')
    accounts.change_member(owner,band,member,'remove')
    accounts.accept_invite(member,accounts.invite(owner,band))
    with pytest.raises(PermissionError):devices.config(device_proof(devices,enrolled,key))


def test_device_follows_explicit_account_merge_and_band_revoke_is_permanent(accounts):
    from rook.remote.devices import DeviceStore
    local=new_user(accounts);google=accounts.google_identity({'sub':'google'})['user_id']
    band=accounts.enrollment.bands()[0]['id'];accounts.assign(band,google)
    devices=DeviceStore(accounts);key,csr=certificate_request();enrolled=devices.enroll(band,google,csr,'laptop')
    accounts.merge(google,local)
    assert devices.config(device_proof(devices,enrolled,key))['band']['id']==band
    accounts.enrollment.revoke(band);accounts.enrollment.rotate(band)
    with pytest.raises(PermissionError):devices.config(device_proof(devices,enrolled,key))


def test_operator_password_rotation_invalidates_old_account_password(accounts):
    uid=accounts.bootstrap('operator','old-secret')
    session=accounts.new_session(uid)
    assert accounts.bootstrap('operator','new-secret')==uid
    assert accounts.login('operator','old-secret') is None
    assert accounts.login('operator','new-secret')==uid
    assert accounts.session(session) is None


@pytest.mark.asyncio
async def test_device_enrollment_http_and_public_apk_hash(accounts,tmp_path,monkeypatch):
    import hashlib
    import rook.remote.bootstrap as bootstrap
    server=CombinedServer(band_psk='old-key',web_user='operator',web_pass='operator-password',domain='rook.example.com')
    key,csr=certificate_request()
    band=server._enrollment.bands()[0]['id']
    grant=server._enrollment.issue(band)
    monkeypatch.setattr(bootstrap,'__file__',str(tmp_path/'bootstrap.py'))
    apk=tmp_path/'rook-worker.apk';apk.write_bytes(b'generic test artifact')
    monkeypatch.setenv('ROOK_PUBLIC_APK_SHA256',hashlib.sha256(apk.read_bytes()).hexdigest())
    async with TestClient(TestServer(server._app)) as client:
        response=await client.post('/auth/devices/enroll',json={'code':'xxxxxx','csr':csr})
        assert response.status==403
        response=await client.post('/auth/devices/enroll',json={'code':grant['code'],'csr':csr,'name':'test device'})
        assert response.status==200
        enrolled=await response.json()
        assert enrolled['band']['id']==band
        proof=device_proof(server._accounts.devices,enrolled,key)
        response=await client.post('/auth/devices/config',json=proof)
        assert response.status==200 and (await response.json())['band']['id']==band
        response=await client.get('/apk');assert response.status==200
        response=await client.get('/apk.json');assert response.status==503
        manifest={'package':'systems.bake.rook','version_code':5,'version_name':'0.4.1',
                  'sha256':hashlib.sha256(apk.read_bytes()).hexdigest(),'size':apk.stat().st_size}
        sidecar=tmp_path/'rook-worker-apk.json'
        sidecar.write_text(json.dumps(manifest))
        response=await client.get('/apk.json');assert response.status==200
        assert await response.json()==manifest
        assert response.headers['Cache-Control']=='no-store'
        sidecar.write_text(json.dumps({**manifest,'sha256':'0'*64}))
        response=await client.get('/apk.json');assert response.status==503
        sidecar.write_text(json.dumps(manifest))
        apk.write_bytes(b'old secret-bearing artifact')
        response=await client.get('/apk');assert response.status==503
        response=await client.get('/apk.json');assert response.status==503


@pytest.mark.asyncio
async def test_legacy_dashboard_login_bridges_to_worker_move_page(accounts):
    server=CombinedServer(band_psk='old-key',web_user='operator',web_pass='operator-password',domain='rook.example.com')
    async with TestClient(TestServer(server._app)) as client:
        path='/account/bands?worker=selected-worker'
        response=await client.get(path,headers={'Cookie':'rook_session='+server._make_session_cookie()},allow_redirects=False)
        assert response.status==302 and response.headers['Location']==path
        token=response.cookies['rook_account'].value
        response=await client.get(path,headers={'Cookie':'rook_account='+token},allow_redirects=False)
        assert response.status==302
        assert response.headers['Location']=='/#bands?worker=selected-worker'
        response=await client.get('/account/bands/component',headers={'Cookie':'rook_session='+server._make_session_cookie()},allow_redirects=False)
        assert response.status==302 and response.headers['Location']=='/account/bands/component'
        token=response.cookies['rook_account'].value
        response=await client.get('/account/bands/component',headers={'Cookie':'rook_account='+token})
        assert response.status==200 and 'band-dialog' in (await response.json())['html']


@pytest.mark.asyncio
async def test_legacy_login_can_open_tokens_first(accounts):
    server=CombinedServer(band_psk='old-key',web_user='operator',web_pass='operator-password',domain='rook.example.com')
    async with TestClient(TestServer(server._app)) as client:
        response=await client.get('/account/session',headers={'Cookie':'rook_session='+server._make_session_cookie()},allow_redirects=False)
        assert response.status==302 and response.headers['Location']=='/account/session'
        token=response.cookies['rook_account'].value
        response=await client.get('/account/session',headers={'Cookie':'rook_account='+token})
        assert response.status==200 and (await response.json())['user']['admin']
