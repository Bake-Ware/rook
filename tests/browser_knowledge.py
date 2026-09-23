"""Render the Knowledge (wiki) and Work (tasks) views in a real browser against
a real KnowledgeService with seeded data, at desktop and phone widths.

    .venv/bin/python tests/browser_knowledge.py [--shots DIR]

Fails on JavaScript errors or missing content. Screenshots go to DIR.
"""
import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from aiohttp import web
from playwright.async_api import async_playwright

from rook.knowledge.service import KnowledgeService

AGENT = {'kind': 'agent', 'actor': 'claude.claudecode.cachyrig@.home.bake.rook', 'token': 'claude',
         'client': 'claudecode', 'host': 'cachyrig', 'dir': '/home/bake/rook'}
HUMAN = {'id': 'human:bake', 'kind': 'human', 'label': 'Bake'}


class Bands:
    def bands(self, active_only=False):
        return [{'id': 'b1', 'name': 'bakenet', 'label': '7f68c499', 'is_primary': 1},
                {'id': 'b2', 'name': 'rooknet', 'label': '6178ba5f', 'is_primary': 0}]


async def seed(s):
    async def do(action, rkind=None, band=None, rid=None, **data):
        return await s.dispatch(action, band, rkind, rid, data=data, request_id=uuid.uuid4().hex)
    await do('create', 'knowledge', 'bakenet', title='Hosts', slug='hosts', body='Machines on the band.')
    await do('create', 'knowledge', 'bakenet', title='Sojourn (Hermes agent host)', slug='sojourn', parent='hosts',
             body='# Role\nRuns the **Hermes** agent. Memory lives in `/root/.hermes/memories/`.\n\n- Restart the gateway after updates\n- See [[hermes-mcp-empty-responses]]\n\nDocs: https://example.com/hermes')
    fix = await do('create', 'knowledge', 'bakenet', title='Hermes: MCP tool calls return empty', slug='hermes-mcp-empty-responses',
                   body='Stale gateway MCP session after a network change.\n\n```\nhermes gateway restart\n```\nHost: [[sojourn]]')
    await do('link', rid=fix['id'], kind='journal', ref='c9c799b6', relation='source', note='read MEMORY.md')
    c = await do('create', 'concept', 'bakenet', title='Agent work system', slug='agent-work-system')
    p = await do('create', 'project', 'bakenet', title='Agent work system rollout', slug='rollout', parent=c['id'],
                 body='Ship it. Part of [[agent-work-system]].')
    t1 = await do('create', 'task', 'bakenet', title='Deploy e6aaa2b', slug='deploy', parent=p['id'], body='Ship to the hub.')
    await do('claim', rid=t1['id'])
    await do('link', rid=t1['id'], kind='journal', ref='4f6a413a', relation='evidence')
    cur = await do('get', rid=t1['id'])
    await do('update', rid=t1['id'], revision=cur['revision'], patch={'state': 'done', 'attrs': {'outcome': 'Live; 38/38 tests.'}})
    t2 = await do('create', 'task', 'bakenet', title='Rotate Hermes secrets', slug='rotate', parent=p['id'],
                  body='Move secrets out of memory. See [[sojourn]].', attrs={'criteria': ['No secrets in memory'], 'tags': ['security']})
    await do('claim', rid=t2['id'])
    t3 = await do('create', 'task', 'bakenet', title='Fix hermes.memory.read', slug='fix-read', parent=p['id'])
    cur = await do('get', rid=t3['id'])
    await do('update', rid=t3['id'], revision=cur['revision'], patch={'state': 'blocked', 'attrs': {'blocked_reason': 'needs worker restart window'}})
    await do('create', 'task', 'bakenet', title='Import Claude memory', slug='import-claude', parent=p['id'])
    c2 = await do('create', 'concept', 'rooknet', title='Tablets', slug='tablets')
    p2 = await do('create', 'project', 'rooknet', title='Tablet fleet', slug='tablet-fleet', parent=c2['id'])
    await do('create', 'task', 'rooknet', title='Charge the tablets', parent=p2['id'])


