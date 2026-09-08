"""Per-device certificates for authenticated HTTPS configuration retrieval.

This controls enrollment/configuration access. Legacy PSK mesh traffic is still
legacy traffic; issuing a certificate does not enforce peer identity there.
"""
import base64
import datetime as dt
import hashlib
import json
import secrets
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID

from .accounts import digest

UTC=dt.timezone.utc


class DeviceStore:
    def __init__(self,accounts):
        self.accounts=accounts
        with accounts.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS device_ca (
                    id INTEGER PRIMARY KEY CHECK(id=1), private_key BLOB NOT NULL, certificate BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, band_id TEXT NOT NULL REFERENCES bands(id),
                    sponsor TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                    public_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL, last_seen REAL, credential_epoch INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(band_id,public_hash));
                CREATE TABLE IF NOT EXISTS device_certificates (
                    fingerprint TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                    pem BLOB NOT NULL, expires REAL NOT NULL);
            ''')
            row=db.execute('SELECT * FROM device_ca WHERE id=1').fetchone()
            if row:
                self.key=serialization.load_pem_private_key(row['private_key'],password=None)
                self.cert=x509.load_pem_x509_certificate(row['certificate'])
            else:
                self.key=ec.generate_private_key(ec.SECP256R1())
                subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Rook device enrollment CA')])
                now=dt.datetime.now(UTC)
                self.cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now-dt.timedelta(minutes=5)).not_valid_after(now+dt.timedelta(days=3650))
                    .add_extension(x509.BasicConstraints(ca=True,path_length=0),True)
                    .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),True)
                    .sign(self.key,hashes.SHA256()))
                db.execute('INSERT INTO device_ca VALUES(1,?,?)',(self.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()),self.ca_pem()))

    def ca_pem(self):
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def issue(self,db,device_id,public_key):
        now=dt.datetime.now(UTC)
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,device_id)]))
            .issuer_name(self.cert.subject).public_key(public_key).serial_number(x509.random_serial_number())
            .not_valid_before(now-dt.timedelta(minutes=5)).not_valid_after(now+dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),False)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier('urn:rook:device:'+device_id)]),False)
            .sign(self.key,hashes.SHA256()))
        pem=cert.public_bytes(serialization.Encoding.PEM)
        db.execute('DELETE FROM device_certificates WHERE expires<?',(time.time(),))
        db.execute('INSERT INTO device_certificates VALUES(?,?,?,?)',(cert.fingerprint(hashes.SHA256()).hex(),device_id,pem,cert.not_valid_after_utc.timestamp()))
        return pem.decode()

    def enroll(self,band_id,sponsor,csr_pem,name):
        if not isinstance(csr_pem,str) or len(csr_pem)>16000:raise ValueError('Invalid certificate request.')
        csr=x509.load_pem_x509_csr(csr_pem.encode())
        key=csr.public_key()
        if not csr.is_signature_valid or not isinstance(key,ec.EllipticCurvePublicKey) or not isinstance(key.curve,ec.SECP256R1):
            raise ValueError('Use a signed P-256 certificate request.')
        public_hash=hashlib.sha256(key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
        with self.accounts.db() as db:
            if sponsor is None:
                row=db.execute("SELECT user_id FROM memberships WHERE band_id=? AND role='owner' ORDER BY user_id LIMIT 1",(band_id,)).fetchone()
                if not row:raise ValueError('A band owner must configure device enrollment first.')
                sponsor=row['user_id']
            self.accounts.require_band(sponsor,band_id,False,db)
            band=db.execute('SELECT * FROM bands WHERE id=? AND active=1',(band_id,)).fetchone()
            if not band:raise ValueError('Band revoked.')
            if db.execute('SELECT 1 FROM devices WHERE band_id=? AND public_hash=?',(band_id,public_hash)).fetchone():
                raise ValueError('This device key is already registered. Use renewal or generate a new key.')
            device_id=secrets.token_hex(16)
            db.execute('INSERT INTO devices(id,band_id,sponsor,name,public_hash,created,credential_epoch) VALUES(?,?,?,?,?,?,?)',
                       (device_id,band_id,sponsor,str(name or 'worker')[:100],public_hash,time.time(),band['epoch']))
            cert=self.issue(db,device_id,key)
            self.accounts.audit(db,sponsor,'device_enroll',device_id)
            return {'device_id':device_id,'certificate':cert,'ca_certificate':self.ca_pem().decode(),
                    'band':{'id':band_id,'name':band['name'],'psk':band['psk'],'hub':band['hub'],'epoch':band['epoch']}}

    def challenge(self,device_id,purpose):
        if purpose not in ('config','renew'):raise ValueError('Invalid device operation.')
        nonce=secrets.token_urlsafe(32)
        grant=self.accounts.grant('device_proof',{'device_id':device_id,'purpose':purpose,'nonce':nonce},60)
        message='rook-device-v1\n'+device_id+'\n'+purpose+'\n'+nonce
        return {'challenge':grant,'message':message}

    def authenticate(self,challenge,certificate,signature,purpose):
        if not isinstance(certificate,str) or len(certificate)>16000 or not isinstance(signature,str) or len(signature)>1024:
            raise ValueError('Invalid device proof.')
        proof=self.accounts.consume(challenge,'device_proof')
        if proof['purpose']!=purpose:raise ValueError('Device operation mismatch.')
        cert=x509.load_pem_x509_certificate(certificate.encode())
        message='rook-device-v1\n'+proof['device_id']+'\n'+purpose+'\n'+proof['nonce']
        try:
            cert.public_key().verify(base64.b64decode(signature,validate=True),message.encode(),ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature,TypeError):
            raise ValueError('Invalid device signature.') from None
        with self.accounts.db() as db:
            device=db.execute('SELECT d.* FROM devices d JOIN device_certificates c ON c.device_id=d.id JOIN bands b ON b.id=d.band_id JOIN memberships m ON m.band_id=d.band_id AND m.user_id=d.sponsor WHERE c.fingerprint=? AND c.expires>? AND d.id=? AND d.active=1 AND b.active=1',
                              (cert.fingerprint(hashes.SHA256()).hex(),time.time(),proof['device_id'])).fetchone()
            if not device:raise PermissionError('Device certificate expired or authorization revoked.')
            db.execute('UPDATE devices SET last_seen=? WHERE id=?',(time.time(),device['id']))
            return dict(device),cert.public_key()

    def config(self,proof):
        device,key=self.authenticate(proof.get('challenge',''),proof.get('certificate',''),proof.get('signature',''),'config')
        with self.accounts.db() as db:
            # Recheck authorization in the transaction which reads the secret.
            self.accounts.require_band(device['sponsor'],device['band_id'],False,db)
            band=db.execute('SELECT id,name,psk,hub,epoch FROM bands WHERE id=? AND active=1 AND EXISTS(SELECT 1 FROM devices WHERE id=? AND active=1)',(device['band_id'],device['id'])).fetchone()
            if not band:raise PermissionError('Device or band revoked.')
            db.execute('UPDATE devices SET credential_epoch=? WHERE id=?',(band['epoch'],device['id']))
            return {'band':dict(band),'device_id':device['id']}

    def renew(self,proof):
        device,key=self.authenticate(proof.get('challenge',''),proof.get('certificate',''),proof.get('signature',''),'renew')
        with self.accounts.db() as db:
            self.accounts.require_band(device['sponsor'],device['band_id'],False,db)
            if not db.execute('SELECT 1 FROM devices d JOIN bands b ON b.id=d.band_id WHERE d.id=? AND d.active=1 AND b.active=1',(device['id'],)).fetchone():
                raise PermissionError('Device or band revoked.')
            return {'certificate':self.issue(db,device['id'],key),'ca_certificate':self.ca_pem().decode()}

    def list(self,uid,band_id):
        self.accounts.require_band(uid,band_id,True)
        with self.accounts.db() as db:
            return [dict(r) for r in db.execute('SELECT id,name,active,created,last_seen,credential_epoch FROM devices WHERE band_id=?',(band_id,))]

    def revoke(self,uid,device_id):
        with self.accounts.db() as db:
            row=db.execute('SELECT band_id FROM devices WHERE id=?',(device_id,)).fetchone()
            if not row:raise ValueError('Device not found.')
            self.accounts.require_band(uid,row['band_id'],True,db)
            db.execute('UPDATE devices SET active=0 WHERE id=?',(device_id,))
            self.accounts.audit(db,uid,'device_revoke',device_id)
