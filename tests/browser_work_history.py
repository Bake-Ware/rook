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
        band.active = True
        band.messageable = True
        band.workers['host1']['caps'].append('claude-history.follow')
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
            await expect(page.locator('#work-conversation')).to_contain_text('Message 500', timeout=15000)
            await expect(page.get_by_role('button', name='Load more', exact=True)).to_have_count(0)
            assert all('items' not in s for s in work.store.all())
            await expect(page.locator('#work-compose')).to_be_visible()
            await page.locator('#work-input').fill('A message from Work')
            await page.locator('#work-input').press('Shift+Enter')
            await page.locator('#work-input').type('with a second line 💌')
            assert not any(cap.endswith('.send') for cap, _ in band.calls)
            await page.locator('#work-input').press('Enter')
            await expect(page.locator('.work-session-heading #work-history-refresh')).to_be_visible()
            await expect(page.locator('#work-resume-note')).to_have_text('Message queued on host.')
            sent = [(cap, args) for cap, args in band.calls if cap.endswith('.send')]
            assert len(sent) == 1 and sent[0][1]['text'] == 'A message from Work\nwith a second line 💌'
            assert sent[0][1]['session_id'] == work.store.get(sid)['source_id']
            assert 'second line' not in work.store.path.read_bytes().decode(errors='ignore')
            band.fail_send = True
            await page.locator('#work-input').fill('Keep this failed draft')
            await page.locator('#work-compose button').click()
            await expect(page.locator('#work-error')).to_have_text('Host rejected the message.')
            await expect(page.locator('#work-input')).to_have_value('Keep this failed draft')
            band.fail_send = False
            await expect(page.locator('#work-resume')).to_be_hidden()
            await expect(page.locator('#work-meta')).to_contain_text('Active on host')
            # Switching sessions must cancel an unfinished whole-conversation read.
            blocked, release = asyncio.Event(), asyncio.Event()
            cancelled = []
            async def hold_old_page(route):
                if f'/history/{sid}?' in route.request.url and 'offset=20&' in route.request.url and not blocked.is_set():
                    blocked.set()
                    await release.wait()
                try:
                    await route.continue_()
                except Exception:
                    pass  # The previous fetch was cancelled by selecting another entry.
            page.on('requestfailed', lambda request: cancelled.append(request.url))
            await page.route('**/account/work/history/**', hold_old_page)
            await page.locator('#work-history-refresh').click()
            await asyncio.wait_for(blocked.wait(), 5)
            other = next(s['id'] for s in work.store.all() if s['id'] != sid)
            await page.locator(f'[data-session="{other}"]').click()
            release.set()
            await expect(page.locator('#work-conversation')).to_contain_text('Message 500', timeout=15000)
            assert any(f'/history/{sid}?' in url and 'offset=20&' in url for url in cancelled)
            await page.unroute('**/account/work/history/**', hold_old_page)
            await page.locator(f'[data-session="{sid}"]').click()
            await expect(page.locator('#work-history-note')).to_have_text('', timeout=15000)
            failed_follow = []
            async def fail_one_follow(route):
                if 'follow=1' in route.request.url and not failed_follow:
                    failed_follow.append(True)
                    await route.fulfill(status=503, content_type='application/json', body='{"error":"Temporary host disconnect"}')
                else:
                    await route.continue_()
            await page.route('**/account/work/history/**', fail_one_follow)
            band.version += 1
            await expect(page.locator('#work-conversation')).to_contain_text('Live update from host', timeout=15000)
            assert failed_follow
            await page.unroute('**/account/work/history/**', fail_one_follow)
            await expect(page.locator('#work-history-note')).to_have_text('')
            follow_calls = [args for cap, args in band.calls if cap.endswith('.follow')]
            assert follow_calls and all(args['offset'] >= 500 for args in follow_calls)
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
