"""Drive the chat panel on Manage > Home agent in a real browser, against the
real home agent plugin (fake model endpoint), the real huddle plugin and its
dashboard API, and a real ChatStore.

    .venv/bin/python tests/browser_home_chat.py [--shots DIR]

Covers: the off/unconfigured notice pointing at the form (and no huddle made
by visiting); the first message starting the 1:1 huddle with agent:home (a
chaired huddle with no roles, titled with its name: the one the Huddles page
lists); the thinking state until the reply lands; a failed round shown as an
error with its reason, twice in a row; history and no horizontal scroll at
phone width.
"""
import asyncio
import sys
import tempfile
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
KEY = 'sk-test-not-a-real-key'


class Vault:
    def get(self, name, actor, via='get', task=None): return KEY
    def list(self): return [{'name': 'llm-key'}]


class Accounts:
    def session(self, token):
        return {'id': '1', 'username': 'op', 'admin': True, 'csrf': 'x'} if token == 's' else None


class LLM:
    """OpenAI-compatible endpoint: replies wait for ``gate``; ``status`` != 200 fails."""
    def __init__(self):
        self.gate, self.status, self.calls = asyncio.Event(), 200, 0

    async def __call__(self, method, url, body, headers, timeout):
        if url.endswith('/models'):
            return 200, {'data': [{'id': 'small-model'}]}
        self.calls += 1
        await self.gate.wait()
        if self.status != 200:
            return self.status, {'error': {'message': 'bad key'}}
        return 200, {'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': 'Hello! The lights are on.\nAnything else?'}}]}


async def main():
    shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
    settings_plugin._admin_gate = lambda what: None
    tmp = Path(tempfile.mkdtemp(prefix='rook-homechat-'))
    chat = ChatStore(str(tmp / 'chat.db'))
    node = HubNode(str(tmp), entry_points=False, vault=Vault(), chat=chat,
                   settings_store=SettingsStore(tmp / 'settings.db'))
    node.journal = SimpleNamespace(record=lambda **kw: None)
    home = node.plugin('home')
    home._http = llm = LLM()
    svc = node.settings
    huddles = node.plugin('huddle').service

    async def watcher():          # the chat loop: it must leave the 1:1 huddle to the chair
        while True:
            await home.tick()
            await asyncio.sleep(0.2)

    # The same calls the dashboard's /account/settings/api and /account/huddle/api proxies make.
    async def settings_api(r):
        if r.method == 'GET':
            return web.json_response({'csrf': 'x', **home.page(svc)})
        d = await r.json()
        return web.json_response(await home.page_action(svc, d, 'human:op'))
    mcp = httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=huddle_routes(lambda: node, Accounts()))),
                            base_url='http://mcp.test', cookies={'rook_account': 's'})
    async def huddle_api(r):
        res = await mcp.request(r.method, '/huddle/account-api', content=await r.read(),
                                headers={'Content-Type': 'application/json'})
        return web.Response(body=res.content, status=res.status_code, content_type='application/json')
    async def asset(r): return web.FileResponse(ROOT / 'rook/web' / r.match_info['name'])

    page_html = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/theme.css"><style>body{background:var(--bg);color:var(--fg);font-family:var(--sans);margin:0;padding:16px}</style></head>
