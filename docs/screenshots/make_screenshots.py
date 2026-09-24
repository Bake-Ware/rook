"""Regenerate the README screenshots in docs/img/ from the real dashboard code
and entirely fictional data (no real hosts, people, tokens or chat).

    .venv/bin/python docs/screenshots/make_screenshots.py [--out docs/img]

Serves rook/web/index.html and its modules from a throwaway aiohttp app backed
by real stores (ChatStore, KnowledgeService, Guidance) seeded with a mock
homelab, plus mock JSON for the worker roster and the secret vault, then drives
each view with Playwright.
"""
import asyncio
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
from aiohttp import web
from playwright.async_api import async_playwright

from rook.band_mcp.chat_rooms import ChatStore
from rook.band_mcp.guidance import Guidance
from rook.knowledge.service import KnowledgeService

WEB = ROOT / 'rook/web'
OP = 'user:operator'
ALEX = {'id': 'human:alex', 'kind': 'human', 'label': 'Alex'}
AGENT = {'id': 'claude.claudecode.laptop', 'kind': 'agent', 'label': 'claude', 'token': 'claude',
         'client': 'claudecode', 'host': 'laptop', 'dir': '/home/alex/homelab'}
CODEX = {'id': 'codex.codex.gpu-01', 'kind': 'agent', 'label': 'codex', 'token': 'codex',
         'client': 'codex', 'host': 'gpu-01'}
NOW = time.time()


# ---------------------------------------------------------------- workers
def worker(name, os_, caps, age=20, battery=None, version='112', build=112, **extra):
    info = {'linux': {'system': 'Linux', 'machine': 'x86_64', 'release': '6.12'},
            'arm': {'system': 'Linux', 'machine': 'aarch64', 'release': '6.6'},
            'windows': {'system': 'Windows', 'machine': 'AMD64', 'release': '11'},
            'android': {'system': 'Android', 'android_release': '15', 'manufacturer': 'Google', 'model': 'Pixel 8',
                        'screen': {'w': 1080, 'h': 2400, 'dpi': 420}},
            'tablet': {'system': 'Android', 'android_release': '14', 'manufacturer': 'Samsung', 'model': 'Galaxy Tab S9',
                       'screen': {'w': 1600, 'h': 2560, 'dpi': 274}},
            'chip': {}}[os_]
    hb = {'info': info}
    if battery:
        hb['battery'] = {'percent': battery[0], 'charging': battery[1]}
    return {'worker_id': uuid.uuid5(uuid.NAMESPACE_DNS, name).hex, 'name': name, 'description': extra.pop('description', ''),
            'band': 'a1b2c3d4', 'caps': ['caps.describe', 'info.host', 'info.uptime', 'info.ping', *caps],
            'plugins': [], 'version': version, 'build': build, 'app_release': extra.pop('app_release', {}),
            'hb': hb, 'last_seen_age_secs': age, **extra}


SHELL = ['shell.exec', 'shell.which', 'file.read', 'file.list', 'file.write', 'log.tail', 'worker.status', 'worker.restart']
WORKERS = [
    worker('build', 'linux', SHELL + ['proc.open', 'proc.list'], 12),
    worker('ci-runner', 'linux', SHELL, 31),
    worker('db-01', 'linux', SHELL, 8, description='postgres primary'),
    worker('dns', 'arm', SHELL, 44, description='pi-hole'),
    worker('edge-us', 'linux', SHELL, 19),
    worker('gpu-01', 'linux', SHELL + ['screenshot.capture', 'hid.type', 'agent.wake'], 5, description='2× RTX, local models'),
    worker('kvm-dongle', 'chip', ['kvm.type', 'kvm.key_combo', 'serial.write'], 27, version='0.6.9', build=0),
    worker('laptop', 'linux', SHELL + ['screenshot.capture', 'battery.status', 'camera.snap'], 3, battery=(82, True)),
    worker('media', 'linux', SHELL + ['deluge.list', 'deluge.status', 'deluge.add'], 16, description='jellyfin + downloads'),
    worker('nas', 'linux', SHELL + ['hermes.chat', 'hermes.status'], 22, description='storage, backups, hermes'),
    worker('office-pc', 'windows', SHELL + ['screenshot.capture', 'hid.type'], 38),
    worker('pixel-8', 'android', ['battery.status', 'notify.list', 'sms.list', 'location.get', 'device.info'], 9,
           battery=(64, False), version='0.4.7', app_release={'platform': 'android', 'version': '0.4.7', 'code': 47}),
    worker('tab-s9', 'tablet', ['battery.status', 'notify.list', 'device.info'], 24, battery=(23, False),
           version='0.4.7', app_release={'platform': 'android', 'version': '0.4.7', 'code': 47}),
]


