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
