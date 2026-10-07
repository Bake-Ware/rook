"""Browser test for the Sessions page (rook/web/sessions.js).

Runs the dashboard against an in-process worker: a real TerminalsPlugin (so
real shells under a PTY) plus scripted ``sessions.*`` caps for sessions Rook
did not start (a mirrored Claude Code session whose inbox holds messages, a
Codex session with only a transcript, a closed Claude session to resume).
Drives it with Playwright Chromium: the grouped list and its filters, a stale
host notice, New session into a live terminal (type, second viewer, hand
off), the live mirror view (streamed text, tool output in a read-only
emulator), the transcript view, Send (held, turn and keys), Link to task,
Resume into a Rook terminal, Stop, the classic-view link and the phone layout.

    python tests/browser_sessions.py [--screenshots DIR]
"""
import argparse
import asyncio
import os
import sys
import tempfile
import time
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

MIRRORED = '6f1c0a52-0000-4000-8000-00000000a001'
CODEX = '019a2b3c-0000-7000-8000-00000000c002'
CLOSED = '8f3a1b2c-0000-4000-8000-000000000001'
# Tool output with colour, a clipboard write (OSC 52) and a hyperlink (OSC 8):
# the page must render the text and swallow both sequences.
TOOL_OUT = ('\x1b[32mPASSED\x1b[0m tests/test_upstream.py\n'
            '\x1b]52;c;cHduZWQ=\x07\x1b]8;;https://example.invalid/\x07click-me\x1b]8;;\x07 done\n')