# ---------------------------------------------------------------- chat
def seed_chat(chat):
    claude, hermes, codex = 'agent:claude', 'agent:hermes', 'agent:codex'
    def say(room, who, text, mention=()):
        chat.send(room, who, text, list(mention), bool(mention))
    r = chat.start('backup rotation', OP, [claude])['room']
    say(r, OP, '@claude can you check last night\'s offsite backup before I rotate the keys?', [claude])
    say(r, claude, 'On it. The nas job finished at 03:12, 214 GB, checksum verified against the offsite copy.')
    say(r, claude, 'One thing: the key in the vault (backup-s3-key) is 91 days old. Want me to rotate it now and update the job?')
    say(r, OP, 'yes, rotate it and leave a handoff on the task')
    say(r, claude, 'Done: new key stored in the vault, job re-run to test it (OK in 41 s), handoff saved on rotate-backup-keys.')
    r = chat.start('media box', OP, [hermes])['room']
    say(r, OP, 'is the new season finished downloading?', [hermes])
    say(r, hermes, '3 of 4 torrents complete; the last is at 87% and should finish in about ten minutes.')
    a = chat.start('GPU driver upgrade', codex, [claude])['room']
    say(a, codex, 'Driver 575 is staged on gpu-01. I need a reboot window; nothing is scheduled on it tonight.')
    say(a, claude, 'Agreed. I\'ll drain the model server at 23:00 and ping you when it\'s idle.')
    b = chat.start('Weekly hygiene sweep', hermes, [claude])['room']
    say(b, hermes, 'Two tasks have been idle 30+ min with unrecorded work: fix-dns-ttl and media-transcode-cache.')
    return r


PRESENCE = {'agents': [
    {'identity': 'agent:claude', 'online': True, 'last_seen_age_secs': 4},
    {'identity': 'agent:hermes', 'online': False, 'wake': 'hermes.chat', 'worker': 'nas', 'last_seen_age_secs': 900},
    {'identity': 'agent:codex', 'online': False, 'wake': 'agent.wake', 'worker': 'gpu-01', 'last_seen_age_secs': 5400},
    {'identity': 'agent:voice', 'online': False, 'last_seen_age_secs': 86400 * 2},
]}


# ---------------------------------------------------------------- knowledge + work
class Bands:
    def bands(self, active_only=False):
        return [{'id': 'b1', 'name': 'homelab', 'label': 'a1b2c3d4', 'is_primary': 1}]


