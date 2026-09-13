"""Exercise worker-owned Work through Chromium without a real agent."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from pytest import MonkeyPatch
from test_band_management import portal
from test_work_sessions import RuntimeBand


async def main():
    with tempfile.TemporaryDirectory() as temp, MonkeyPatch.context() as patch:
        p = portal.__wrapped__(Path(temp), patch)
        band = RuntimeBand(Path(temp) / 'host' / 'work.sqlite3')
        p.server._band = band
        await band.plugin.start()
        async def index(request):
            return web.Response(content_type='text/html', text=(ROOT / 'rook/web/index.html').read_text())
        async def empty(request):
            return web.json_response([])
        p.app.router.add_get('/', index)
        for route in ['/api/bands', '/api/band/workers', '/api/avatars', '/api/presence', '/api/chat/rooms']:
            p.app.router.add_get(route, empty)
        try:
            async with TestServer(p.app) as server, async_playwright() as pw:
                url = str(server.make_url('/'))
                p.account.origin = url.rstrip('/')
                browser = await pw.chromium.launch()
                ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                await ctx.add_cookies([{'name': 'rook_account', 'value': p.headers['Cookie'].split('=', 1)[1], 'url': url}])
                page = await ctx.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(url + '#work')
                await page.locator('#work-create input[name=title]').fill('Host-owned work')
                await page.locator('#work-create input[name=cwd]').fill('/tmp')
                await expect(page.locator('#work-create select[name=worker] option')).to_have_count(2)
                await page.locator('#work-create select[name=worker]').select_option('host1')
                await page.locator('#work-create button[type=submit]').click()
                await expect(page.locator('#work-status')).to_have_text('ready', timeout=15000)
                sid = p.account.work_web.store.all()[0]['id']
                await page.locator('#work-input').fill('A private prompt')
                await page.locator('#work-compose button').click()
                await expect(page.locator('#work-status')).to_have_text('working', timeout=15000)
                band.emit({'method': 'item/agentMessage/delta', 'params': {'itemId': 'a', 'delta': '🦉' * 7000 + ' PAGED_END'}})
                band.emit({'method': 'item/commandExecution/requestApproval', 'id': 88, 'params': {'command': 'echo approval'}})
                await expect(page.locator('#work-conversation')).to_contain_text('PAGED_END', timeout=20000)
                await page.get_by_role('button', name='Decline', exact=True).click()
                band.emit({'method': 'turn/completed', 'params': {'turn': {}}})
                await expect(page.locator('#work-pending')).to_be_empty(timeout=15000)
                band.emit({'method': 'item/agentMessage/delta', 'params': {'itemId': 'b', 'delta': 'INCREMENTAL_UPDATE'}})
                await expect(page.locator('#work-conversation')).to_contain_text('INCREMENTAL_UPDATE', timeout=15000)
                assert any(cap == 'work.view_page' and args.get('since', 0) > 0 for cap, args in band.calls)
                with p.account.work_web.store.db() as db:
                    saved = db.execute('SELECT state FROM work_sessions WHERE id=?', (sid,)).fetchone()[0]
                    assert not any(text in saved for text in ('PAGED_END', 'INCREMENTAL_UPDATE', 'A private prompt', 'echo approval'))
                    assert db.execute('SELECT COUNT(*) FROM work_events').fetchone()[0] == 0
                await page.reload()
                await page.locator(f'[data-session="{sid}"]').click()
                await expect(page.locator('#work-conversation')).to_contain_text('INCREMENTAL_UPDATE', timeout=20000)
                await page.locator('#work-close').click()
                await expect(page.locator('#work-status')).to_have_text('closed', timeout=15000)
                await page.locator('#work-resume').click()
                await expect(page.locator('#work-status')).to_have_text('ready', timeout=15000)
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert not errors, errors
                await browser.close()
                print('PASS: worker-owned create, paged view, incremental output, approval, reload, close/reopen, metadata-only web DB, mobile')
        finally:
            await band.plugin.stop()


if __name__ == '__main__':
    asyncio.run(main())