async def main():
    shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
    tmp = Path(tempfile.mkdtemp(prefix='rook-kn-'))
    svc = KnowledgeService(tmp / 'k.db', lambda: AGENT, Bands(), handoffs=lambda a, h: 'thread-1')
    await seed(svc)

    async def api(request):
        if request.method == 'GET':
            return web.json_response({'csrf': 'x', 'bands': svc.bands(), 'actor': HUMAN})
        d = await request.json()
        try:
            res = await svc.dispatch(d.get('action', 'list'), d.get('band'), d.get('kind'), d.get('id'),
                                     d.get('query', ''), d.get('data'), d.get('request_id'), actor=HUMAN)
            return web.json_response({'ok': True, 'result': res})
        except (ValueError, KeyError, TypeError, PermissionError) as e:
            return web.json_response({'error': str(e)}, status=400)

    async def asset(request):
        return web.FileResponse(ROOT / 'rook/web' / request.match_info['name'])

    page_html = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/theme.css"><style>body{background:var(--bg);color:var(--fg);font-family:var(--sans);margin:0;padding:16px}
button{font:inherit;color:var(--fg);background:var(--chip);border:1px solid var(--line2);padding:6px 10px;cursor:pointer}</style></head>
<body><div id="view-knowledge" hidden></div><div id="view-work" hidden></div><script type="module">
import {mountKnowledge,mountTasks} from '/account/knowledge/assets/knowledge.js';
const k=await mountKnowledge(document.getElementById('view-knowledge')), w=await mountTasks(document.getElementById('view-work'));
let cur=null;async function show(){const v=location.hash.slice(1).split('?')[0]||'knowledge';if(v===cur)return;cur=v;
 document.getElementById('view-knowledge').hidden=v!=='knowledge';document.getElementById('view-work').hidden=v!=='work';
 if(v==='knowledge'){w.deactivate();await k.activate();}else{k.deactivate();await w.activate();}}