class SessionsBand(PtyBand):
    """A real terminals worker with scripted sessions.* caps, plus a second
    worker that has stopped answering (served stale from the hub's cache)."""

    def __init__(self, plugin, project):
        super().__init__(plugin)
        self.project = project
        w = self.workers['host1']
        w['caps'] = w['caps'] + ['sessions.list', 'sessions.mirror', 'sessions.follow', 'sessions.send', 'sessions.stop']
        w['hb'] = {'work': {'harnesses': ['shell', 'claude']}, 'sessions': {'live': 2, 'idle': 0}}
        self.workers['host2'] = dict(worker_id='host2', name='laptop', band='test', last_seen=time.time(),
                                     caps=['sessions.list'], hb={})
        self.host2_down = False
        self.sent = []
        self.mirror_calls = 0

    def records(self):
        now = time.time()
        recs = {
            ('claude', MIRRORED): dict(agent='claude', native_id=MIRRORED, title='Fix the nginx upstream timeouts',
                                       cwd=str(self.project), state='live', origin='external', updated=now - 30, messages=40,
                                       view={'terminal': None, 'mirror': True, 'transcript': True}, input='inbox',
                                       inbox_policy='hold', links={}, resumable=False, activity='working'),
            ('codex', CODEX): dict(agent='codex', native_id=CODEX, title='Port the parser to Rust',
                                   cwd='/srv/parser', state='idle', origin='external', updated=now - 600, messages=3,
                                   view={'terminal': None, 'mirror': False, 'transcript': True}, input='inbox',
                                   inbox_policy='accept', links={}, resumable=False),
            ('claude', CLOSED): dict(agent='claude', native_id=CLOSED, title='Write the release notes',
                                     cwd=str(self.project), state='closed', origin='external', updated=now - 90000,
                                     messages=12, view={'terminal': None, 'mirror': False, 'transcript': True},
                                     input='none', inbox_policy='unknown', links={}, resumable=True),
        }
        for t in self.plugin.terms.values():
            info = t.info()
            native = info['resume'] or info['id']
            recs[(info['harness'], native)] = dict(
                agent=info['harness'], native_id=native, title=info['title'] or info['harness'], cwd=info['cwd'],
                state='live' if info['running'] else 'closed', origin='rook', updated=now,
                messages=None, view={'terminal': info['id'], 'mirror': False, 'transcript': bool(info['resume'])},
                input='pty' if info['running'] else 'none', inbox_policy='unknown',
                links={'work_session': info['session']} if info['session'] else {},
                resumable=not info['running'] and bool(info['resume']))
        return list(recs.values())

    def find(self, agent, native):
        return next((r for r in self.records() if r['agent'] == agent and r['native_id'] == native), None)

    async def call(self, cap, args, target, timeout, identity=None):
        if target == 'host2':
            if self.host2_down:
                raise asyncio.TimeoutError()
            return {'ok': True, 'from': target, 'result': {'ok': True, 'harnesses': [], 'total': 1, 'items': [dict(
                agent='claude', native_id='aaaa0000-0000-4000-8000-0000000000b3', title='Laptop notes', cwd='/home/me/notes',
                state='closed', origin='external', updated=time.time() - 7200, messages=5,
                view={'terminal': None, 'mirror': False, 'transcript': True}, input='none',
                inbox_policy='unknown', links={}, resumable=True)]}}
        if not cap.startswith('sessions.'):
            return await super().call(cap, args, target, timeout, identity)
        self.calls.append((cap, dict(args)))
        result = await self.scripted(cap, args)
        return {'ok': True, 'from': target, 'result': result}

    async def scripted(self, cap, args):
        if cap == 'sessions.list':
            items = self.records()
            if args.get('live_only'):
                items = [i for i in items if i['state'] != 'closed']
            q = (args.get('query') or '').lower()
            if q:
                items = [i for i in items if q in f"{i['title']} {i['cwd']} {i['native_id']}".lower()]
            items.sort(key=lambda r: (r['state'] == 'closed', -r['updated']))
            return {'ok': True, 'harnesses': ['shell', 'claude'], 'items': items, 'total': len(items)}
        if cap == 'sessions.mirror':
            self.mirror_calls += 1
            if args['cursor'] == 0:
                return {'ok': True, 'exists': True, 'done': False, 'cursor': 12, 'events': [
                    {'seq': 1, 'type': 'session.start', 'cwd': str(self.project), 'model': 'opus', 'version': '2.1', 'inbound': 'hold'},
                    {'seq': 2, 'type': 'prompt', 'text': 'Why do upstream requests time out?', 'from': 'person'},
                    {'seq': 3, 'type': 'state', 'state': 'working'},
                    {'seq': 4, 'type': 'assistant.delta', 'text': 'Checking the proxy '},
                    {'seq': 5, 'type': 'assistant.delta', 'text': 'timeouts first.'},
                    {'seq': 6, 'type': 'assistant.done', 'text': 'Checking the proxy timeouts first.'},
                    {'seq': 7, 'type': 'tool.call', 'id': 'tc1', 'name': 'Bash', 'input': {'command': 'pytest tests/test_upstream.py'}},
                    {'seq': 8, 'type': 'tool.result', 'id': 'tc1', 'ok': True, 'text': TOOL_OUT},
                    {'seq': 9, 'type': 'assistant.delta', 'text': 'proxy_read_timeout '},
                    {'seq': 10, 'type': 'assistant.done', 'text': 'proxy_read_timeout is 5s.'},
                    {'seq': 11, 'type': 'turn.end', 'stop_reason': 'answer'},
                    {'seq': 12, 'type': 'state', 'state': 'waiting'},
                ]}
            await asyncio.sleep(min(float(args.get('wait') or 0), 0.5))
            return {'ok': True, 'exists': True, 'done': False, 'cursor': args['cursor'], 'events': []}
        if cap == 'sessions.follow':
            if args.get('version') == 'v1':
                return {'ok': True, 'unchanged': True, 'version': 'v1'}
            msgs = [('user', 'Port the tokenizer first.'), ('assistant', 'Done: src/lexer.rs compiles.'),
                    ('tool', 'cargo test\n\x1b[32mok\x1b[0m 14 passed')]
            return {'ok': True, 'version': 'v1', 'replace_from': args['offset'], 'truncated': False, 'total_messages': 3,
                    'messages': [dict(index=i, content_offset=0, role=r, content=c) for i, (r, c) in enumerate(msgs)
                                 if i >= args['offset']]}
        if cap == 'sessions.send':
            self.sent.append(dict(args))
            rec = self.find(args['agent'], args['native_id'])
            if rec['input'] == 'pty':
                await self.plugin.caps()['work.stream.write'](id=rec['view']['terminal'], data=args['text'] + '\r')
                return {'ok': True, 'delivery': 'keys', 'note': 'Typed.', 'terminal': rec['view']['terminal']}
            if rec['inbox_policy'] == 'hold':
                return {'ok': True, 'delivery': 'held', 'note': 'Waiting for approval on this host.', 'detail': 'queued'}
            return {'ok': True, 'delivery': 'turn', 'note': 'Delivered.'}
        if cap == 'sessions.stop':
            rec = self.find(args['agent'], args['native_id'])
            term = rec['view']['terminal']
            res = await self.plugin.caps()['work.stream.close'](id=term)
            return {'ok': True, 'stopped': 'terminal', 'terminal': term, 'exit_code': res.get('exit_code')}
        raise AssertionError(cap)


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
        band = SessionsBand(plugin, project)
        p.server._band = band
        # Task claims, links and end notes go to the knowledge service: recorded, never sent.
        task_writes = []

        async def knowledge_call(request, user, payload):
            task_writes.append(payload)
            return {'task': payload.get('id')} if payload.get('action') == 'claim' else {}
        p.account.work_web.knowledge_call = knowledge_call

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
            browser = await pw.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or None)
            cookie = [{'name': 'rook_account', 'value': p.headers['Cookie'].split('=', 1)[1], 'url': url}]
            ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            await ctx.add_cookies(cookie)
            # Count clipboard writes: tool output must never reach the clipboard.
            await ctx.add_init_script("""window.__clipboardWrites = 0;
              try { const c = navigator.clipboard; if (c) { c.writeText = async () => { window.__clipboardWrites++; };
                c.write = async () => { window.__clipboardWrites++; }; } } catch {}""")
            page = await ctx.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(url + '#sessions')

            # The list: grouped by host then project, live first.
            await expect(page.locator('.sx')).to_be_visible()
            item = lambda text: page.locator('.sx-item', has_text=text)
            await expect(item('Fix the nginx upstream timeouts')).to_be_visible()
            hosts = page.locator('.sx-host-head')
            await expect(hosts.first).to_contain_text('test-host')
            await expect(hosts.nth(1)).to_contain_text('laptop')
            first_project = page.locator('.sx-host').first.locator('.sx-project').first
            await expect(first_project.locator('.sx-project-head strong')).to_have_text('project')
            await expect(first_project.locator('.sx-item').first).to_contain_text('Fix the nginx upstream timeouts')
            await expect(first_project.locator('.sx-item').last).to_contain_text('Write the release notes')
            await expect(page.locator('#sx-counts')).to_have_text('1 live · 1 idle · 2 closed')
            # Filters: agent, host, live only, search.
            await page.locator('#sx-agent').select_option('codex')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await page.locator('#sx-agent').select_option('')
            await page.locator('#sx-host').select_option('host2')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await expect(page.locator('.sx-item')).to_contain_text('Laptop notes')
            await page.locator('#sx-host').select_option('')
            await page.locator('#sx-live').check()
            await expect(page.locator('.sx-item')).to_have_count(2)
            await page.locator('#sx-live').uncheck()
            await page.locator('#sx-search').fill('release')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await page.locator('#sx-search').fill('')
            await expect(page.locator('.sx-item')).to_have_count(4)
            # A host that stops answering stays listed from the hub's cache, with a notice.
            band.host2_down = True
            await page.evaluate("document.querySelector('#sx-live').dispatchEvent(new Event('change'))")
            await expect(page.locator('.sx-notice')).to_contain_text('laptop did not answer')
            await expect(page.locator('.sx-host-head', has_text='laptop')).to_contain_text('stale')
            await expect(item('Laptop notes')).to_be_visible()
            band.host2_down = False

            # Tier 2: the mirror view of a session started outside Rook.
            await item('Fix the nginx upstream timeouts').click()
            detail = page.locator('#sx-detail')
            await expect(detail.locator('h3')).to_have_text('Fix the nginx upstream timeouts')
            await expect(detail.locator('.sx-msg.sx-user')).to_contain_text('Why do upstream requests time out?')
            await expect(detail.locator('.sx-msg.sx-assistant .sx-text')).to_have_text(
                ['Checking the proxy timeouts first.', 'proxy_read_timeout is 5s.'])
            await expect(detail.locator('.sx-turn')).to_have_text('Turn ended')
            await expect(detail.locator('.sx-state')).to_have_text('waiting for approval on test-host')
            tool = detail.locator('.sx-tool')
            await expect(tool.locator('.sx-tool-name')).to_have_text('Bash')
            await expect(tool.locator('.sx-tool-ok')).to_have_text('ok')
            await tool.locator('summary').click()  # results start folded when they succeeded
            await expect(tool.locator('.sx-pane .xterm-rows')).to_contain_text('PASSED tests/test_upstream.py', timeout=10000)
            await expect(tool.locator('.sx-pane .xterm-rows')).to_contain_text('click-me done')
            assert await tool.locator('.sx-pane a').count() == 0
            assert await page.evaluate('window.__clipboardWrites') == 0
            await expect(detail.locator('.sx-hint')).to_contain_text('/rook-move')
            await expect(detail.locator('[data-act=stop]')).to_be_hidden()
            # Send to a held inbox.
            await detail.locator('#sx-send-text').fill('Also check keepalive.')
            await detail.locator('.sx-send button').click()
            await expect(detail.locator('.sx-send-note')).to_have_text('Waiting for approval on test-host.')
            assert band.sent[-1]['native_id'] == MIRRORED and band.sent[-1]['text'] == 'Also check keepalive.'
            # Link to task.
            await detail.locator('.sx-d-link [name=task]').fill('t_0123abcd')
            await detail.locator('.sx-d-link button').click()
            await expect(detail.locator('.sx-link-note')).to_have_text('Linked.')
            await expect(item('Fix the nginx upstream timeouts')).to_contain_text('task t_0123abcd')
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-mirror.png'))

            # Tier 3: transcript of a Codex session; a message arrives as a turn.
            await item('Port the parser to Rust').click()
            await expect(detail.locator('h3')).to_have_text('Port the parser to Rust')
            await expect(detail.locator('.sx-msg.sx-assistant')).to_contain_text('Done: src/lexer.rs compiles.')
            await expect(detail.locator('.sx-tool .sx-pane .xterm-rows')).to_contain_text('14 passed', timeout=10000)
            await detail.locator('#sx-send-text').fill('Next: the parser.')
            await detail.locator('#sx-send-text').press('Enter')
            await expect(detail.locator('.sx-send-note')).to_have_text('Delivered as a new turn.')
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-transcript.png'))

            # New session: a shell in a Rook terminal (tier 1).
            await page.locator('#sx-new').click()
            form = page.locator('#sx-form')
            await expect(form.locator('[name=harness] option')).to_have_count(2)
            await form.locator('[name=harness]').select_option('shell')
            await form.locator('[name=cwd]').fill(str(project))
            await form.locator('[name=title]').fill('Build the thing')
            await form.locator('[name=task]').fill('t_feedbeef')
            await form.locator('[name=mcp]').uncheck()
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-new.png'))
            await form.locator('button[type=submit]').click()
            await expect(form).to_be_hidden(timeout=15000)
            await expect(detail.locator('h3')).to_have_text('Build the thing')
            term = detail.locator('.sx-term')
            await expect(term.locator('.xterm-rows')).to_contain_text('$', timeout=10000)
            await term.locator('.xterm').click()
            await page.keyboard.type('echo hello-$((6*7))\n')
            await expect(term.locator('.xterm-rows')).to_contain_text('hello-42', timeout=10000)
            await expect(term.locator('.sx-holder')).to_contain_text('You have control')
            await expect(item('Build the thing')).to_contain_text('task t_feedbeef', timeout=15000)
            # Started for a task: the task is claimed and the terminal linked to it.
            assert [w['action'] for w in task_writes if w['id'] == 't_feedbeef'][:2] == ['claim', 'link']
            # Send into a Rook terminal types keys.
            await detail.locator('#sx-send-text').fill('echo typed-$((2+3))')
            await detail.locator('.sx-send button').click()
            await expect(detail.locator('.sx-send-note')).to_have_text('Typed into the Rook terminal.')
            await expect(term.locator('.xterm-rows')).to_contain_text('typed-5', timeout=10000)
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-terminal.png'))
            # A second viewer watches; control is handed over.
            page2 = await ctx.new_page()
            page2.on('pageerror', lambda e: errors.append(str(e)))
            await page2.goto(url + '#sessions')
            await page2.locator('.sx-item', has_text='Build the thing').click()
            term2 = page2.locator('#sx-detail .sx-term')
            await expect(term2.locator('.xterm-rows')).to_contain_text('hello-42', timeout=10000)
            await expect(term2.locator('.sx-holder')).to_contain_text('has control')
            await expect(term.locator('.sx-holder')).to_contain_text('2 watching')
            await term.locator('[data-t=handoff]').select_option(index=1)
            await expect(term2.locator('.sx-holder')).to_contain_text('You have control')
            await term2.locator('.xterm').click()
            await page2.keyboard.type('echo from-two-$((1+1))\n')
            await expect(term.locator('.xterm-rows')).to_contain_text('from-two-2', timeout=10000)
            await term.locator('[data-t=take]').click()
            await expect(term.locator('.sx-holder')).to_contain_text('You have control')
            await page2.close()
            # Stop a Rook-started session.
            page.once('dialog', lambda d: asyncio.ensure_future(d.accept()))
            await detail.locator('[data-act=stop]').click()
            await expect(term.locator('.sx-term-note')).to_contain_text('Process exited', timeout=10000)
            assert any(c == 'sessions.stop' for c, _ in band.calls)

            # Resume a closed session into a Rook terminal.
            await item('Write the release notes').click()
            await expect(detail.locator('h3')).to_have_text('Write the release notes')
            await expect(detail.locator('.sx-send')).to_be_hidden()
            await detail.locator('[data-act=resume]').click()
            await expect(detail.locator('.sx-term .xterm-rows')).to_contain_text('--resume ' + CLOSED, timeout=15000)
            await expect(item('Write the release notes').locator('.sx-dot')).to_have_class('sx-dot sx-live', timeout=15000)
            await expect(detail.locator('[data-act=stop]')).to_be_visible()
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-resumed.png'))

            # Phone width: no horizontal scroll; the open session replaces the list.
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.wait_for_timeout(300)
            overflow = await page.evaluate('document.documentElement.scrollWidth - innerWidth')
            assert overflow <= 1, overflow
            await expect(page.locator('#sx-list')).to_be_hidden()
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-mobile-detail.png'))
            await detail.locator('[data-act=back]').click()
            await expect(page.locator('#sx-list')).to_be_visible()
            overflow = await page.evaluate('document.documentElement.scrollWidth - innerWidth')
            assert overflow <= 1, overflow
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-mobile.png'), full_page=True)
            await page.set_viewport_size({'width': 1440, 'height': 1000})

            # The classic Codex view is one link away, remembered per browser, and back.
            await page.locator('#sx-classic').click()
            await expect(page.locator('.work-layout')).to_be_visible()
            await page.reload()
            await expect(page.locator('.work-layout')).to_be_visible()
            await page.locator('#work-sessions').click()
            await expect(page.locator('.sx')).to_be_visible()
            assert not errors, errors
            await browser.close()
        for t in list(plugin.terms.values()):
            await plugin.caps()['work.stream.close'](id=t.id)
        await plugin.stop()
        print('sessions browser test: ok')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    args = parser.parse_args()
    if args.screenshots:
        os.makedirs(args.screenshots, exist_ok=True)
    asyncio.run(main(args.screenshots))