async def seed_knowledge(s):
    async def do(action, kind=None, rid=None, actor=AGENT, **data):
        return await s.dispatch(action, 'b1' if not rid else None, kind, rid, data=data,
                                request_id=uuid.uuid4().hex, actor=actor)
    async def page(slug, title, body, parent=None, **attrs):
        return await do('create', 'knowledge', slug=slug, title=title, body=body,
                        **({'parent': parent} if parent else {}), attrs={'knowledge_kind': 'fact', **attrs})
    async def review(slug, verdict, note=''):
        r = await s.dispatch('get', None, None, slug)
        await s.dispatch('review', None, None, slug, data={'revision': r['revision'], 'verdict': verdict, 'note': note},
                         request_id=uuid.uuid4().hex, actor=ALEX)
    await page('hosts', 'Hosts', 'Every machine on the band: what it is, how to reach it, what runs on it.')
    await page('nas', 'nas (storage and backups)',
               '# Role\nZFS pool **tank** (4× 8 TB, raidz1). Nightly backup at 03:00 to the offsite bucket.\n\n'
               '- Hermes runs here and answers in chat\n- Backup key: vault `backup-s3-key` (see [[rook-vault-usage]])\n'
               '- Restore drill: [[restore-from-offsite]]', 'hosts', tags=['storage'])
    await page('gpu-01', 'gpu-01 (local models)',
               'Runs the local model server on port 8080. One 24 GB card. Drain it before driver upgrades.', 'hosts')
    await page('media', 'media (jellyfin + downloads)', 'Jellyfin on :8096, Deluge for downloads, transcode cache on /fast.', 'hosts')
    await page('dns', 'dns (pi-hole)', 'Pi-hole on a Raspberry Pi 4. Upstream: two DoH resolvers.', 'hosts')
    await page('runbooks', 'Runbooks', 'Step-by-step procedures agents and people can follow.')
    await page('restore-from-offsite', 'Restore from the offsite backup',
               '1. Stop the app writing to the dataset\n2. `rclone copy offsite:tank/<dataset> /tank/restore`\n'
               '3. Verify checksums, then swap datasets\n\nTested quarterly on [[nas]].', 'runbooks',
               knowledge_kind='procedure')
    await page('rotate-backup-keys', 'Rotate the backup key', 'Create the new key, store it in the vault, re-run the job, then revoke the old key.',
               'runbooks', knowledge_kind='procedure')
    await page('incidents', 'Incidents', 'What went wrong, why, and what changed afterwards.')
    await page('dns-outage-2026-08', 'DNS outage (Aug 2026)',
               'Pi-hole SD card filled up; every lookup on the LAN failed for 40 min. Fix: log rotation plus a disk alert. Host: [[dns]].',
               'incidents', knowledge_kind='summary')
    await page('security', 'Security & credentials', 'Where credentials live and the rules for using them.')
    await page('rook-vault-usage', 'Using secrets from the vault',
               'Put `{{secret:name}}` in rook_call args; the hub fills it in and masks it in the reply. Never paste values into pages or chat.',
               'security', knowledge_kind='procedure')
    for folder in ('hosts', 'runbooks', 'incidents', 'security'):
        await review(folder, 'verified')
    await review('nas', 'verified')
    await review('restore-from-offsite', 'verified')
    await review('dns-outage-2026-08', 'verified')
    await review('gpu-01', 'disputed', 'It has two 24 GB cards since the June upgrade, not one.')

    c = await do('create', 'concept', title='Homelab reliability', slug='homelab-reliability',
                 body='Nothing important should depend on one disk, one key or one person remembering.')
    p = await do('create', 'project', title='Offsite backups', slug='offsite-backups', parent=c['id'],
                 body='Every dataset on [[nas]] has a tested offsite copy.')
    t1 = await do('create', 'task', title='Rotate the backup key', slug='rotate-backup-keys-task', parent=p['id'],
                  body='Follow [[rotate-backup-keys]].', attrs={'criteria': ['New key in the vault', 'Job re-run OK']})
    await s.dispatch('claim', None, None, t1['id'], data={}, request_id=uuid.uuid4().hex, actor=AGENT)
    t2 = await do('create', 'task', title='Quarterly restore drill', slug='restore-drill', parent=p['id'],
                  body='Restore one dataset and verify it. See [[restore-from-offsite]].')
    await s.dispatch('link', None, None, t2['id'], data={'kind': 'journal', 'ref': 'c9c7a1e2', 'relation': 'evidence'},
                     request_id=uuid.uuid4().hex, actor=AGENT)
    cur = await s.dispatch('get', None, None, t2['id'])
    await s.dispatch('update', None, None, t2['id'], data={'revision': cur['revision'], 'patch': {
        'state': 'done', 'attrs': {'outcome': 'Restored tank/photos (38 GB) in 22 min; checksums match.'}}},
        request_id=uuid.uuid4().hex, actor=AGENT)
    t3 = await do('create', 'task', title='Back up the Pi-hole config', slug='backup-pihole', parent=p['id'])
    cur = await s.dispatch('get', None, None, t3['id'])
    await s.dispatch('update', None, None, t3['id'], data={'revision': cur['revision'], 'patch': {
        'state': 'blocked', 'attrs': {'blocked_reason': 'dns has no SSH key for the backup user yet'}}},
        request_id=uuid.uuid4().hex, actor=AGENT)
    await do('create', 'task', title='Alert when a backup is more than 26 h old', slug='backup-age-alert', parent=p['id'])
    p2 = await do('create', 'project', title='GPU driver upgrade', slug='gpu-driver-upgrade', parent=c['id'])
    t4 = await do('create', 'task', title='Drain the model server and reboot gpu-01', slug='drain-gpu-01', parent=p2['id'])
    await s.dispatch('claim', None, None, t4['id'], data={}, request_id=uuid.uuid4().hex, actor=CODEX)