<body><div id="view-home"></div><script type="module">
import {mountHome} from '/account/settings/assets/home.js';
window.ui=await mountHome(document.getElementById('view-home'));window.ui.activate();window.ready=true;
</script></body></html>'''
    app = web.Application()
    app.router.add_route('*', '/account/settings/api', settings_api)
    app.router.add_route('*', '/account/huddle/api', huddle_api)
    app.router.add_get('/account/settings/assets/{name}', asset)
    app.router.add_get('/account/bands/assets/{name}', asset)
    app.router.add_get('/account/huddle/assets/{name}', asset)
    app.router.add_get('/theme.css', lambda r: web.FileResponse(ROOT / 'rook/web/theme.css'))
    app.router.add_get('/', lambda r: web.Response(text=page_html, content_type='text/html'))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    loop_task = asyncio.get_running_loop().create_task(watcher())
    errors = []

    async def shot(page, label):
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shots / f'home-chat-{label}.png'), full_page=True)

    def one_to_ones():
        return [x for x in huddles.huddles(archived=True) if x['chaired'] and x['roles'] == []]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: m.type == 'error' and errors.append(m.text))
        await page.goto(base); await page.wait_for_function('window.ready===true')

        # Off: say so, point at the form, no sending, no huddle made.
        await page.wait_for_selector('.hc-note.warn:has-text("The home agent is off")')
        assert await page.is_disabled('.hc-input textarea')
        await page.click('.hc-note button:has-text("Go to the form")')
        await page.wait_for_selector('.hc-log:has-text("No conversation yet")')
        await shot(page, 'off')
        assert one_to_ones() == []

        # Enabled without a model: still blocked, with what is missing.
        for k, v in {'enabled': True, 'base_url': 'http://llm.example:1234/v1',
                     'api_key': '{{secret:llm-key}}'}.items():
            svc.set(f'home.{k}', v, actor='human:op')
        await page.reload(); await page.wait_for_function('window.ready===true')
        await page.wait_for_selector('.hc-note.warn:has-text("needs model")')
        svc.set('home.model', 'small-model', actor='human:op')
        await page.reload(); await page.wait_for_function('window.ready===true')
        await page.wait_for_selector('.hc h2:has-text("Chat with @home")')
        await page.wait_for_function("!document.querySelector('.hc-input textarea').disabled")
        assert await page.locator('.hc-note').is_hidden()

        # First message: starts the 1:1 huddle, shows thinking until the reply.
        await page.fill('.hc-input textarea', 'are the lights on?')
        await page.press('.hc-input textarea', 'Enter')
        await page.wait_for_selector('.hc-msg.me .hc-text:has-text("are the lights on?")')
        await page.wait_for_selector('.hc-typing:has-text("home is thinking")')
        for _ in range(50):
            if llm.calls: break
            await asyncio.sleep(0.1)
        assert llm.calls == 1, 'the chair never took the message'
        await shot(page, 'thinking')
        llm.gate.set()
        await page.wait_for_selector('.hc-msg:not(.me) .hc-text:has-text("The lights are on.")')
        await page.wait_for_selector('.hc-typing', state='hidden')
        hs = one_to_ones()
        assert len(hs) == 1 and hs[0]['title'] == 'home' and not hs[0]['archived'], hs
        room = hs[0]['room']
        assert [m['sender'] for m in chat.read(room, None)['messages']] == [OP, 'agent:home']
        assert llm.calls == 1, 'answered twice (the chat loop too?)'
        assert await page.get_attribute('.hc .mg-panel-head a', 'href') == '#huddles?room=' + room
        await shot(page, 'reply')

        # A failed round ends the wait with an error and its reason.
        llm.status = 401
        await page.fill('.hc-input textarea', 'still there?')
        await page.click('.hc-input button:has-text("Send")')
        await page.wait_for_selector('.hc-note.bad:has-text("could not answer")', timeout=15000)
        assert 'HTTP 401' in await page.inner_text('.hc-note')
        await page.wait_for_selector('.hc-typing', state='hidden')
        await page.wait_for_function("document.querySelectorAll('.hc-msg.quiet').length===1")
        # Again: every failed round says so (the chair posts its line each time).
        await page.fill('.hc-input textarea', 'hello?')
        await page.press('.hc-input textarea', 'Enter')
        await page.wait_for_selector('.hc-note:not(.bad)', state='attached', timeout=5000)
        await page.wait_for_selector('.hc-note.bad:has-text("HTTP 401")', timeout=20000)
        await page.wait_for_function("document.querySelectorAll('.hc-msg.quiet').length===2")
        # The failed turns are in Recent activity.
        await page.wait_for_selector('.mg-row:has-text("Huddle")')
        await shot(page, 'error')
        await page.close()

        # Phone: the same conversation, no horizontal scroll, input usable.
        llm.status = 200
        phone = await browser.new_page(viewport={'width': 390, 'height': 844})
        phone.on('pageerror', lambda e: errors.append('phone: ' + str(e)))
        await phone.goto(base); await phone.wait_for_function('window.ready===true')
        await phone.wait_for_selector('.hc-msg .hc-text:has-text("The lights are on.")')
        assert await phone.locator('.hc-msg').count() == 6
        overflow = await phone.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        assert overflow <= 1, f'phone: horizontal overflow {overflow}px'
        box = await phone.locator('.hc-input textarea').bounding_box()
        assert box and box['width'] > 200, box
        await phone.fill('.hc-input textarea', 'one more')
        await phone.press('.hc-input textarea', 'Enter')
        await phone.wait_for_selector('.hc-text:has-text("Anything else?") >> nth=1')
        await shot(phone, 'phone')
        await browser.close()
    loop_task.cancel()
    await mcp.aclose()
    await runner.cleanup()
    if errors:
        raise SystemExit('Browser errors:\n' + '\n'.join(errors))
    print('home agent chat OK' + (f'; screenshots in {shots}' if shots else ''))


asyncio.run(main())
