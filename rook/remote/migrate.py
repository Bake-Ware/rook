"""Run an owner-approved routine migration from the server's protected state.

Requires a recorded completion report and an explicit expected-worker inventory.
Use a private stage directory; never place its contents in public artifacts.
This assumes the existing fleet is trusted. It is not compromise recovery.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

from .accounts import AccountStore
from .devices import DeviceStore
from .enrollment import EnrollmentStore
from .migration import MigrationStore
from ..band_mcp.client import BandClient


def report(event,**fields):
    print(json.dumps({'event':event,**fields}),flush=True)


async def call(client,cap,worker,args=None):
    result=await client.call(cap,args=args or {},target=worker,timeout=45)
    if not result.get('ok'):raise RuntimeError(cap+' failed for '+worker)
    payload=result.get('result',{})
    if isinstance(payload,dict) and payload.get('ok') is False:raise RuntimeError(cap+' was rejected for '+worker)
    return payload


async def run(args):
    expected=json.loads(Path(args.workers).read_text())
    if not isinstance(expected,list) or not expected:raise ValueError('Expected-worker inventory must be a nonempty array.')
    wanted={w['worker_id']:w['name'] for w in expected}
    if len(wanted)!=len(expected):raise ValueError('Worker identities must be unique.')
    gate=json.loads(Path(args.completion_report).read_text())
    if gate.get('decision')!='completion received; rollout allowed when ready' or not gate.get('notifications'):
        raise ValueError('A reviewed completion report is required.')
    enrollment=EnrollmentStore();accounts=AccountStore(enrollment);devices=DeviceStore(accounts)
    migrations=MigrationStore(accounts)
    with accounts.db() as db:
        owner=args.owner or db.execute("SELECT value FROM account_settings WHERE key='bootstrap_user'").fetchone()['value']
    accounts.require_band(owner,args.band,True)
    band=next(b for b in enrollment.bands(secrets_visible=True) if b['id']==args.band)
    client=BandClient(band['psk'],args.hub_host,args.hub_port,use_ws=args.ws)
    await client.start()
    try:
        if not args.resume:
            end=time.monotonic()+60
            while not set(wanted).issubset(client.workers):
                if time.monotonic()>end:raise RuntimeError('Expected workers are missing; no migration was started.')
                await asyncio.sleep(1)
            for wid,name in wanted.items():
                if client.workers[wid]['name']!=name:raise ValueError('Worker name/identity changed; refresh the inventory.')
                caps=client.workers[wid].get('caps',[])
                if not all(c in caps for c in ('worker.enrollment_prepare','worker.enrollment_finish','worker.enrollment_prove')):
                    raise ValueError('Worker '+name+' needs the compatible build first.')
        if not args.execute:
            report('preflight_passed',band=band['name'],expected=len(wanted));return
        if args.resume:
            mid=args.resume
            status=migrations.status(owner,mid)
            if {w['worker_id'] for w in status['workers']}!=set(wanted):raise ValueError('Resume inventory differs from recorded migration.')
        else:
            mapping={}
            for wid,name in wanted.items():
                prepared=await call(client,'worker.enrollment_prepare',wid,{'server':args.server})
                if prepared.get('enrolled'):
                    finished=prepared
                else:
                    if prepared['worker_id']!=wid or hashlib.sha256(prepared['csr'].encode()).hexdigest()!=prepared['csr_hash']:
                        raise ValueError('Worker CSR response was inconsistent.')
                    grant=accounts.grant('device_enroll',{'user_id':owner,'band_id':args.band,'csr_hash':prepared['csr_hash']},300)
                    finished=await call(client,'worker.enrollment_finish',wid,{'grant':grant})
                if finished['band_id']!=args.band or finished['worker_id']!=wid:raise ValueError('Enrolled device identity/band mismatch.')
                mapping[wid]=finished['device_id']
                report('device_enrolled',worker=name,device=finished['device_id'])
            mid=migrations.prepare(owner,args.band,mapping,ttl=args.window)['id']
            report('migration_prepared',migration_id=mid,expected=len(wanted))
        status=migrations.status(owner,mid)
        if status['phase']=='prepared':
            while any(w['staged'] is None for w in status['workers']):
                if time.time()>status['deadline']:raise RuntimeError('Staging window expired; old band remains active. Review and abort or recover explicitly.')
                report('waiting_for_staging',migration_id=mid,staged=sum(w['staged'] is not None for w in status['workers']),expected=len(wanted))
                await asyncio.sleep(10)
                status=migrations.status(owner,mid)
            migrations.activate(owner,mid)
            report('migration_activated',migration_id=mid)
        elif status['phase']=='complete':
            report('already_complete',migration_id=mid);return
        elif status['phase']!='active':raise ValueError('Migration cannot be resumed in this state.')
        with accounts.db() as db:
            pending=db.execute('SELECT new_psk FROM band_migrations WHERE id=?',(mid,)).fetchone()['new_psk']
        await client.stop()
        client=BandClient(pending,args.hub_host,args.hub_port,use_ws=args.ws);await client.start()
        deadline=migrations.status(owner,mid)['deadline']
        confirmed={w['worker_id'] for w in migrations.status(owner,mid)['workers'] if w['confirmed'] is not None}
        while confirmed!=set(wanted):
            if time.time()>deadline:raise RuntimeError('Verification window expired; both authorized bands remain available for forward recovery. Old key was not retired.')
            for wid in set(wanted)-confirmed:
                if wid not in client.workers:continue
                try:
                    challenge=migrations.challenge(owner,mid,wid)
                    proof=await call(client,'worker.enrollment_prove',wid,challenge)
                    migrations.confirm(owner,mid,wid,proof)
                    if await call(client,'info.ping',wid)!='pong':raise RuntimeError('Unexpected ping response.')
                    confirmed.add(wid)
                    report('new_channel_verified',migration_id=mid,worker=wanted[wid],verified=len(confirmed),expected=len(wanted))
                except (asyncio.TimeoutError,ValueError,RuntimeError):
                    report('verification_retry',worker=wanted[wid])
            if confirmed!=set(wanted):await asyncio.sleep(5)
        migrations.finalize(owner,mid)
        report('migration_complete',migration_id=mid,expected=len(wanted),epoch=band['epoch']+1)
    finally:
        await client.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--band',required=True)
    parser.add_argument('--workers',required=True)
    parser.add_argument('--completion-report',required=True)
    parser.add_argument('--server',default='https://rook.bakeforge.com')
    parser.add_argument('--owner')
    parser.add_argument('--hub-host',default='127.0.0.1')
    parser.add_argument('--hub-port',type=int,default=7474)
    parser.add_argument('--ws',action='store_true')
    parser.add_argument('--window',type=int,default=3600)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--resume')
    args=parser.parse_args()
    try:asyncio.run(run(args))
    except Exception as error:
        report('migration_stopped',error=str(error));raise SystemExit(1)


if __name__=='__main__':main()