# ---------------------------------------------------------------- vault (mock)
SECRETS = [
    ('backup-s3-key', 'Offsite bucket key for the nightly nas backup. Rotated by the rotate-backup-keys runbook.', 'human:alex', 0.2, 0.1),
    ('github-deploy-token', 'Fine-grained token for the homelab repo (contents: read).', 'human:alex', 40, 2),
    ('grafana-api-key', 'Editor key for dashboards on edge-us.', 'claude.claudecode.laptop', 12, 0.5),
    ('nas-admin-password', 'Web UI admin for the nas. Prefer the API token where possible.', 'human:alex', 90, 30),
    ('pihole-api-token', 'Pi-hole admin API token on dns.', 'human:alex', 20, 6),
    ('smtp-relay', 'App password for outgoing alerts.', 'human:alex', 60, 1),
]
VAULT = {'csrf': 'x', 'secrets': [
    {'name': n, 'description': d, 'set_by': by, 'created': NOW - age * 86400, 'updated': NOW - age * 86400,
     'last_used': NOW - used * 3600} for n, d, by, age, used in SECRETS],
    'access': [
        {'ts': NOW - 360, 'name': 'backup-s3-key', 'action': 'set', 'actor': 'claude.claudecode.laptop', 'via': 'rook_secret', 'task': 'rotate-backup-keys-task'},
        {'ts': NOW - 420, 'name': 'backup-s3-key', 'action': 'use', 'actor': 'claude.claudecode.laptop', 'via': '{{secret}}', 'task': 'rotate-backup-keys-task'},
        {'ts': NOW - 1800, 'name': 'grafana-api-key', 'action': 'use', 'actor': 'codex.codex.gpu-01', 'via': '{{secret}}', 'task': ''},
        {'ts': NOW - 21600, 'name': 'pihole-api-token', 'action': 'get', 'actor': 'human:alex', 'via': 'site', 'task': ''},
    ]}


