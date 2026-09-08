"""Durable routine PSK migration; no credential travels through a band RPC.

Owners stage a replacement, devices persist it through authenticated HTTPS,
then the controller verifies each expected device on the replacement band before
retiring the old key. A missed deadline never silently excludes a device.
This is compatibility migration, not certificate enforcement on peer traffic.
"""
import base64
import hashlib
import json
import secrets
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec,ed25519

from .psk import generate_psk

SCHEMA = '''
CREATE TABLE IF NOT EXISTS band_migrations (
 id TEXT PRIMARY KEY, band_id TEXT NOT NULL, old_epoch INTEGER NOT NULL,
 new_psk TEXT, new_hash TEXT NOT NULL, phase TEXT NOT NULL,
 created REAL NOT NULL, deadline REAL NOT NULL, owner TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_migration ON band_migrations(band_id)
 WHERE phase IN ('prepared','active');
CREATE TABLE IF NOT EXISTS migration_workers (
 migration_id TEXT NOT NULL, worker_id TEXT NOT NULL, device_id TEXT NOT NULL,
 staged REAL, confirmed REAL, PRIMARY KEY(migration_id,worker_id),
 UNIQUE(migration_id,device_id));
CREATE TABLE IF NOT EXISTS migration_proofs (
 hash TEXT PRIMARY KEY, migration_id TEXT NOT NULL, worker_id TEXT NOT NULL,
 expires REAL NOT NULL);
'''


def proof_message(migration_id,device_id,worker_id,epoch,nonce):
    return '\n'.join(('rook-migration-proof-v1',migration_id,device_id,worker_id,str(epoch),nonce)).encode()


