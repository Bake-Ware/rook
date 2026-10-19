"""Drive the dashboard's Huddles view, Manage > Roles and the Memory page's
role filter in a real browser, against the real huddle and roles plugins
(their dashboard API through the same proxy path), a real ChatStore and a
fake model endpoint for the chair.

    .venv/bin/python tests/browser_huddles.py [--shots DIR]

Covers: the Huddles tab (old #chat links land there); role and chair chips;
the per-role "answering" line while a round runs; an action card inline under
its message, approved; @role completion; a new huddle with the default roles
checked; a 1:1 with the chair; archive; the old chat rooms, read-only, under
the filter (a room whose title has quotes, and an agent-only room); the phone
layout (list, then thread, back); the Roles page (edit, add from presets);
the Memory page's role filter.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
import httpx
from aiohttp import web
from playwright.async_api import async_playwright
from starlette.applications import Starlette

import rook.hub.plugins.settings as settings_plugin
from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.plugins.huddle.web import routes as huddle_routes
from rook.hub.settings_store import SettingsStore

OP = 'user:operator'


class Vault:
    def get(self, name, actor, via='get', task=None): return 'sk-test-not-a-real-key'
    def list(self): return [{'name': 'llm-key'}]


class Accounts:
    def session(self, token):
        return {'id': '1', 'username': 'op', 'admin': True, 'csrf': 'x'} if token == 's' else None


async def llm(method, url, body, headers, timeout):
    if url.endswith('/models'):
        return 200, {'data': [{'id': 'small-model'}]}
    return 200, {'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': 'Hello from the chair.'}}]}


async def main():
    shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
    settings_plugin._admin_gate = lambda what: None
    tmp = Path(tempfile.mkdtemp(prefix='rook-huddles-'))
    chat = ChatStore(str(tmp / 'chat.db'))
    node = HubNode(str(tmp), entry_points=False, vault=Vault(), chat=chat,
                   settings_store=SettingsStore(tmp / 'settings.db'))
    node.journal = SimpleNamespace(record=lambda **kw: None)
    node.plugin('home')._http = llm
    for k, v in {'enabled': True, 'base_url': 'http://llm.example:1234/v1', 'model': 'small-model',
                 'api_key': '{{secret:llm-key}}'}.items():
        node.settings.set(f'home.{k}', v, actor='human:op')
    node.settings.set('huddle.default_roles', ['qa'], actor='human:op')
    roles = node.plugin('role')
    roles.store.save({'id': 'qa', 'name': 'QA engineer', 'charter': 'Watches quality.',
                      'tools': {'allow': ['info.*']}}, 'human:op')
    roles.store.save({'id': 'pm', 'name': 'Project manager', 'charter': 'Keeps the deck.'}, 'human:op')
    svc = node.plugin('huddle').service

    # A standup with a round in flight and a card waiting.
    team = (await svc.start(OP, 'Standup', ['qa', 'pm']))['room']
    ask = svc._send(team, OP, "What's on deck?")
    svc._send(team, 'agent:home', '@qa: Anything failing?\n@pm: What is next?', quiet=True)
    qa = svc._send(team, 'role:qa', 'Two flaky jobs.')
    svc.store.round_add('r_1', team, ask['seq'], OP, {})
    svc.store.round_set('r_1', state='roles', replies=[{'role': 'qa', 'state': 'done'},
                                                       {'role': 'pm', 'state': 'running'}])
    svc.store.card_add({'id': 'c_1', 'room': team, 'round': 'r_1', 'seq': qa['seq'], 'kind': 'job.run',
                        'by': 'role:qa', 'summary': 'Check the hub status',
                        'call': {'worker': 'rook', 'cap': 'home.status', 'args': {}},
                        'state': 'proposed'})
    # Old chat rooms: one with quotes in its title, one only agents are in.
    quoted = chat.start('Knowledge (349e3eb) - remove "gate", it\'s audit only', OP, ['agent:claude code'])['room']
    chat.send(quoted, 'agent:claude code', 'from the old days', [], False)
    agents = chat.start('agents only', 'agent:Claude web', ['agent:claude code'])['room']
    chat.send(agents, 'agent:claude code', 'agent-to-agent note', [], False)

    mcp = httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=huddle_routes(lambda: node, Accounts()))),
                            base_url='http://mcp.test', cookies={'rook_account': 's'})
    async def huddle_api(r):
        res = await mcp.request(r.method, '/huddle/account-api', content=await r.read(),
                                headers={'Content-Type': 'application/json'})
        return web.Response(body=res.content, status=res.status_code, content_type='application/json')
    async def rooms(r): return web.json_response(chat.rooms_for(OP, limit=200, include_all=True))
    async def read(r): return web.json_response(chat.read(r.query['room'], OP, int(r.query.get('since', 0))))
    async def index(r): return web.Response(text=(ROOT / 'rook/web/index.html').read_text(), content_type='text/html')
    async def asset(r):
        path = ROOT / 'rook/web' / r.match_info['name']
        return web.FileResponse(path) if path.is_file() else web.Response(status=404)
    async def other(r): return web.json_response({})

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_route('*', '/account/huddle/api', huddle_api)
    for prefix in ('huddle', 'bands', 'settings', 'knowledge', 'ui'):
        app.router.add_get(f'/account/{prefix}/assets/{{name}}', asset)
    app.router.add_get('/theme.css', lambda r: web.FileResponse(ROOT / 'rook/web/theme.css'))
    app.router.add_get('/shell.css', lambda r: web.FileResponse(ROOT / 'rook/web/shell.css'))
    app.router.add_get('/api/chat/rooms', rooms)
    app.router.add_get('/api/chat/read', read)
    app.router.add_route('*', '/{tail:.*}', other)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    errors = []

    async def shot(page, label):
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shots / f'huddles-{label}.png'))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))

        # An old #chat link lands on Huddles; the newest chaired huddle opens.
        await page.goto(base + '#chat')
        await page.wait_for_selector('#tab-huddles.active')
        assert await page.locator('#tab-chat').count() == 0
        await page.wait_for_selector('.hx-item.on:has-text("Standup")')
        await page.wait_for_selector('.hx-msg .hx-chip.hx-role:has-text("QA engineer")')
        assert await page.locator('.hx-head .hx-chip.hx-chair').inner_text() == 'home · chair'
        assert await page.locator('.hx-msg.route').count() == 1
        # The round is running: one "answering" line, for the role still at it.
        await page.wait_for_selector('.hx-wait:has-text("Project manager")')
        assert await page.locator('.hx-wait').count() == 1
        # The card sits under QA's message; the list shows it waiting.
        card = page.locator('.hx-msg:has-text("Two flaky jobs.") + .hx-card')
        assert 'rook · home.status' in await card.inner_text()
        assert await page.locator('.hx-item.on .hx-badge').inner_text() == '1'
        await shot(page, 'round')
        await card.locator('button:has-text("Approve")').click()
        await page.wait_for_selector('.hx-card.hx-c-done:has-text("Done")')
        assert svc.store.card('c_1')['decided_by'] == 'human:1'
        svc._send(team, 'role:pm', 'The deck is clear.')
        svc.store.round_set('r_1', state='done', ended=time.time())
        await page.wait_for_selector('.hx-msg:has-text("The deck is clear.")', timeout=5000)
        await page.wait_for_selector('.hx-wait', state='detached')

        # @ completes the huddle's roles.
        await page.fill('.hx-compose textarea', '')
        await page.type('.hx-compose textarea', 'hey @p')
        await page.wait_for_selector('.hx-pop li.on:has-text("@pm")')
        await page.press('.hx-compose textarea', 'Enter')
        assert await page.input_value('.hx-compose textarea') == 'hey @pm '
        await page.fill('.hx-compose textarea', '')

        # A new huddle: the default roles are checked.
        await page.click('button:has-text("New huddle")')
        await page.wait_for_selector('.hx-form')
        assert await page.is_checked('.hx-check input[value=qa]')
        assert not await page.is_checked('.hx-check input[value=pm]')
        await page.uncheck('.hx-check input[value=qa]')
        await page.fill('.hx-form input:not([type])', 'Just us')
        await page.click('.hx-form button:has-text("Start")')
        await page.wait_for_selector('.hx-head h2:has-text("Just us")')
        one = next(x for x in svc.huddles() if x['title'] == 'Just us')
        assert one['roles'] == []
        # Shift+Enter is a new line, Enter sends; the chair answers.
        await page.type('.hx-compose textarea', 'hello')
        await page.keyboard.down('Shift'); await page.keyboard.press('Enter'); await page.keyboard.up('Shift')
        await page.type('.hx-compose textarea', 'there')
        await page.press('.hx-compose textarea', 'Enter')
        await page.wait_for_selector('.hx-msg.me .hx-text:has-text("there")')
        assert [m['text'] for m in chat.tail(one['room'], 10) if m['sender'] == OP] == ['hello\nthere']
        await page.wait_for_selector('.hx-msg .hx-text:has-text("Hello from the chair.")', timeout=10000)
        await shot(page, 'one-to-one')

        # Archive the standup: off the list, under Archived, read-only.
        await page.click('.hx-item:has-text("Standup")')
        await page.click('.hx-acts button:has-text("Archive")')
        await page.wait_for_selector('.hx-acts button:has-text("Unarchive")')
        assert await page.is_disabled('.hx-compose textarea')
        await page.wait_for_selector('.hx-list .hx-item:has-text("Standup")', state='detached')
        await page.select_option('.hx-side-head select', 'archived')
        await page.wait_for_selector('.hx-item:has-text("Standup")')

        # Old chat rooms, read-only, under the filter.
        await page.select_option('.hx-side-head select', 'chat')
        await page.wait_for_selector('.hx-item:has-text("agents only")')
        assert await page.locator('.hx-item').count() == 2
        await page.click('.hx-item:has-text("audit only")')
        await page.wait_for_selector('.hx-msg .hx-text:has-text("from the old days")')
        assert 'audit only' in await page.inner_text('.hx-head h2')
        assert await page.locator('.hx-compose').count() == 0
        await page.click('.hx-item:has-text("agents only")')
        await page.wait_for_selector('.hx-msg .hx-text:has-text("agent-to-agent note")')
        assert OP not in chat.read(agents, None)['participants']     # reading joins nobody
        await shot(page, 'old-room')
        # A deep link to an old room (#chat?room=...) opens it the same way.
        await page.goto(base + '#chat?room=' + agents)
        await page.wait_for_selector('.hx-head h2:has-text("agents only")')
        assert page.url.endswith('#huddles?room=' + agents)

        # Manage > Roles: edit a role, add one from the presets.
        await page.goto(base + '#roles')
        await page.wait_for_selector('#view-roles .mg-row:has-text("QA engineer")')
        await page.click('#view-roles .mg-row:has-text("QA engineer")')
        await page.wait_for_selector('#view-roles .mg-panel-head h2:has-text("QA engineer")')
        name = page.locator('#view-roles .mg-side label:has-text("Name") input')
        await name.fill('QA lead')
        await page.click('#view-roles .mg-side button:has-text("Save")')
        await page.wait_for_selector('#view-roles .mg-status:has-text("revision 2")')
        assert roles.store.get('qa')['name'] == 'QA lead'
        assert roles.store.history('qa')[0]['actor'] == 'human:op'
        await page.click('#view-roles button:has-text("Add from presets")')
        await page.click('.mg-menu button:has-text("Lead developer")')
        await page.click('#view-roles .mg-side button:has-text("Create")')
        await page.wait_for_selector('#view-roles .mg-row:has-text("Lead developer")')
        assert roles.store.get('lead')['tools']['allow'][0] == 'sessions.list'
        assert await page.get_attribute('#view-roles a:has-text("What it remembers")', 'href') == '#memory?role=lead'
        await shot(page, 'roles')

        # Memory page, role filter: a role without a home yet says so.
        await page.click('#view-roles a:has-text("What it remembers")')
        await page.wait_for_selector('#view-memory select[aria-label="Whose memory"]')
        assert await page.input_value('#view-memory select[aria-label="Whose memory"]') == 'lead'
        await page.wait_for_selector('#view-memory .mg-banner:has-text("no home yet")')
        await page.close()

        # Phone: the list, then the thread (no list), then back.
        phone = await browser.new_page(viewport={'width': 390, 'height': 844})
        phone.on('pageerror', lambda e: errors.append('phone: ' + str(e)))
        await phone.goto(base + '#huddles')
        await phone.wait_for_selector('.hx-item:has-text("Just us")')
        assert await phone.locator('.hx-main').is_hidden()
        await phone.click('.hx-item:has-text("Just us")')
        await phone.wait_for_selector('.hx-msg:has-text("Hello from the chair.")')
        assert await phone.locator('.hx-side').is_hidden()
        overflow = await phone.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        assert overflow <= 1, f'phone: horizontal overflow {overflow}px'
        box = await phone.locator('.hx-compose textarea').bounding_box()
        assert box and box['width'] > 220, box
        await shot(phone, 'phone-thread')
        await phone.click('.hx-back')
        await phone.wait_for_selector('.hx-side .hx-item')
        await phone.goto(base + '#roles')
        await phone.wait_for_selector('#view-roles .mg-row:has-text("QA lead")')
        overflow = await phone.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        assert overflow <= 1, f'phone roles: horizontal overflow {overflow}px'
        await shot(phone, 'phone-roles')
        await browser.close()
    await mcp.aclose()
    await runner.cleanup()
    if errors:
        raise SystemExit('Browser errors:\n' + '\n'.join(errors))
    print('huddles view OK' + (f'; screenshots in {shots}' if shots else ''))


asyncio.run(main())
