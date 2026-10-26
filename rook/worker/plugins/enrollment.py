"""Owner-approved device enrollment and proof for staged PSK migration.

Band RPCs carry public CSRs, a CSR-bound one-use grant and public proofs only.
The private key stays local; configuration is fetched separately over HTTPS.
This bootstraps routine upgrades of trusted existing workers, not recovery of a
compromised mesh. Compromise recovery requires independent owner enrollment.
"""
import asyncio
import base64
import hashlib
import json
import time

from ..plugin import Plugin,capability
from .. import enroll
from ..device_key import certificate_request,private_pem,sign


class EnrollmentPlugin(Plugin):
    NAMESPACE='worker'

    def bind_worker(self,worker):
        self.worker=worker

    @capability('enrollment_prepare', risk='admin', scope="multi-user")
    def prepare(self,server):
        """Start device enrollment with the HTTPS enrollment ``server``: make a device key and CSR."""
        enroll.check_origin(server,'Use the HTTPS origin of the enrollment server.')
        saved=enroll.load()
        if saved.get('device'):
            return self.status()
        path=enroll.storage_path().with_name('enrollment-request.json')
        if path.exists():
            pending=json.loads(path.read_text())
            if pending['server']!=server.rstrip('/'):
                raise ValueError('A different enrollment is already pending.')
        else:
            key,csr=certificate_request()
            pending={'server':server.rstrip('/'),'csr':csr,'private_key':private_pem(key)}
            enroll.save(pending,path)
        return {'worker_id':self.worker.worker_id,'name':self.worker.name,'csr':pending['csr'],
                'csr_hash':hashlib.sha256(pending['csr'].encode()).hexdigest(),'enrolled':False}

    @capability('enrollment_move_prepare', risk='admin', scope="multi-user")
    def move_prepare(self,server):
        """Advertise support for authenticated cross-band configuration changes."""
        return self.prepare(server)

    @capability('enrollment_finish', risk='admin', scope="multi-user")
    async def finish(self,grant):
        """Finish enrollment with the server's one-time ``grant``: store the device certificate and bands."""
        return await asyncio.to_thread(self._finish,grant)

    def _finish(self,grant):
        if enroll.load().get('device'):return self.status()
        path=enroll.storage_path().with_name('enrollment-request.json')
        pending=json.loads(path.read_text())
        issued=enroll.post(pending['server'],'/auth/devices/enroll',{'enrollment_grant':grant,'csr':pending['csr'],'name':self.worker.name})
        if 'band' in issued:raise ValueError('A routine upgrade requires a CSR-bound enrollment grant.')
        issued['private_key']=pending['private_key']
        saved={'server':pending['server'],'device':issued,'auto_start':True,'bands':[],
               'active_band':issued['band_id'],'last_verified':0}
        result=enroll.post(saved['server'],'/auth/devices/config',enroll.proof(saved,'config'))
        if result['band']['id']!=saved['active_band']:raise ValueError('Unexpected band.')
        saved['bands']=[result['band']];saved['last_verified']=time.time()
        enroll.save(saved)
        path.unlink()
        return self.status()

    @capability('enrollment_status', risk='read', scope="multi-user")
    def status(self):
        """Whether this worker is enrolled, with its device id, active band and migration state."""
        saved=enroll.load()
        if not saved.get('device'):return {'enrolled':False,'worker_id':self.worker.worker_id}
        from telesthete.protocol.crypto import derive_band_id
        band=next(b for b in saved['bands'] if b['id']==saved['active_band'])
        migration=saved.get('migration') or {}
        return {'enrolled':True,'worker_id':self.worker.worker_id,'device_id':saved['device']['device_id'],
                'band_id':saved['active_band'],'epoch':band['epoch'],'migration_id':migration.get('id'),
                'migration_phase':migration.get('phase'),
                'on_saved_band':derive_band_id(band['psk'])==self.worker.transport.band_id}

    @capability('enrollment_prove', risk='admin', scope="multi-user")
    def prove(self,migration_id,epoch,nonce):
        """Sign a band-migration proof (``migration_id``, ``epoch``, ``nonce``) with the device key."""
        if not isinstance(nonce,str) or not 32<=len(nonce)<=128:raise ValueError('Invalid nonce.')
        saved=enroll.load();status=self.status()
        if not status.get('on_saved_band') or status['epoch']!=epoch or status['migration_id']!=migration_id or status['migration_phase']!='active':
            raise ValueError('Worker is not running on the activated migration band.')
        device=saved['device']
        message='\n'.join(('rook-migration-proof-v1',migration_id,device['device_id'],self.worker.worker_id,str(epoch),nonce)).encode()
        return {'nonce':nonce,'certificate':device['certificate'],
                'signature':base64.b64encode(sign(device['private_key'],message)).decode()}


PLUGIN=EnrollmentPlugin