class MigrationStore:
    def __init__(self,accounts):
        self.accounts=accounts
        with accounts.db() as db:db.executescript(SCHEMA)

    def prepare(self,uid,band_id,expected,ttl=3600):
        if not isinstance(expected,dict) or not expected or len(expected)>1000:
            raise ValueError('Specify the complete worker-to-device mapping.')
        if not 300<=ttl<=86400:raise ValueError('Use a 5-minute to 24-hour migration window.')
        with self.accounts.db() as db:
            self.accounts.require_band(uid,band_id,True,db)
            band=db.execute('SELECT * FROM bands WHERE id=? AND active=1',(band_id,)).fetchone()
            if not band:raise ValueError('Active band required.')
            if db.execute("SELECT 1 FROM band_migrations WHERE band_id=? AND phase IN ('prepared','active')",(band_id,)).fetchone():
                raise ValueError('A migration is already open.')
            for worker,device in expected.items():
                if not isinstance(worker,str) or not worker or len(worker)>128:raise ValueError('Invalid worker identity.')
                if not db.execute('SELECT 1 FROM devices d JOIN memberships m ON m.user_id=d.sponsor AND m.band_id=d.band_id WHERE d.id=? AND d.band_id=? AND d.active=1',(device,band_id)).fetchone():
                    raise ValueError('Every expected worker needs a currently authorized device identity.')
            if len(set(expected.values()))!=len(expected):raise ValueError('Device identities must be unique.')
            mid=secrets.token_hex(16);psk=generate_psk();now=time.time()
            db.execute('INSERT INTO band_migrations VALUES(?,?,?,?,?,?,?,?,?)',
                       (mid,band_id,band['epoch'],psk,hashlib.sha256(psk.encode()).hexdigest(),'prepared',now,now+ttl,uid))
            db.executemany('INSERT INTO migration_workers(migration_id,worker_id,device_id) VALUES(?,?,?)',[(mid,w,d) for w,d in expected.items()])
            self.accounts.audit(db,uid,'migration_prepare',mid)
        return self.status(uid,mid)

    def _owned(self,db,uid,mid):
        row=db.execute('SELECT * FROM band_migrations WHERE id=?',(mid,)).fetchone()
        if not row:raise ValueError('Migration not found.')
        self.accounts.require_band(uid,row['band_id'],True,db)
        band=db.execute('SELECT * FROM bands WHERE id=?',(row['band_id'],)).fetchone()
        if row['phase'] in ('prepared','active') and (not band['active'] or band['epoch']!=row['old_epoch']):
            raise ValueError('Band credentials changed; this migration is no longer valid.')
        return row

    def status(self,uid,mid):
        with self.accounts.db() as db:
            row=dict(self._owned(db,uid,mid));row.pop('new_psk');row.pop('new_hash')
            row['workers']=[dict(r) for r in db.execute('SELECT worker_id,device_id,staged,confirmed FROM migration_workers WHERE migration_id=?',(mid,))]
            row['overdue']=row['deadline']<time.time()
            return row

    def activate(self,uid,mid):
        with self.accounts.db() as db:
            row=self._owned(db,uid,mid)
            if row['phase']=='active':return
            if row['phase']!='prepared' or row['deadline']<time.time():raise ValueError('Migration is not ready or its window expired.')
            if db.execute('SELECT 1 FROM migration_workers WHERE migration_id=? AND staged IS NULL',(mid,)).fetchone():
                raise ValueError('Every expected device must persist the candidate configuration first.')
            db.execute("UPDATE band_migrations SET phase='active' WHERE id=?",(mid,))
            self.accounts.audit(db,uid,'migration_activate',mid)

    def candidate(self,db,device_id):
        row=db.execute("SELECT m.*,b.name,b.hub FROM band_migrations m JOIN migration_workers w ON w.migration_id=m.id JOIN bands b ON b.id=m.band_id WHERE w.device_id=? AND m.phase IN ('prepared','active') AND b.active=1 AND b.epoch=m.old_epoch",(device_id,)).fetchone()
        if not row:return None
        return {'id':row['id'],'phase':row['phase'],'deadline':row['deadline'],
                'band':{'id':row['band_id'],'name':row['name'],'hub':row['hub'],'psk':row['new_psk'],'epoch':row['old_epoch']+1}}

    def staged(self,device,mid):
        with self.accounts.db() as db:
            self.accounts.require_band(device['sponsor'],device['band_id'],False,db)
            row=db.execute("SELECT 1 FROM band_migrations m JOIN devices d ON d.band_id=m.band_id JOIN bands b ON b.id=m.band_id WHERE m.id=? AND m.phase='prepared' AND d.id=? AND d.active=1 AND b.active=1 AND b.epoch=m.old_epoch",(mid,device['id'])).fetchone()
            if not row:raise ValueError('Migration or device is no longer authorized.')
            result=db.execute('UPDATE migration_workers SET staged=COALESCE(staged,?) WHERE migration_id=? AND device_id=?',(time.time(),mid,device['id']))
            if result.rowcount!=1:raise PermissionError('Device is not expected in this migration.')
        return {'ok':True}

    def challenge(self,uid,mid,worker_id):
        with self.accounts.db() as db:
            row=self._owned(db,uid,mid)
            if row['phase']!='active':raise ValueError('Activate before verifying the new channel.')
            if not db.execute('SELECT 1 FROM migration_workers WHERE migration_id=? AND worker_id=?',(mid,worker_id)).fetchone():raise ValueError('Unexpected worker.')
            token=secrets.token_urlsafe(32)
            db.execute('DELETE FROM migration_proofs WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO migration_proofs VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),mid,worker_id,time.time()+60))
            return {'migration_id':mid,'epoch':row['old_epoch']+1,'nonce':token}

    def confirm(self,uid,mid,worker_id,proof):
        from cryptography.exceptions import InvalidSignature
        with self.accounts.db() as db:
            migration=self._owned(db,uid,mid)
            if migration['phase']!='active':raise ValueError('Migration is not active.')
            nonce=str(proof.get('nonce',''));digest=hashlib.sha256(nonce.encode()).hexdigest()
            challenge=db.execute('SELECT 1 FROM migration_proofs WHERE hash=? AND migration_id=? AND worker_id=? AND expires>?',(digest,mid,worker_id,time.time())).fetchone()
            if not challenge:raise ValueError('Verification challenge expired or already used.')
            cert=x509.load_pem_x509_certificate(proof['certificate'].encode())
            device=db.execute('SELECT d.* FROM migration_workers w JOIN devices d ON d.id=w.device_id JOIN device_certificates c ON c.device_id=d.id JOIN memberships m ON m.user_id=d.sponsor AND m.band_id=d.band_id WHERE w.migration_id=? AND w.worker_id=? AND d.active=1 AND c.fingerprint=? AND c.expires>?',
                              (mid,worker_id,cert.fingerprint(hashes.SHA256()).hex(),time.time())).fetchone()
            if not device:raise PermissionError('Proof does not identify the expected authorized device.')
            message=proof_message(mid,device['id'],worker_id,migration['old_epoch']+1,nonce)
            signature=base64.b64decode(proof['signature'],validate=True)
            try:
                key=cert.public_key()
                if isinstance(key,ed25519.Ed25519PublicKey):key.verify(signature,message)
                else:key.verify(signature,message,ec.ECDSA(hashes.SHA256()))
            except InvalidSignature:raise ValueError('Invalid migration proof.') from None
            db.execute('DELETE FROM migration_proofs WHERE hash=?',(digest,))
            db.execute('UPDATE migration_workers SET confirmed=? WHERE migration_id=? AND worker_id=?',(time.time(),mid,worker_id))
            self.accounts.audit(db,uid,'migration_confirm',worker_id)

    def finalize(self,uid,mid):
        with self.accounts.db() as db:
            row=self._owned(db,uid,mid)
            if row['phase']=='complete':return
            if row['phase']!='active':raise ValueError('Migration is not active.')
            if db.execute('SELECT 1 FROM migration_workers WHERE migration_id=? AND confirmed IS NULL',(mid,)).fetchone():
                raise ValueError('Every expected device must prove the new channel before retirement.')
            if db.execute('SELECT 1 FROM migration_workers w JOIN devices d ON d.id=w.device_id WHERE w.migration_id=? AND (d.active=0 OR NOT EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=d.sponsor AND m.band_id=d.band_id))',(mid,)).fetchone():
                raise ValueError('Device authorization changed; review before finalizing.')
            old=db.execute('SELECT psk_hash FROM bands WHERE id=?',(row['band_id'],)).fetchone()['psk_hash']
            db.execute('INSERT OR IGNORE INTO retired VALUES(?)',(old,))
            db.execute('UPDATE bands SET psk=?,psk_hash=?,epoch=epoch+1 WHERE id=?',(row['new_psk'],row['new_hash'],row['band_id']))
            db.execute('DELETE FROM pairing WHERE band_id=?',(row['band_id'],))
            db.execute("UPDATE band_migrations SET phase='complete',new_psk=NULL WHERE id=?",(mid,))
            self.accounts.audit(db,uid,'migration_complete',mid)

    def abort(self,uid,mid):
        with self.accounts.db() as db:
            row=self._owned(db,uid,mid)
            if row['phase']!='prepared':raise ValueError('An activated migration requires forward recovery; it cannot silently roll devices back.')
            db.execute('INSERT OR IGNORE INTO retired VALUES(?)',(row['new_hash'],))
            db.execute("UPDATE band_migrations SET phase='aborted',new_psk=NULL WHERE id=?",(mid,))
            self.accounts.audit(db,uid,'migration_abort',mid)
