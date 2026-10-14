import datetime
import json
import os
import urllib.error

import pytest

from rook.worker import enroll


def test_private_configuration_storage(tmp_path,monkeypatch):
    path=tmp_path/'private'/'enrollment.json';monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(path))
    enroll.save({'active_band':'band','bands':[]})
    assert enroll.load()['active_band']=='band'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_worker_enrollment_rejects_insecure_origins():
    for url in ['http://rook.example.com','https://user:password@rook.example.com','https://rook.example.com/path','https://rook.example.com?token=x']:
        with pytest.raises(ValueError):enroll.post(url,'/enroll',{})


def test_pairing_fetch_issues_a_unique_private_device_key(tmp_path,monkeypatch):
    path=tmp_path/'enrollment.json';monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(path))
    calls=[]
    def post(server,endpoint,data):
        calls.append((endpoint,data))
        assert endpoint=='/auth/devices/enroll'
        assert 'PRIVATE KEY' not in data['csr']
        assert 'CERTIFICATE REQUEST' in data['csr']
        return {'device_id':'device','certificate':'certificate','ca_certificate':'ca',
                'band':{'id':'band','name':'test','psk':'new-key','hub':'hub.example.com:443','epoch':2}}
    monkeypatch.setattr(enroll,'post',post)
    enroll.enroll('https://rook.example.com','abc123')
    saved=enroll.load()
    assert saved['device']['device_id']=='device'
    assert 'PRIVATE KEY' in saved['device']['private_key']
    assert saved['bands'][0]['epoch']==2
    assert calls[0][1]['code']=='abc123'


def test_known_revocation_never_uses_cached_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(tmp_path/'enrollment.json'))
    enroll.save({'server':'https://rook.example.com','device':{'device_id':'device'},'last_verified':9999999999,'bands':[]})
    def denied(*args,**kwargs):raise urllib.error.HTTPError('https://rook.example.com',403,'denied',{},None)
    monkeypatch.setattr(enroll,'proof',denied)
    with pytest.raises(ValueError,match='denied'):enroll.refresh()


def test_mobile_device_key_uses_standard_pkcs10_and_pkcs8():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from rook.worker.device_key import certificate_request, private_pem, sign
    key,csr=certificate_request()
    request=x509.load_pem_x509_csr(csr.encode())
    assert request.is_signature_valid
    pem=private_pem(key)
    loaded=serialization.load_pem_private_key(pem.encode(),None)
    assert loaded.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)==bytes(key.verify_key)
    request.public_key().verify(sign(pem,b'test proof'),b'test proof')


def test_cross_band_refresh_requires_explicit_active_move(tmp_path,monkeypatch):
    monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(tmp_path/'enrollment.json'))
    old={'id':'source','epoch':8,'psk':'old','hub':'hub'}
    new={'id':'target','epoch':1,'psk':'new','hub':'hub'}
    saved={'server':'https://rook.example.com','active_band':'source','bands':[old],
           'device':{'device_id':'device','expires_at':9999999999}}
    enroll.save(saved)
    response={'band':new,'migration':None}
    monkeypatch.setattr(enroll,'proof',lambda *args:{})
    monkeypatch.setattr(enroll,'post',lambda *args:response)
    with pytest.raises(ValueError,match='different band'):enroll.refresh()
    response['migration']={'id':'move','kind':'move','phase':'prepared','source_band_id':'source','band':new}
    with pytest.raises(ValueError):enroll.refresh()
    response['migration']['phase']='active'
    actual=enroll.refresh()
    assert actual['active_band']=='target' and actual['bands']==[new]
    # Restart while the source still holds enrollment; repeated refresh is valid.
    assert enroll.refresh()['active_band']=='target'
    response['migration']=None
    assert enroll.refresh()['active_band']=='target'


# -- the enrollment a person sees (numbered steps, no prompts without a terminal) ----

def _pairing_post(server,endpoint,data):
    return {'device_id':'device','certificate':'certificate','ca_certificate':'ca',
            'band':{'id':'band','name':'home','psk':'new-key','hub':'hub.example.com:443','epoch':2}}


