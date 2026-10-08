"""Drive the chat panel on Manage > Home agent in a real browser, against the
real home agent plugin (fake model endpoint) and a real ChatStore.

    .venv/bin/python tests/browser_home_chat.py [--shots DIR]

Covers: the off/unconfigured notice pointing at the form; the first message
creating the operator's two-person room with agent:home (the room the Chat
view uses); the thinking state until the reply lands; a failed reply shown as
an error, both from the room's generic line and (when the room already had
one) from the agent's activity; history and no horizontal scroll at phone
width.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from aiohttp import web
from playwright.async_api import async_playwright

import rook.hub.plugins.settings as settings_plugin
from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.settings_store import SettingsStore

OP = 'user:operator'
KEY = 'sk-test-not-a-real-key'


class Vault:
    def get(self, name, actor, via='get', task=None): return KEY
    def list(self): return [{'name': 'llm-key'}]


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

    async def watcher():
        while True:
            await home.tick()
            await asyncio.sleep(0.2)

    # The same calls the hub's /api/chat/* handlers and /settings/account-api make.
    async def settings_api(r):
        if r.method == 'GET':
            return web.json_response({'csrf': 'x', **home.page(svc)})
        d = await r.json()
        return web.json_response(await home.page_action(svc, d, 'human:op'))
    async def rooms(r): return web.json_response(chat.rooms_for(OP, limit=200, include_all=True))
    async def read(r): return web.json_response(chat.read(r.query['room'], OP, int(r.query.get('since', 0))))
    async def send(r):
        d = await r.json()
        return web.json_response(chat.send(d['room'], OP, d['text'], d.get('mention') or [], bool(d.get('expects_reply'))))
    async def start(r):
        d = await r.json()
        return web.json_response(chat.start(d.get('title') or 'chat', OP, d.get('invite') or []))
    async def asset(r): return web.FileResponse(ROOT / 'rook/web' / r.match_info['name'])

    page_html = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/theme.css"><style>body{background:var(--bg);color:var(--fg);font-family:var(--sans);margin:0;padding:16px}</style></head>
<body><div id="view-home"></div><script type="module">
import {mountHome} from '/account/settings/assets/home.js';
window.ui=await mountHome(document.getElementById('view-home'));window.ui.activate();window.ready=true;
</script></body></html>'''
    app = web.Application()
    app.router.add_route('*', '/account/settings/api', settings_api)
    app.router.add_get('/account/settings/assets/{name}', asset)
    app.router.add_get('/account/bands/assets/{name}', asset)
    app.router.add_get('/theme.css', lambda r: web.FileResponse(ROOT / 'rook/web/theme.css'))
    app.router.add_get('/api/chat/rooms', rooms)
    app.router.add_get('/api/chat/read', read)
    app.router.add_post('/api/chat/send', send)
    app.router.add_post('/api/chat/start', start)
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: m.type == 'error' and errors.append(m.text))
        await page.goto(base); await page.wait_for_function('window.ready===true')

        # Off: say so, point at the form, no sending, no room created.
        await page.wait_for_selector('.hc-note.warn:has-text("The home agent is off")')
        assert await page.is_disabled('.hc-input textarea')
        await page.click('.hc-note button:has-text("Go to the form")')
        await page.wait_for_selector('.hc-log:has-text("No conversation yet")')
        await shot(page, 'off')
        assert chat.rooms_for(OP)['rooms'] == []

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

        # First message: creates the 1:1 room, shows thinking until the reply.
        await page.fill('.hc-input textarea', 'are the lights on?')
        await page.press('.hc-input textarea', 'Enter')
        await page.wait_for_selector('.hc-msg.me .hc-text:has-text("are the lights on?")')
        await page.wait_for_selector('.hc-typing:has-text("home is thinking")')
        for _ in range(50):
            if llm.calls: break
            await asyncio.sleep(0.1)
        assert llm.calls == 1, 'the home agent never picked the message up'
        await shot(page, 'thinking')
        llm.gate.set()
        await page.wait_for_selector('.hc-msg:not(.me) .hc-text:has-text("The lights are on.")')
        await page.wait_for_selector('.hc-typing', state='hidden')
        rooms_ = chat.rooms_for(OP)['rooms']
        assert len(rooms_) == 1 and sorted(rooms_[0]['participants']) == ['agent:home', OP], rooms_
        room = rooms_[0]['room']
        assert [m['sender'] for m in chat.read(room, None)['messages']] == [OP, 'agent:home']
        await shot(page, 'reply')

        # A failed reply: the room's generic line ends the wait with an error.
        llm.status = 401
        await page.fill('.hc-input textarea', 'still there?')
        await page.click('.hc-input button:has-text("Send")')
        await page.wait_for_selector('.hc-note.bad:has-text("could not answer")', timeout=15000)
        await page.wait_for_selector('.hc-typing', state='hidden')
        assert await page.locator('.hc-msg.quiet').count() == 1
        # A second failure posts nothing in the room (once per 10 minutes): the
        # panel finds it in the agent's activity and shows the summary.
        await page.fill('.hc-input textarea', 'hello?')
        await page.press('.hc-input textarea', 'Enter')
        await page.wait_for_selector('.hc-note:not(.bad)', state='attached', timeout=5000)
        await page.wait_for_selector('.hc-note.bad:has-text("HTTP 401")', timeout=20000)
        await shot(page, 'error')
        await page.close()

        # Phone: the same conversation, no horizontal scroll, input usable.
        llm.status = 200
        phone = await browser.new_page(viewport={'width': 390, 'height': 844})
        phone.on('pageerror', lambda e: errors.append('phone: ' + str(e)))
        await phone.goto(base); await phone.wait_for_function('window.ready===true')
        await phone.wait_for_selector('.hc-msg .hc-text:has-text("The lights are on.")')
        assert await phone.locator('.hc-msg').count() == 5
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
    await runner.cleanup()
    if errors:
        raise SystemExit('Browser errors:\n' + '\n'.join(errors))
    print('home agent chat OK' + (f'; screenshots in {shots}' if shots else ''))


asyncio.run(main())
