"""Browser smoke test for synced review entries; no agent invocation required."""
import asyncio
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
from test_work_sessions import HistoryBand


async def main():
    with tempfile.TemporaryDirectory() as temp, MonkeyPatch.context() as patch:
        p = portal.__wrapped__(Path(temp), patch)
        p.server._band = band = HistoryBand()
        work = p.account.work_web
        await work.sync_history(band.workers['host1'], 'claude', [p.uid])
        sid = work.store.all()[0]['id']
        async def index(request):
            return web.Response(content_type='text/html', text=(ROOT / 'rook/web/index.html').read_text())
        p.app.router.add_get('/', index)
        async def empty(request):
            return web.json_response([])
        for route in ['/api/bands', '/api/band/workers', '/api/avatars', '/api/presence', '/api/chat/rooms']:
            p.app.router.add_get(route, empty)
        async with TestServer(p.app) as server, async_playwright() as pw:
            url = str(server.make_url('/'))
            p.account.origin = url.rstrip('/')
            browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            await context.add_cookies([{'name': 'rook_account', 'value': p.headers['Cookie'].split('=', 1)[1], 'url': url}])
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url + '#sessions')
            await expect(page).to_have_url(url + '#work')
            await expect(page.locator('#tab-sessions')).to_have_count(0)
            await expect(page.locator('#tab-work')).to_have_attribute('aria-current', 'page')
            card = page.locator('.work-session-card').filter(has=page.locator(f'[data-session="{sid}"]'))
            await card.locator('[data-session]').click()
            await expect(page.locator('#work-title')).not_to_have_text('Loading…')
            await expect(page.locator('#work-conversation')).to_contain_text('Message 500')
            await expect(page.locator('#work-compose')).to_be_hidden()
            await card.locator('select').select_option('blocked')
            await expect(page.locator('#work-status')).to_have_text('blocked')
            await card.locator('[data-close-session]').click()
            await expect(page.locator('#work-status')).to_have_text('closed')
            await page.reload()
            await page.locator(f'[data-session="{sid}"]').click()
            await expect(page.locator('#work-status')).to_have_text('closed')
            await page.locator(f'[data-status-session="{sid}"]').select_option('auto')
            await expect(page.locator('#work-status')).to_have_text('ready')
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            await browser.close()
            print('PASS: imported transcript, sidebar block/close/auto, reload persistence, mobile width, no browser errors')


if __name__ == '__main__':
    asyncio.run(main())