window.addEventListener('hashchange',show);await show();window.ready=true;
</script></body></html>'''

    app = web.Application()
    app.router.add_route('*', '/account/knowledge/api', api)
    app.router.add_get('/account/knowledge/assets/{name}', asset)
    app.router.add_get('/theme.css', lambda r: web.FileResponse(ROOT / 'rook/web/theme.css'))
    app.router.add_get('/', lambda r: web.Response(text=page_html, content_type='text/html'))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{port}/'
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for name, size in (('desktop', {'width': 1280, 'height': 900}), ('phone', {'width': 390, 'height': 844})):
            page = await browser.new_page(viewport=size)
            page.on('pageerror', lambda e, n=name: errors.append(f'{n}: {e}'))
            page.on('console', lambda m, n=name: m.type == 'error' and errors.append(f'{n}: {m.text}'))

            async def shot(label, page=page, name=name):
                await page.wait_for_timeout(250)
                if shots:
                    shots.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(shots / f'{name}-{label}.png'), full_page=True)
            try:
                await run_checks(page, base, shot)
            except Exception:
                print('collected browser errors:', errors, file=sys.stderr)
                await shot('FAILED')
                raise
            overflow = await page.evaluate('document.documentElement.scrollWidth - window.innerWidth')
            assert overflow <= 1, f'{name}: horizontal overflow {overflow}px'
            await page.close()
        await browser.close()
    await runner.cleanup()
    if errors:
        raise SystemExit('Browser errors:\n' + '\n'.join(errors))
    print('knowledge + work views OK' + (f'; screenshots in {shots}' if shots else ''))


async def run_checks(page, base, shot):
    await page.goto(base + '#knowledge'); await page.wait_for_function('window.ready===true')
    await page.wait_for_selector('text=Recently updated')
    await page.evaluate("localStorage.removeItem('kn-open')")
    assert await page.locator('.kn-tree-item').count() == 2   # Hosts (folded) + the top-level page
    assert await page.locator('.kn-sections .kn-card:has-text("Hosts")').count() == 1
    await shot('wiki-home')
    await page.click('.kn-tog[aria-label="Expand Hosts"]')
    await page.click('.kn-tree-item:has-text("Sojourn")')
    await page.wait_for_selector('h1:has-text("Sojourn")')
    assert await page.locator('.kn-crumbs button:has-text("Hosts")').count() == 1
    assert await page.locator('.kn-md h3:has-text("Role")').count() == 1
    assert await page.locator('.kn-md code').count() == 1
    assert await page.locator('h2:has-text("What links here")').count() == 1
    await shot('wiki-page')
    await page.click('.kn-ref:has-text("hermes-mcp-empty-responses")')
    await page.wait_for_selector('h1:has-text("MCP tool calls return empty")')
    assert await page.locator('.kn-md pre').count() == 1
    assert await page.locator('h2:has-text("Sources and evidence")').count() == 1
    # Move the Hermes page into Hosts with the Move dialog.
    await page.click('.kn-page button:has-text("Move")')
    await page.select_option('.kn-dialog select[name=parent]', label='Hosts')
    await page.click('.kn-dialog button:has-text("Save")')
    await page.wait_for_selector('.kn-crumbs button:has-text("Hosts")')
    assert await page.locator('.kn-tree-row').count() == 3 and await page.locator('.kn-tree-item').first.inner_text() == 'Hosts\n2'
    await page.click('.kn-crumbs button:has-text("Hosts")'); await page.wait_for_selector('h1:has-text("Hosts")')
    assert await page.locator('h2:has-text("Pages in here") + ul li').count() == 2
    await shot('wiki-section')
    await page.go_back(); await page.wait_for_selector('h1:has-text("MCP tool calls return empty")')
    await page.click('.kn-page button:has-text("Move")')   # and back to the top level
    await page.select_option('.kn-dialog select[name=parent]', value='')
    await page.click('.kn-dialog button:has-text("Save")')
    await page.wait_for_function("!document.querySelector('.kn-crumbs button:nth-of-type(2)')")
    await page.go_back(); await page.go_back(); await page.wait_for_selector('h1:has-text("Sojourn")')
    # Verify from the page, then walk the rest in review mode.
    await page.click('.kn-review button:has-text("Verify")')
    await page.wait_for_selector('.kn-review-verified:has-text("Verified by Bake")')
    assert await page.locator('.kn-current .kn-dot-verified').count() == 1
    await page.click('.kn-review-btn'); await page.wait_for_selector('.kn-reviewbar')
    await shot('review-mode')
    first = await page.locator('.kn-page h1').inner_text()
    await page.click('.kn-reviewbar button:has-text("Dispute")')
    await page.fill('.kn-dialog textarea[name=note]', 'Wrong restart command')
    await page.click('.kn-dialog button:has-text("Save")')
    await page.wait_for_function(f"document.querySelector('.kn-page h1')?.textContent!=={first!r}")
    second = await page.locator('.kn-page h1').inner_text()
    await page.click('.kn-reviewbar button:has-text("Verify & next")')
    await page.wait_for_selector('text=Review done')
    await page.locator('.kn-tree-item span', has_text=first).first.click()
    await page.wait_for_selector('.kn-review-disputed:has-text("Wrong restart command")')
    await page.click('.kn-review-disputed button:has-text("Clear")')
    await page.wait_for_selector('.kn-review-unverified')
    for title in (second, 'Sojourn'):   # leave the data as found for the next viewport
        await page.locator('.kn-tree-item span', has_text=title).first.click()
        await page.wait_for_selector(f'h1:has-text({title!r})')
        await page.click('.kn-review button:has-text("Unverify")'); await page.wait_for_selector('.kn-review-unverified')
    assert await page.locator('.kn-md strong:has-text("Hermes")').count() == 1
    await page.fill('.kn-side input[type=search]', 'stale'); await page.press('.kn-side input[type=search]', 'Enter')
    await page.wait_for_function("document.querySelectorAll('#view-knowledge .kn-index-item').length===1")
    await page.goto(base + '#work'); await page.wait_for_selector('#view-work h1:has-text("Work")')
    assert await page.locator('#view-work .kn-section h3:has-text("In progress")').count() == 1
    assert await page.locator('#view-work .kn-side h3:has-text("Agent work system")').count() == 1
    await shot('work-overview')
    await page.click('#view-work .kn-index-item:has-text("Agent work system rollout")')
    await page.wait_for_selector('#view-work h1:has-text("Agent work system rollout")')
    for label in ('In progress', 'Blocked', 'To do', 'Recently done'):
        assert await page.locator(f'#view-work .kn-section h3:has-text("{label}")').count() == 1, label
    await shot('work-project')
    await page.click('#view-work .kn-card:has-text("Deploy e6aaa2b")')
    await page.wait_for_selector('#view-work h1:has-text("Deploy e6aaa2b")')
    assert await page.locator('.kn-callout:has-text("Live; 38/38 tests.")').count() == 1
    assert await page.locator('h2:has-text("Audit trail")').count() == 1
    await shot('work-task')
    await page.goto(base + '#work?t=rotate&b=b1'); await page.wait_for_selector('#view-work h1:has-text("Rotate Hermes secrets")')
    await page.click('#view-work .kn-ref:has-text("sojourn")')  # cross-view jump to the wiki
    await page.wait_for_selector('#view-knowledge h1:has-text("Sojourn")')


asyncio.run(main())