async def main():
    out = Path(sys.argv[sys.argv.index('--out') + 1]) if '--out' in sys.argv else ROOT / 'docs/img'
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='rook-shots-'))
    chat = ChatStore(str(tmp / 'chat.db'))
    open_room = seed_chat(chat)
    svc = KnowledgeService(tmp / 'k.db', lambda: AGENT, Bands(), handoffs=lambda a, h: 'thread-1')
    await seed_knowledge(svc)
    guidance = Guidance(str(tmp / 'guidance.db'))

    async def index(r):
        return web.Response(text=(WEB / 'index.html').read_text(), content_type='text/html')
    async def asset(r):
        return web.FileResponse(WEB / r.match_info['name'])
    async def rooms(r): return web.json_response(chat.rooms_for(OP, limit=200, include_all=True))
    async def read(r): return web.json_response(chat.read(r.query['room'], OP, int(r.query.get('since', 0))))
    async def knowledge(r):
        if r.method == 'GET':
            return web.json_response({'csrf': 'x', 'bands': svc.bands(), 'actor': ALEX})
        d = await r.json()
        try:
            res = await svc.dispatch(d.get('action', 'list'), d.get('band'), d.get('kind'), d.get('id'), d.get('query', ''),
                                     d.get('data'), d.get('request_id'), actor=ALEX)
            return web.json_response({'ok': True, 'result': res})
        except (ValueError, KeyError, TypeError, PermissionError) as e:
            return web.json_response({'error': str(e)}, status=400)
    async def guide(r):
        return web.json_response({'csrf': 'x', 'editable': True, 'slots': guidance.slots(),
                                  'tools': ['rook_call', 'rook_knowledge', 'rook_secret', 'rook_task', 'rook_workers']})
    json_routes = {
        '/api/band/workers': WORKERS, '/api/bands': [{'id': 'a1b2c3d4', 'name': 'homelab'}],
        '/api/presence': PRESENCE, '/api/avatars': {}, '/account/vault/api': VAULT,
    }
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/account/{area}/assets/{name}', asset)
    app.router.add_get('/api/chat/rooms', rooms)
    app.router.add_get('/api/chat/read', read)
    app.router.add_route('*', '/account/knowledge/api', knowledge)
    app.router.add_route('*', '/account/guidance/api', guide)
    for path, body in json_routes.items():
        app.router.add_get(path, lambda r, b=body: web.json_response(b))
    app.router.add_route('*', '/{tail:.*}', lambda r: web.json_response({}, status=404))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=1.5)
        await ctx.add_init_script("localStorage.setItem('kn-open', JSON.stringify([]))")
        page = await ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        async def shot(name, full=False):
            await page.wait_for_timeout(600)
            await page.screenshot(path=str(out / f'{name}.png'), full_page=full)
            print('wrote', out / f'{name}.png')

        await page.goto(base + '#workers')
        await page.wait_for_selector('.item .n')
        await page.wait_for_timeout(1500)                     # heartbeat bars settle
        await shot('dashboard')

        await page.goto(base + '#chat')
        await page.wait_for_selector('#roomlist .roomrow')
        await page.click('#roomlist .roomrow:has-text("backup rotation")')
        await page.wait_for_selector('#chatlog .bubble:has-text("handoff saved")')
        await page.wait_for_timeout(3500)                     # next poll clears the unread badge
        await shot('chat-web')

        await page.goto(base + '#knowledge?p=nas&b=b1')
        await page.wait_for_selector('h1:has-text("nas (storage")')
        await page.click('.kn-tog[aria-label="Expand Runbooks"]')
        await shot('knowledge')

        await page.goto(base + '#knowledge?p=media&b=b1&r=1')
        await page.wait_for_selector('.kn-reviewbar')
        await shot('knowledge-review')

        await page.goto(base + '#work?p=offsite-backups&b=b1')
        await page.wait_for_selector('#view-work h1:has-text("Offsite backups")')
        await shot('work')

        await page.goto(base + '#vault')
        await page.wait_for_selector('#view-vault .kn-card')
        await shot('vault')

        await page.goto(base + '#guidance')
        await page.wait_for_selector('#view-guidance .gd-slot')
        await shot('guidance')

        mobile = await browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=2.5,
                                           is_mobile=True, has_touch=True)
        page = await mobile.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(base + '#workers')
        await page.wait_for_selector('.item .n')
        await page.wait_for_timeout(1500)
        await shot('dashboard-mobile')
        await browser.close()
    await runner.cleanup()
    if errors:
        print('page errors:\n' + '\n'.join(errors), file=sys.stderr)


asyncio.run(main())
