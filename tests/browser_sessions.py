"""Browser test for the Sessions page (rook/web/sessions.js).

Runs the dashboard against an in-process worker: a real TerminalsPlugin (so
real shells under a PTY) plus scripted ``sessions.*`` caps for sessions Rook
did not start (a mirrored Claude Code session whose inbox holds messages, a
Codex session with only a transcript from a worker without ``tail=``, a closed
Claude session with a 400-message transcript read by the real
claude-history follow, a session that may still be running) and a second,
slow worker. Drives it with Playwright Chromium: the list as each host
answers and the hub's cached copy, filters, a stale host notice, the live
mirror view and transcripts as one read-only xterm.js each (ANSI text and
styles, no links or clipboard writes), tail-first opening, no Resume for a
session that may still run, New session into a live terminal (type, second
viewer, hand off), Send (held, turn and keys), Link to task, Resume into a
Rook terminal, Stop, the classic-view link and the phone layout.

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
from rook.worker.plugins import claude_history, terminals
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin
from rook.worker.plugins.terminals import TerminalsPlugin

MIRRORED = '6f1c0a52-0000-4000-8000-00000000a001'
CODEX = '019a2b3c-0000-7000-8000-00000000c002'
CLOSED = '8f3a1b2c-0000-4000-8000-000000000001'
MAYBE = '9a9a9a9a-0000-4000-8000-0000000000d4'
TURNS = 100     # the closed session's transcript: 4 messages a turn, 400 in all


def write_transcript(path, cwd):
    """A long Claude Code transcript: prompt, a Bash call, its coloured
    output and a reply, TURNS times. Old enough to resume."""
    import json
    lines = []
    for n in range(TURNS):
        tid = f'toolu_{n:04d}'
        lines += [
            {'type': 'user', 'cwd': cwd, 'message': {'role': 'user', 'content': f'Step {n}: run the release checks'}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': f'make check-{n}', 'description': 'checks'}}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': tid, 'content': f'\x1b[32mok\x1b[0m check-{n} passed\nline two\nline three'}]}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': f'Step {n} is green.' if n < TURNS - 1 else 'Release notes drafted in NOTES.md.'}]}},
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in lines))
    old = time.time() - 3600
    os.utime(path, (old, old))
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
        self.host2_delay = 0.0
        self.sent = []
        self.mirror_calls = 0
        self.follows = []
        self.history = ClaudeHistoryPlugin()     # the real follow, on a transcript in the temp dir

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
                                     messages=TURNS * 4, view={'terminal': None, 'mirror': False, 'transcript': True},
                                     input='none', inbox_policy='unknown', links={}, resumable=True),
            # No process evidence, but its log changed a minute ago: maybe still running.
            ('claude', MAYBE): dict(agent='claude', native_id=MAYBE, title='Tune the cache headers',
                                    cwd='/srv/web', state='live', origin='external', updated=now - 60,
                                    messages=8, view={'terminal': None, 'mirror': False, 'transcript': True},
                                    input='none', inbox_policy='unknown', links={}, resumable=False,
                                    possibly_live='recent_write'),
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
            await asyncio.sleep(self.host2_delay)
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
            self.follows.append(dict(args))
            if args['agent'] == 'claude':
                out = self.history._follow(session_id=args['native_id'], offset=args.get('offset', 0),
                                           version=args.get('version', ''), tail=args.get('tail', 0))
                return dict(out, agent='claude', native_id=args['native_id'])
            # Codex: an older worker's reply (no tail=), so the page's fallback runs.
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
        claude_root = temp / 'claude-projects'
        patch.setattr(claude_history, '_default_root', lambda: claude_root)
        patch.setattr(ClaudeHistoryPlugin, '_default_root', staticmethod(lambda: claude_root))
        write_transcript(claude_root / '-project' / f'{CLOSED}.jsonl', str(project))
        (claude_root / '-srv-web' ).mkdir(parents=True)
        (claude_root / '-srv-web' / f'{MAYBE}.jsonl').write_text(
            '{"type": "user", "message": {"role": "user", "content": "Set max-age on the assets"}}\n')
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
            # A slow host does not hold up the list: each host's rows appear as it answers.
            band.host2_delay = 5.0
            await page.goto(url + '#sessions')

            # The list: grouped by host then project, live first.
            await expect(page.locator('.sx')).to_be_visible()
            item = lambda text: page.locator('.sx-item', has_text=text)
            await expect(item('Fix the nginx upstream timeouts')).to_be_visible()
            await expect(page.locator('#sx-status')).to_have_text('Asking laptop…')
            assert await item('Laptop notes').count() == 0
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-list-progressive.png'))
            await expect(item('Laptop notes')).to_be_visible(timeout=10000)
            await expect(page.locator('#sx-status')).to_contain_text('Updated')
            # Reopened: the hub's last copy of every host shows at once (cached=1).
            await page.reload()
            await expect(item('Laptop notes')).to_be_visible(timeout=1500)
            await expect(page.locator('#sx-status')).to_have_text('Asking laptop…')
            band.host2_delay = 0.0
            await expect(page.locator('#sx-status')).to_contain_text('Updated', timeout=10000)
            hosts = page.locator('.sx-host-head')
            await expect(hosts.first).to_contain_text('test-host')
            await expect(hosts.nth(1)).to_contain_text('laptop')
            first_project = page.locator('.sx-host').first.locator('.sx-project').first
            await expect(first_project.locator('.sx-project-head strong')).to_have_text('project')
            await expect(first_project.locator('.sx-item').first).to_contain_text('Fix the nginx upstream timeouts')
            await expect(first_project.locator('.sx-item').last).to_contain_text('Write the release notes')
            await expect(page.locator('#sx-counts')).to_have_text('2 live · 1 idle · 2 closed')
            # Filters: agent, host, live only, search.
            await page.locator('#sx-agent').select_option('codex')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await page.locator('#sx-agent').select_option('')
            await page.locator('#sx-host').select_option('host2')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await expect(page.locator('.sx-item')).to_contain_text('Laptop notes')
            await page.locator('#sx-host').select_option('')
            await page.locator('#sx-live').check()
            await expect(page.locator('.sx-item')).to_have_count(3)
            await page.locator('#sx-live').uncheck()
            await page.locator('#sx-search').fill('release')
            await expect(page.locator('.sx-item')).to_have_count(1)
            await page.locator('#sx-search').fill('')
            await expect(page.locator('.sx-item')).to_have_count(5)
            # A host that stops answering stays listed from the hub's cache, with a notice.
            band.host2_down = True
            await page.evaluate("document.querySelector('#sx-live').dispatchEvent(new Event('change'))")
            await expect(page.locator('.sx-notice')).to_contain_text('laptop did not answer')
            await expect(page.locator('.sx-host-head', has_text='laptop')).to_contain_text('stale')
            await expect(item('Laptop notes')).to_be_visible()
            band.host2_down = False

            # Tier 2: the mirror view of a session started outside Rook, as a
            # read-only terminal that looks like Claude Code's own.
            await item('Fix the nginx upstream timeouts').click()
            detail = page.locator('#sx-detail')
            await expect(detail.locator('h3')).to_have_text('Fix the nginx upstream timeouts')
            screen = detail.locator('.sx-screen')
            rows = screen.locator('.xterm-rows')
            await expect(rows).to_contain_text('> Why do upstream requests time out?', timeout=10000)
            await expect(rows).to_contain_text('● Checking the proxy timeouts first.')
            await expect(rows).to_contain_text('● Bash(pytest tests/test_upstream.py)')
            await expect(rows).to_contain_text('⎿  PASSED tests/test_upstream.py')
            await expect(rows).to_contain_text('click-me done')
            await expect(rows).to_contain_text('● proxy_read_timeout is 5s.')
            await expect(rows).to_contain_text('────────')
            await expect(rows).to_contain_text('✻ Waiting for approval on test-host')
            await expect(detail.locator('.sx-state')).to_have_text('waiting for approval on test-host')
            assert await screen.count() == 1 and await detail.locator('.sx-msg, .sx-tool, .sx-pane').count() == 0
            text = await screen.evaluate('el => el.sxScreen.text()')
            # Streamed pieces and the final message are one block, written once.
            assert text.count('Checking the proxy timeouts first.') == 1, text
            assert text.count('proxy_read_timeout is 5s.') == 1, text
            # ANSI: bold prompt, green tool bullet, dim output, green tool colour kept.
            styles = await screen.evaluate("""el => {
              const b = el.sxScreen.term.buffer.active, cell = (y, x) => b.getLine(y).getCell(x), out = {};
              for (let y = 0; y < b.length; y++) {
                const line = b.getLine(y).translateToString(true);
                if (line.startsWith('> Why')) out.prompt = !!cell(y, 2).isBold();
                if (line.startsWith('● Bash')) out.call = [cell(y, 0).getFgColor(), !!cell(y, 2).isBold()];
                if (line.includes('PASSED')) { const x = line.indexOf('PASSED'); out.passed = [cell(y, x).getFgColor(), !!cell(y, x).isDim(), !!cell(y, x + 8).isDim()]; }
              }
              out.stdin = el.sxScreen.term.options.disableStdin; out.scrollback = el.sxScreen.term.options.scrollback;
              return out; }""")
            assert styles['prompt'] and styles['call'] == [2, True] and styles['passed'][0] == 2 and styles['passed'][1] and styles['passed'][2], styles
            assert styles['stdin'] is True and styles['scrollback'] == 10000, styles
            assert await screen.locator('a').count() == 0
            assert await page.evaluate('window.__clipboardWrites') == 0
            await expect(detail.locator('.sx-hint')).to_contain_text('/rook-move')
            await expect(detail.locator('[data-act=stop]')).to_be_hidden()
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-mirror.png'))
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

            # Tier 3: transcript of a Codex session (a worker without tail=); a message arrives as a turn.
            await item('Port the parser to Rust').click()
            await expect(detail.locator('h3')).to_have_text('Port the parser to Rust')
            rows = detail.locator('.sx-screen .xterm-rows')
            await expect(rows).to_contain_text('› Port the tokenizer first.', timeout=10000)
            await expect(rows).to_contain_text('• Done: src/lexer.rs compiles.')
            await detail.locator('#sx-send-text').fill('Next: the parser.')
            await detail.locator('#sx-send-text').press('Enter')
            await expect(detail.locator('.sx-send-note')).to_have_text('Delivered as a new turn.')
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-transcript-codex.png'))

            # Tier 3, tail first: a 400-message Claude transcript opens at its
            # last 40 messages in one request, drawn like Claude Code.
            band.follows.clear()
            await item('Write the release notes').click()
            await expect(detail.locator('h3')).to_have_text('Write the release notes')
            screen = detail.locator('.sx-screen')
            rows = screen.locator('.xterm-rows')
            await expect(rows).to_contain_text('● Release notes drafted in NOTES.md.', timeout=10000)
            await expect(rows).to_contain_text(f'● Bash(make check-{TURNS - 1})')
            await expect(rows).to_contain_text(f'⎿  ok check-{TURNS - 1} passed')
            await expect(detail.locator('.sx-log-note')).to_be_hidden()
            first = band.follows[0]
            assert first['tail'] == 40 and first['offset'] == 0, band.follows
            assert not any(f.get('offset') and f['offset'] < TURNS * 4 - 40 for f in band.follows), band.follows
            text = await screen.evaluate('el => el.sxScreen.text()')
            assert f'{TURNS * 4 - 40} earlier messages not shown' in text, text
            assert f'Step {TURNS - 11}:' not in text and f'> Step {TURNS - 1}: run the release checks' in text, text
            # Tool results are output under their call, not prompts.
            assert '> ok check' not in text and '> \x1b' not in text
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-transcript.png'))

            # A session whose log changed in the last 2 minutes may still be running: no Resume.
            await item('Tune the cache headers').click()
            await expect(detail.locator('h3')).to_have_text('Tune the cache headers')
            await expect(detail.locator('.sx-hint')).to_contain_text('may still be running on test-host')
            await expect(detail.locator('[data-act=resume]')).to_be_hidden()
            await expect(item('Tune the cache headers')).to_contain_text('maybe running')

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
            # The live view's terminal fits a phone too.
            await item('Fix the nginx upstream timeouts').click()
            rows = detail.locator('.sx-screen .xterm-rows')
            await expect(rows).to_contain_text('Why do upstream', timeout=10000)
            await detail.locator('.sx-screen').scroll_into_view_if_needed()
            overflow = await page.evaluate('document.documentElement.scrollWidth - innerWidth')
            assert overflow <= 1, overflow
            if shots:
                await page.screenshot(path=str(Path(shots) / 'sessions-mobile-mirror.png'))
            await detail.locator('[data-act=back]').click()
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
