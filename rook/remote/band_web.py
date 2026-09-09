"""Account-scoped band management and resumable worker migration controls."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from aiohttp import web

from .account_web import NO_STORE


class BandWeb:
    def __init__(self, account):
        self.account = account
        self.store = account.store
        self.server = account.server
        self.migrations = account.devices.migrations
        self.lock = asyncio.Lock()

    def install(self, app):
        app.router.add_get('/account/bands', self.page)
        app.router.add_get('/account/bands/component', self.component)
        app.router.add_get('/account/bands/assets/{name}', self.asset)
        app.router.add_get('/account/bands/api', self.inventory)
        app.router.add_post('/account/bands/api', self.action)

    async def asset(self, request):
        name=request.match_info['name']
        types={'bands.js':'application/javascript','bands.css':'text/css','shell.css':'text/css','account.js':'application/javascript','tokens.js':'application/javascript','settings.css':'text/css','theme.css':'text/css','rook-art.css':'text/css','rook-scene.js':'application/javascript','rook-scene.js.LEGAL.txt':'text/plain'}
        if name not in types:raise web.HTTPNotFound()
        path=Path(__file__).parents[1]/'web'/name
        if name.startswith('rook-scene.'):
            return web.FileResponse(path, headers={'Cache-Control':'no-cache','Content-Type':types[name],'X-Content-Type-Options':'nosniff'})
        return web.Response(text=path.read_text(),content_type=types[name],headers=NO_STORE)

    async def component(self, request):
        user=self.account.require(request)
        return web.json_response({'html':Path(__file__).with_name('bands.html').read_text(),
                                  'csrf':user['csrf']},headers=NO_STORE)

    async def page(self, request):
        user = self.account.require(request)
        if user['admin']:
            from urllib.parse import urlencode
            suffix='?'+urlencode({'worker':request.query['worker']}) if request.query.get('worker') else ''
            raise web.HTTPFound('/#bands'+suffix)
        body = '<link rel="stylesheet" href="/account/bands/assets/bands.css">'
        body += '<div id="bands-root" class="bands-workspace">'+Path(__file__).with_name('bands.html').read_text()+'</div>'
        csrf=json.dumps(user['csrf']).replace('<','\\u003c')
        body += '<script type="module">import {mountBands} from "/account/bands/assets/bands.js";const ui=await mountBands(document.getElementById("bands-root"),{csrf:'+csrf+'});const wid=new URLSearchParams(location.search).get("worker");if(wid)ui.openWorker(wid);</script>'
        return self.account.response('Bands', body)

    def roster(self, label):
        if self.server._band is None:
            return []
        return [w for w in self.server._band.workers.values() if w.get('band') == label]

    def inventory_data(self, uid):
        bands = self.store.bands(uid)
        for band in bands:
            band.pop('psk_hash', None)
            band['workers'] = [
                {'id': w['worker_id'], 'name': w.get('name') or w['worker_id'],
                 'description': w.get('description', ''),
                 'online': time.time() - w.get('last_seen', 0) < 90,
                 'can_move': 'worker.enrollment_move_prepare' in w.get('caps', []),
                 'can_migrate_psk': all(c in w.get('caps', []) for c in
                     ('worker.enrollment_prepare', 'worker.enrollment_finish', 'worker.enrollment_prove'))}
                for w in self.roster(band['label'])]
            with self.store.db() as db:
                band['primary'] = bool(db.execute('SELECT is_primary FROM bands WHERE id=?', (band['id'],)).fetchone()[0])
                band['enrolled_devices'] = db.execute('SELECT count(*) FROM devices WHERE band_id=? AND active=1', (band['id'],)).fetchone()[0]
        with self.store.db() as db:
            rows = db.execute("SELECT m.id FROM band_migrations m JOIN memberships a ON a.band_id=m.band_id WHERE a.user_id=? AND a.role='owner' ORDER BY m.created DESC LIMIT 30", (uid,)).fetchall()
        migrations = []
        for row in rows:
            try:
                migrations.append(self.migrations.status(uid, row['id']))
            except (ValueError, PermissionError) as error:
                # Keep blocked jobs visible so they cannot disappear during recovery.
                with self.store.db() as db:
                    blocked=dict(db.execute('SELECT id,phase,band_id,target_band_id FROM band_migrations WHERE id=?',(row['id'],)).fetchone())
                migrations.append({**blocked, 'error': str(error), 'workers': []})
        return {'bands': bands, 'migrations': migrations}

    async def inventory(self, request):
        user = self.account.require(request)
        return web.json_response(self.inventory_data(user['id']), headers=NO_STORE)

    def source(self, uid, bid):
        self.store.require_band(uid, bid, True)
        band = next((b for b in self.store.bands(uid) if b['id'] == bid), None)
        if not band or not band['active']:
            raise ValueError('Choose an active source band that you own.')
        return band

    async def rpc(self, client, cap, wid, args=None, timeout=15):
        result = await client.call(cap=cap, target=wid, args=args or {}, timeout=timeout)
        if not result.get('ok') or result.get('from') != wid:
            raise ValueError('Worker did not acknowledge ' + cap + '. Refresh its status and retry.')
        payload = result.get('result')
        if isinstance(payload, dict) and payload.get('ok') is False:
            raise ValueError('Worker rejected ' + cap + '.')
        return payload

    def band_client(self, label):
        if self.server._band is None:
            raise ValueError('Band connection is unavailable.')
        client = next((c for c in self.server._band._clients if c.label == label), None)
        if client is None:
            raise ValueError('Band is reconnecting. Try again shortly.')
        return client

    async def prepare(self, uid, data):
        source = self.source(uid, str(data.get('band_id', '')))
        mode = data.get('mode')
        if mode not in ('move', 'move_all', 'psk'):
            raise ValueError('Choose a worker move or PSK migration.')
        roster = {w['worker_id']: w for w in self.roster(source['label'])}
        expected = data.get('workers')
        if mode == 'psk' and expected == [] and not roster:
            self.server._enrollment.rotate(source['id'], require_empty=True)
            return {'phase': 'complete', 'band_id': source['id']}
        if not isinstance(expected, list) or not expected or any(not isinstance(w, str) for w in expected) or len(set(expected)) != len(expected):
            raise ValueError('Review and select the expected workers first.')
        if mode == 'move' and len(expected) != 1:
            raise ValueError('Select one worker to move.')
        if mode != 'move' and set(expected) != set(roster):
            raise ValueError('The band roster changed. Reload and review all workers.')
        required = {'worker.enrollment_prepare', 'worker.enrollment_finish', 'worker.enrollment_prove'}
        if mode != 'psk':
            required.add('worker.enrollment_move_prepare')
        for wid in expected:
            worker = roster.get(wid)
            if not worker or time.time() - worker.get('last_seen', 0) >= 90:
                raise ValueError('Every selected worker must be online.')
            if self.server._ban_match(worker.get('name'), wid):
                raise ValueError('A selected worker is banned.')
            if not required.issubset(worker.get('caps', [])):
                raise ValueError('Update every selected worker to a compatible build first.')
        target = None
        if mode != 'psk':
            if data.get('new_band_name'):
                if not self.store.rate_limit(uid, 'band_create', 5):
                    raise ValueError('Please wait before creating another band.')
                target = self.store.create_band(uid, data['new_band_name'], self.server.hub_public)
            else:
                target = str(data.get('target_band_id', ''))
                self.store.require_band(uid, target)
            if target == source['id']:
                raise ValueError('Choose a different destination band.')
        client = self.band_client(source['label'])
        mapping = {}
        for wid in expected:
            cap = 'worker.enrollment_prepare' if mode == 'psk' else 'worker.enrollment_move_prepare'
            prepared = await self.rpc(client, cap, wid, {'server': self.account.origin})
            if not isinstance(prepared, dict) or prepared.get('worker_id') != wid:
                raise ValueError('Worker enrollment identity changed.')
            if prepared.get('enrolled'):
                finished = prepared
            else:
                csr = prepared.get('csr', '')
                csr_hash = hashlib.sha256(csr.encode()).hexdigest()
                if not csr or csr_hash != prepared.get('csr_hash'):
                    raise ValueError('Worker certificate request was inconsistent.')
                grant = self.store.grant('device_enroll', {'user_id': uid, 'band_id': source['id'], 'csr_hash': csr_hash}, 300)
                finished = await self.rpc(client, 'worker.enrollment_finish', wid, {'grant': grant})
            if finished.get('worker_id') != wid or finished.get('band_id') != source['id']:
                raise ValueError('Worker is enrolled in a different band or server.')
            mapping[wid] = finished['device_id']
        if mode != 'move':
            with self.store.db() as db:
                enrolled = {r[0] for r in db.execute('SELECT id FROM devices WHERE band_id=? AND active=1', (source['id'],))}
            if set(mapping.values()) != enrolled:
                raise ValueError('Some enrolled devices are offline or absent. Bring them online or explicitly revoke their enrollment before migrating all.')
        result = self.migrations.prepare(uid, source['id'], mapping, target_band_id=target, full_inventory=mode != 'move')
        await self.server._sync_enrollment()
        return result

    async def advance(self, uid, mid):
        status = self.migrations.status(uid, mid)
        if status['phase'] == 'prepared':
            if any(w['staged'] is None for w in status['workers']):
                return status
            self.migrations.activate(uid, mid)
            status = self.migrations.status(uid, mid)
        if status['phase'] == 'active':
            await self.server._sync_enrollment()
            with self.store.db() as db:
                label = db.execute('SELECT new_hash FROM band_migrations WHERE id=?', (mid,)).fetchone()[0][:8]
            client = self.band_client(label)
            # Verify on the destination channel, never the merged/old roster.
            for worker in [w for w in status['workers'] if w['confirmed'] is None][:8]:
                wid = worker['worker_id']
                if wid not in client.workers:
                    continue
                challenge = self.migrations.challenge(uid, mid, wid)
                try:
                    proof = await self.rpc(client, 'worker.enrollment_prove', wid, challenge, timeout=3)
                    self.migrations.confirm(uid, mid, wid, proof)
                except (ValueError, asyncio.TimeoutError):
                    continue
            status = self.migrations.status(uid, mid)
            if all(w['confirmed'] is not None for w in status['workers']):
                self.migrations.finalize(uid, mid)
                await self.server._sync_enrollment()
        return self.migrations.status(uid, mid)

    async def action(self, request):
        user = self.account.require(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            self.account.csrf(request, data, user)
            uid = user['id']
            async with self.lock:
                op = data.get('op')
                bid = str(data.get('band_id', ''))
                if op == 'create':
                    if not self.store.rate_limit(uid, 'band_create', 5):
                        raise ValueError('Please wait before creating another band.')
                    result = {'id': self.store.create_band(uid, data.get('name'), self.server.hub_public)}
                elif op == 'rename':
                    self.store.rename_band(uid, bid, data.get('name'))
                    result = {'ok': True}
                elif op == 'delete':
                    if data.get('confirm') is not True:
                        raise ValueError('Confirm band deletion first.')
                    self.store.delete_band(uid, bid)
                    result = {'ok': True}
                elif op == 'prepare':
                    if data.get('confirm') is not True:
                        raise ValueError('Review and confirm the migration first.')
                    result = await self.prepare(uid, data)
                elif op == 'advance':
                    result = await self.advance(uid, str(data.get('migration_id', '')))
                elif op == 'abort':
                    self.migrations.abort(uid, str(data.get('migration_id', '')))
                    result = {'ok': True}
                else:
                    raise ValueError('Unknown band action.')
                await self.server._sync_enrollment()
            return web.json_response(result, headers=NO_STORE)
        except PermissionError as error:
            return web.json_response({'error': str(error)}, status=403, headers=NO_STORE)
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({'error': str(error)}, status=400, headers=NO_STORE)
        except (ConnectionError, asyncio.TimeoutError):
            return web.json_response({'error': 'Worker connection timed out. Reload to check migration status before retrying.'}, status=503, headers=NO_STORE)