def test_pairing_prints_numbered_steps_and_done(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(tmp_path/'enrollment.json'))
    monkeypatch.delenv('ROOK_INSTALLER',raising=False)
    monkeypatch.setattr(enroll,'post',_pairing_post)
    assert enroll.enroll('https://rook.example.com','ABC123',name='den')=={'worker':'den','band':'home'}
    out=capsys.readouterr().out.splitlines()
    assert out[0]=='Joining a band with pairing code abc123'
    assert [l.split('.')[0].strip() for l in out[1:4]]==['1','2','3']
    assert out[-1]=='Done: den joined home'
    assert not any('--enrolled' in l or 'Saved configurations' in l for l in out)


def test_pairing_inside_the_installer_prints_nothing(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(tmp_path/'enrollment.json'))
    monkeypatch.setenv('ROOK_INSTALLER','1')
    monkeypatch.setattr(enroll,'post',_pairing_post)
    enroll.enroll('https://rook.example.com','abc123',name='den')
    assert capsys.readouterr().out==''
    assert enroll.active_band_name()=='home'


def _account_post(bands):
    def post(server,endpoint,data):
        if endpoint=='/auth/device/start':
            return {'verification_uri_complete':'https://rook.example.com/account?device=WXYZ',
                    'user_code':'WXYZ','device_code':'dc','expires_in':60,'interval':0}
        if endpoint=='/auth/device/poll':
            return {'bands':bands,'enrollment_grants':{b['id']:'grant-'+b['id'] for b in bands}}
        assert endpoint=='/auth/devices/enroll'
        return {'device_id':'device','certificate':'c','ca_certificate':'ca','band':{},'grant':data['enrollment_grant']}
    return post


def test_account_enrollment_never_prompts_without_a_terminal(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('ROOK_ENROLLMENT_FILE',str(tmp_path/'enrollment.json'))
    monkeypatch.delenv('ROOK_BAND',raising=False)
    monkeypatch.setattr(enroll.time,'sleep',lambda s:None)
    monkeypatch.setattr(enroll.webbrowser,'open',lambda url:None)
    monkeypatch.setattr('builtins.input',lambda *a:pytest.fail('prompted without a terminal'))
    monkeypatch.setattr(enroll,'interactive',lambda:False)
    bands=[{'id':'b1','name':'home'},{'id':'b2','name':'lab'}]
    monkeypatch.setattr(enroll,'post',_account_post(bands))
    with pytest.raises(ValueError) as error:
        enroll.enroll('https://rook.example.com',name='den')
    assert '--band NAME' in str(error.value) and 'home, lab' in str(error.value)
    out=capsys.readouterr().out
    assert 'https://rook.example.com/account?device=WXYZ' in out and 'WXYZ' in out
    monkeypatch.setenv('ROOK_BAND','lab')
    assert enroll.enroll('https://rook.example.com',name='den')['band']=='lab'
    assert enroll.load()['active_band']=='b2' and enroll.load()['device']['grant']=='grant-b2'
    with pytest.raises(ValueError,match='No band called'):
        enroll.enroll('https://rook.example.com',band='work')


def test_band_prompt_only_at_a_terminal(monkeypatch,capsys):
    bands=[{'id':'b1','name':'home'},{'id':'b2','name':'lab'}]
    assert enroll.choose_band(bands[:1])==bands[0]
    monkeypatch.setattr('builtins.input',lambda *a:'2')
    assert enroll.choose_band(bands,ask=enroll.Steps(nested=False))==bands[1]
    monkeypatch.setenv('ROOK_YES','1')
    assert enroll.interactive() is False


def test_enrollment_errors_in_plain_words():
    import io
    refused=urllib.error.HTTPError('https://x',403,'Forbidden',{},io.BytesIO(b''))
    assert 'fresh code' in enroll.plain_error(refused)
    gone=urllib.error.URLError(ConnectionRefusedError('refused'))
    assert 'could not reach the hub' in enroll.plain_error(gone)
