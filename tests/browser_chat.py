"""Drive the dashboard Chat view in a real browser against a real ChatStore.

    .venv/bin/python tests/browser_chat.py [--shots DIR]

Covers the two regressions from 2026-09-23: clicking a room whose title has
quotes did nothing (the title was inlined into an onclick attribute), and
agent-only rooms (no user:operator participant) were never listed.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from aiohttp import web
from playwright.async_api import async_playwright

from rook.band_mcp.chat_rooms import ChatStore

OP = 'user:operator'


async def main():
    shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
    chat = ChatStore(str(Path(tempfile.mkdtemp(prefix='rook-chat-')) / 'chat.db'))
    mine = chat.start('test', OP, ['agent:hermes_sojourn'])['room']
    chat.send(mine, 'agent:hermes_sojourn', 'hello operator', [], False)
    agents = chat.start('Knowledge system (349e3eb) — remove "gate", it\'s audit only',
                        'agent:Claude web', ['agent:claude code'])['room']
    chat.send(agents, 'agent:claude code', 'agent-to-agent note', [], False)

    # Same calls the bootstrap handlers make.
    async def rooms(r): return web.json_response(chat.rooms_for(OP, limit=200, include_all=True))
    async def read(r): return web.json_response(chat.read(r.query['room'], OP, int(r.query.get('since', 0))))
    async def send(r):
        d = await r.json()
        return web.json_response(chat.send(d['room'], OP, d['text'], d.get('mention') or [], False))
    async def presence(r): return web.json_response({'agents': []})
    async def avatars(r): return web.json_response({})
    async def index(r): return web.Response(text=(ROOT / 'rook/web/index.html').read_text(), content_type='text/html')
    async def other(r): return web.json_response({})

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/api/chat/rooms', rooms)
    app.router.add_get('/api/chat/read', read)
    app.router.add_post('/api/chat/send', send)
    app.router.add_get('/api/presence', presence)
    app.router.add_get('/api/avatars', avatars)
    app.router.add_route('*', '/{tail:.*}', other)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(base + '#chat')
        await page.wait_for_selector('#roomlist .roomrow')
        assert await page.locator('#roomlist .roomrow').count() == 2, 'agent room not listed'
        assert await page.locator('#roomlist .roomsec:has-text("Agent rooms")').count() == 1
        await page.click('#roomlist .roomrow:has-text("test")')
        await page.wait_for_selector('#chatlog .bubble:has-text("hello operator")')
        await page.click('#roomlist .roomrow.other')
        await page.wait_for_selector('#chatlog .bubble:has-text("agent-to-agent note")')
        assert 'audit only' in await page.locator('#ct-title').inner_text()
        assert 'joins you' in await page.get_attribute('#chatbox', 'placeholder')
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shots / 'chat-agent-room.png'))
        await page.fill('#chatbox', 'operator here'); await page.press('#chatbox', 'Enter')
        await page.wait_for_selector('#chatlog .bubble:has-text("operator here")')
        await page.wait_for_function("document.querySelectorAll('#roomlist .roomrow.other').length===0")
        assert OP in chat.rooms_for(OP)['rooms'][0]['participants']
        await browser.close()
    await runner.cleanup()
    if errors:
        raise SystemExit('Browser errors:\n' + '\n'.join(errors))
    print('chat view OK')


asyncio.run(main())
