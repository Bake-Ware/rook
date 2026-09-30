"""Browser smoke test for the worklog view with real PTY terminals.

Runs the dashboard's Work view against an in-process worker (a real
TerminalsPlugin, so a real shell under a PTY) and drives it with Playwright
Chromium: launch a shell, type into xterm.js, watch from a second viewer,
hand over control, end the session, resume a historical entry, check the
mobile layout and the classic-view switch.

    python tests/browser_worklog.py [--screenshots DIR]
"""
import argparse
import asyncio
import os
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
from test_work_terminals import PtyBand
from rook.worker.plugins import terminals
from rook.worker.plugins.terminals import TerminalsPlugin


async def main(shots):
    with tempfile.TemporaryDirectory() as temp, MonkeyPatch.context() as patch:
        temp = Path(temp)
        patch.setenv('SHELL', '/bin/sh')
        patch.setenv('ROOK_WORK_TERM_DIR', str(temp / 'terms'))
        project = temp / 'project'
        project.mkdir()
        fake = temp / 'claude'
        fake.write_text('#!/bin/sh\necho "claude resumed with: $*"\nexec /bin/sh\n')
        fake.chmod(0o755)
        real_binary = terminals._binary
        patch.setattr(terminals, '_binary', lambda h: str(fake) if h == 'claude' else real_binary(h))
        p = portal.__wrapped__(temp, patch)
        plugin = TerminalsPlugin()
        band = PtyBand(plugin)
        band.workers['host1']['hb']['work']['harnesses'] = ['shell', 'claude']
        p.server._band = band
        work = p.account.work_web
        work.store.save(dict(id='f' * 32, owner=p.uid, worker_id='host1', worker_name='test-host', band='test',
                             agent='claude', imported=True, source_id='8f3a1b2c-0000-4000-8000-000000000001',
                             cwd=str(project), title='Fix the nginx upstream timeouts', status='pending',
                             error='', message_count=12))

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
            # Optional: point at an installed Chrome/Chromium instead of Playwright's download.
            browser = await pw.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or None)
            cookie = [{'name': 'rook_account', 'value': p.headers['Cookie'].split('=', 1)[1], 'url': url}]
            ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            await ctx.add_cookies(cookie)
            page = await ctx.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(url + '#sessions')
            await expect(page.locator('.wl')).to_be_visible()
            await expect(page.locator('#wl-connection')).to_have_text('Connected')
            # History is listed in its project room, collapsed, resumable.
            await page.locator('.wl-room', has_text='project').click()
            entry = page.locator('.wl-entry', has_text='Fix the nginx upstream timeouts')
            await expect(entry).to_be_visible()
            await expect(entry).not_to_have_attribute('open', '')
            # Launch a shell from the room (host and directory prefilled).
            await page.locator('#wl-new').click()
            await expect(page.locator('#wl-launch [name=cwd]')).to_have_value(str(project))
            await page.locator('#wl-launch [name=harness]').select_option('shell')
            await page.locator('#wl-launch [name=mcp]').uncheck()
            await page.locator('#wl-launch [name=title]').fill('Build the thing')
            await page.locator('#wl-launch button[type=submit]').click()
            card = page.locator('.wl-term', has_text='Build the thing')
            await expect(card).to_be_visible(timeout=10000)
            await expect(card.locator('.xterm-rows')).to_contain_text('$', timeout=10000)
            await card.locator('.xterm').click()
            await page.keyboard.type('echo hello-$((6*7))\n')
            await expect(card.locator('.xterm-rows')).to_contain_text('hello-42', timeout=10000)
            await expect(card.locator('.wl-holder')).to_contain_text('You have control')
            if shots:
                await page.screenshot(path=str(Path(shots) / 'worklog-live.png'))
            # A second viewer sees the same terminal and cannot type until handed control.
            page2 = await ctx.new_page()
            page2.on('pageerror', lambda e: errors.append(str(e)))
            await page2.goto(url + '#sessions')
            card2 = page2.locator('.wl-term', has_text='Build the thing')
            await expect(card2.locator('.xterm-rows')).to_contain_text('hello-42', timeout=10000)
            await expect(card2.locator('.wl-holder')).to_contain_text('has control')
            await expect(card.locator('.wl-holder')).to_contain_text('2 watching')
            await card.locator('[data-act=handoff]').select_option(index=1)
            await expect(card2.locator('.wl-holder')).to_contain_text('You have control')
            await card2.locator('.xterm').click()
            await page2.keyboard.type('echo from-two-$((1+1))\n')
            await expect(card.locator('.xterm-rows')).to_contain_text('from-two-2', timeout=10000)
            await card.locator('[data-act=take]').click()
            await expect(card.locator('.wl-holder')).to_contain_text('You have control')
            await page2.close()
            # End the session: it leaves the live list and joins the log.
            page.once('dialog', lambda d: asyncio.ensure_future(d.accept()))
            await card.locator('[data-act=close]').click()
            await expect(card).to_have_count(0, timeout=10000)
            done = page.locator('.wl-entry', has_text='Build the thing')
            await expect(done).to_be_visible()
            await expect(done.locator('summary em')).to_have_text('ended')
            # One-click resume of a historical session through the PTY path.
            await entry.locator('summary').click()
            await entry.locator('[data-resume]').click()
            live = page.locator('.wl-term', has_text='Fix the nginx upstream timeouts')
            await expect(live.locator('.xterm-rows')).to_contain_text('--resume 8f3a1b2c', timeout=10000)
            if shots:
                await page.screenshot(path=str(Path(shots) / 'worklog-resumed.png'))
            # Mobile layout: no horizontal page scroll.
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.wait_for_timeout(300)
            overflow = await page.evaluate('document.documentElement.scrollWidth - innerWidth')
            assert overflow <= 1, overflow
            if shots:
                await page.screenshot(path=str(Path(shots) / 'worklog-mobile.png'), full_page=True)
            await page.set_viewport_size({'width': 1440, 'height': 1000})
            # Classic view switch persists per browser, and back.
            await page.locator('#wl-classic').click()
            await expect(page.locator('.work-layout')).to_be_visible()
            await page.reload()
            await expect(page.locator('.work-layout')).to_be_visible()
            await page.locator('#work-worklog').click()
            await expect(page.locator('.wl')).to_be_visible()
            assert not errors, errors
            await browser.close()
        await plugin.stop()
        print('worklog browser smoke: ok')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    args = parser.parse_args()
    if args.screenshots:
        os.makedirs(args.screenshots, exist_ok=True)
    asyncio.run(main(args.screenshots))
