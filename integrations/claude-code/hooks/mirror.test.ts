import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

type TestEngine = Parameters<TestBody>[0]

import {
  CHUNK_BYTES,
  chunkIndex,
  clip,
  emit,
  emitDelta,
  endMirror,
  inboundOf,
  lastSeq,
  newMirror,
  promptFrom,
  settleMirror,
  startMirror,
  toolInput,
  toolOutput,
  utf8Bytes,
} from './mirror'
import type { MirrorIO } from './mirror'

const STATE = '/home/user/.rook-band-worker'
const FOLDER = `${STATE}/mirror/claude`
const SID = 'aaaa1111-0000-4000-8000-000000000001'

/** An in-memory host: files, env, a clock whose timers run when asked. */
function fakeHost(files: Record<string, string> = {}) {
  const timers: Array<() => void> = []
  const runs: string[][] = []
  const io: MirrorIO = {
    env: async name => ({ HOME: '/home/user' })[name as 'HOME'],
    exists: async path => path === STATE || path in files,
    list: async path => {
      const names = Object.keys(files)
        .filter(name => name.startsWith(`${path}/`))
        .map(name => ({ name: name.slice(path.length + 1) }))
      if (names.length === 0) throw new Error('ENOENT')

      return names
    },
    read: async path => {
      const text = files[path]
      if (text === undefined) throw new Error('ENOENT')

      return text
    },
    write: async (path, text) => {
      files[path] = text
    },
    run: async argv => {
      runs.push([...argv])
    },
    now: async () => 1_700_000_000_000,
    after: (_, fn) => void timers.push(fn),
    sessionId: async () => SID,
    cwd: async () => '/home/user/proj',
    model: async () => 'opus',
    version: async () => '2.1.290',
    settings: async () => ({ crossSessionInbound: 'hold' }),
  }

  return { io, files, runs, tick: () => timers.splice(0).forEach(fn => fn()) }
}

const events = (text: string | undefined) =>
  (text ?? '')
    .split('\n')
    .filter(line => line !== '')
    .map(line => JSON.parse(line) as Record<string, unknown>)

test('helpers: clipping, chunk names, the last seq, who a prompt is from', async () => {
  expect(clip('abcdef', 4)).toBe('abcd… [+2 chars]')
  expect(clip('abc', 4)).toBe('abc')
  expect(utf8Bytes('aé✓😀')).toBe(1 + 2 + 3 + 4)
  expect(chunkIndex(`${SID}.jsonl`, SID)).toBe(0)
  expect(chunkIndex(`${SID}.12.jsonl`, SID)).toBe(12)
  expect(chunkIndex('other.1.jsonl', SID)).toBeUndefined()
  expect(lastSeq('{"seq":1}\n{"seq":2}\n{"seq":3, "ty')).toBe(2)
  expect(lastSeq('')).toBe(0)
  expect(promptFrom('composer')).toBe('person')
  expect(promptFrom('peer')).toBe('peer')
  expect(promptFrom('peer-send-message')).toBe('peer')
  expect(inboundOf({ crossSessionInbound: 'accept' })).toBe('accept')
  expect(inboundOf({ crossSessionInbound: 'sideways' })).toBe('default')
  expect(inboundOf(undefined)).toBe('default')
})

test('tool inputs and results are clipped, never expanded, and keep no reserved keys', async () => {
  const input = toolInput({ tool: 'Bash', tool_use_id: 'tu1', agentId: undefined, command: 'x'.repeat(5000) })
  expect(input.startsWith('{"command":"xxx')).toBe(true)
  expect(input.length).toBeLessThan(2_100)
  expect(input).not.toContain('tool_use_id')
  expect(toolOutput({ text: 'y'.repeat(9000) }).text.length).toBeLessThan(4_100)
  expect(toolOutput({ deny: 'no' })).toEqual({ ok: false, text: 'no' })
  expect(toolOutput({ text: 'boom', isError: true }).ok).toBe(false)
  expect(toolOutput({ result: { files: 2 } }).text).toBe('{"files":2}')
})

test('a session writes its spool in order, batched, and ends it', async () => {
  const host = fakeHost()
  const m = newMirror()
  startMirror(host.io, m, '/home/user/proj')
  emit(host.io, m, { type: 'prompt', text: 'hi', from: 'person' }) // while the spool opens
  await settleMirror(host.io, m)
  emitDelta(host.io, m, 'Hel')
  emitDelta(host.io, m, 'lo')
  emit(host.io, m, { type: 'assistant.done', text: 'Hello' })
  host.tick()
  await settleMirror(host.io, m)
  await endMirror(host.io, m, 'prompt_input_exit')
  const got = events(host.files[`${FOLDER}/${SID}.jsonl`])
  expect(got.map(one => one.type)).toEqual(['session.start', 'prompt', 'assistant.delta', 'assistant.done', 'session.end'])
  expect(got.map(one => one.seq)).toEqual([1, 2, 3, 4, 5])
  expect(got[0]).toMatchObject({ v: 1, cwd: '/home/user/proj', model: 'opus', version: '2.1.290', inbound: 'hold', pid: null })
  expect(got[2]?.text).toBe('Hello') // the two pieces as one delta
  expect(typeof got[0]?.ts).toBe('number')
  // The folder is made owner-only before the first write, each chunk 0600.
  expect(host.runs[0]?.slice(0, 3)).toEqual(['sh', '-c', 'umask 077 && mkdir -p "$1" && chmod 700 "$2" "$1"'])
  expect(host.runs[1]).toEqual(['chmod', '600', `${FOLDER}/${SID}.jsonl`])
  // After an exit nothing more is written.
  emit(host.io, m, { type: 'state', state: 'idle' })
  expect(events(host.files[`${FOLDER}/${SID}.jsonl`]).length).toBe(5)
})

test('the spool rotates into chunks and a reload carries on from the last seq', async () => {
  const host = fakeHost()
  const m = newMirror()
  startMirror(host.io, m, '/w')
  await settleMirror(host.io, m)
  const big = 'z'.repeat(40_000)
  for (let i = 0; i < 8; i++) emit(host.io, m, { type: 'assistant.done', text: big })
  await settleMirror(host.io, m)
  const first = host.files[`${FOLDER}/${SID}.jsonl`] ?? ''
  const second = events(host.files[`${FOLDER}/${SID}.1.jsonl`])
  expect(utf8Bytes(first)).toBeLessThan(CHUNK_BYTES + 1)
  expect(second.length).toBeGreaterThan(0)
  expect(events(first).length + second.length).toBe(9)
  // A hot reload starts a new mirror over the same spool: seq goes on.
  const again = newMirror()
  startMirror(host.io, again, '/w')
  await settleMirror(host.io, again)
  const after = events(host.files[`${FOLDER}/${SID}.1.jsonl`])
  expect(after[after.length - 1]).toMatchObject({ type: 'session.start', seq: 10 })
})

test('no worker on this host: nothing is written', async () => {
  const host = fakeHost()
  const io: MirrorIO = { ...host.io, exists: async () => false }
  const m = newMirror()
  startMirror(io, m, '/w')
  emit(io, m, { type: 'prompt', text: 'hi' })
  await settleMirror(io, m)
  expect(Object.keys(host.files)).toEqual([])
})

// ---- through the engine: the hooks, /rook-move and the Sessions tab

const ROSTER = JSON.stringify([
  { worker_id: 'a1', name: 'nova', description: '', band: 'aaaa1111', build: 170, plugins: ['claude-history', 'sessions'], hb: {}, last_seen_age_secs: 3 },
])

const reply = (result: unknown) => JSON.stringify({ ok: true, result })

const MOVE_RUN = {
  command: 'rook-move',
  args: '',
  origin: { kind: 'composer' as const },
  presentation: { isFullscreen: false, columns: 100 },
}

function host($: TestEngine, on: On, caps: Record<string, unknown>) {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  const files: Record<string, string> = {
    [`${STATE}/worker_id`]: 'a1\n',
    '/home/user/.claude/sessions/4242.json': JSON.stringify({ pid: 4242, sessionId: SID }),
  }
  const calls: Array<{ cap: string; args: Record<string, unknown> }> = []
  const commands: string[] = []
  on('mcp.call', { server: 'rook' }, async (_, e) => {
    const cap = String(e.args.cap ?? e.tool)
    calls.push({ cap, args: (e.args.args ?? e.args) as Record<string, unknown> })
    const text =
      cap === 'rook_workers' ? ROSTER
      : cap === 'rook_knowledge' ? JSON.stringify({ ok: true, result: [] })
      : reply(caps[cap] ?? { ok: false, error: `no such capability: ${cap}` })

    return { value: { content: [{ type: 'text', text }], isError: false } }
  })
  on('env.get', async (_, e) => ({ value: (e as { name: string }).name === 'HOME' ? '/home/user' : undefined }) as never)
  on('fs.exists', async (_, e) => ({ value: e.path === STATE || e.path in files }))
  on('fs.read', async (_, e) => {
    const text = files[e.path]

    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('fs.write', async (_, e) => {
    files[e.path] = e.text

    return { value: undefined }
  })
  on('fs.list', async (_, e) => {
    const names = Object.keys(files).filter(name => name.startsWith(`${e.path}/`))
    if (names.length === 0) return { deny: 'ENOENT' }

    return {
      value: names.map(name => ({ name: name.slice(e.path.length + 1), kind: 'file', size: 1, mtimeMs: 0, isLink: false })),
    } as never
  })
  on('process.run', async () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }) as never)
  on('session.id', async () => ({ value: SID }))
  on('session.cwd', async () => ({ value: '/home/user/proj' }))
  on('command.list', async () => ({ value: [{ name: 'exit', description: 'Exit', source: 'builtin' }] }) as never)
  on('command.run', { command: 'exit' }, async () => {
    commands.push('exit')

    return { text: 'bye' }
  })

  return { clock, files, calls, commands }
}

test('the hooks mirror a session through the engine, from start to end', async ($, on) => {
  const { clock, files } = host($, on, {})
  on('command.register', async (_, e) => ({ value: { command: e.name } }) as never)
  on('tool.register', async () => ({ value: {} }) as never)
  on('ui.open', async () => ({ value: { isPlaced: false } }) as never)
  on('session.start', async (_, e) => ({ cwd: e.cwd }))
  on('turn.complete', async (_, e) => ({ text: e.answer }))
  on('session.end', async (_, e) => ({ sessionId: e.sessionId }))
  await $.session.start({ cwd: '/home/user/proj', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.turn.complete({ answer: 'done', durationMs: 5, isAborted: false, turnId: 'u1', reason: 'answer' } as never)
  await $.session.end({ reason: 'prompt_input_exit', sessionId: SID, resume: { id: SID } })
  const got = events(files[`${FOLDER}/${SID}.jsonl`])
  expect(got.map(one => one.type)).toEqual(['session.start', 'turn.end', 'state', 'session.end'])
  expect(got[0]).toMatchObject({ cwd: '/home/user/proj', pid: 4242 })
  expect(got[3]).toMatchObject({ reason: 'prompt_input_exit', seq: 4 })
})

test('/rook-move asks first, then hands the session to a Rook terminal and exits', async ($, on) => {
  const { clock, calls, commands } = host($, on, { 'work.stream.open': { ok: true, id: 't1', waiting: true } })
  const asked = await $.command.run(MOVE_RUN)
  expect(asked.text).toMatch(/Move to a Rook terminal on nova\?/)
  expect(calls.some(one => one.cap === 'work.stream.open')).toBe(false)
  const ui = await $.ui.mount({ plugin: 'rook', surface: 'terminal', component: 'Pane', requestId: 'rook-bands', props: { bodyColumns: 60 } as never })
  expect(await ui.find({ type: 'Text', text: /Resume it on nova in ~\/proj/ })).toBeDefined()
  await ui.press({ key: 'move-yes' })
  const open = calls.find(one => one.cap === 'work.stream.open')
  expect(open?.args).toEqual({ harness: 'claude', resume: SID, cwd: '/home/user/proj', handoff_pid: 4242 })
  expect(await ui.find({ type: 'Text', text: /continues in Rook terminal t1 on nova/ })).toBeDefined()
  await clock.advance(2_000)
  expect(commands).toEqual(['exit'])
  await ui.unmount()
})

test('/rook-move says why it cannot move: an old worker, or no worker here', async ($, on) => {
  const { files, calls } = host($, on, {})
  await $.command.run(MOVE_RUN)
  const ui = await $.ui.mount({ plugin: 'rook', surface: 'terminal', component: 'Pane', requestId: 'rook-bands', props: { bodyColumns: 60 } as never })
  await ui.press({ key: 'move-yes' })
  expect(calls.some(one => one.cap === 'work.stream.open')).toBe(true)
  expect(await ui.find({ type: 'Text', text: /cannot take a session over yet/ })).toBeDefined()
  delete files[`${STATE}/worker_id`]
  const again = await $.command.run(MOVE_RUN)
  expect(again.text).toMatch(/not enrolled/)
  await ui.unmount()
})

test('the sessions tab reads sessions.list where the worker has it', async ($, on) => {
  const { calls } = host($, on, {
    'sessions.list': {
      ok: true,
      items: [
        { agent: 'claude', native_id: 'c1', title: 'Voice replies', state: 'live', updated: 1_699_999_000, messages: 12, cwd: '/home/user/proj', input: 'inbox' },
        { agent: 'shell', native_id: 't9', title: 'a shell', state: 'live', updated: 1_699_999_000, messages: 0, cwd: '/', input: 'pty' },
      ],
    },
  })
  const ui = await $.ui.mount({ plugin: 'rook', surface: 'terminal', component: 'Pane', requestId: 'rook-bands', props: { bodyColumns: 60 } as never })
  await ui.press({ key: 'refresh' })
  await ui.press({ key: 'tab:sessions' })
  expect(await ui.find({ key: 's:a1:claude:c1' })).toBeDefined()
  expect(await ui.find({ key: 's:a1:shell:t9' })).toBeUndefined()
  expect(calls.some(one => one.cap === 'claude-history.pull')).toBe(false)
  await ui.unmount()
})

test('an older worker falls back to claude-history.pull', async ($, on) => {
  const { calls } = host($, on, {
    'claude-history.pull': { ok: true, sessions: [{ session_id: 's1', title: 'Old path', last_modified: 1_699_999_000, message_count: 3 }] },
  })
  const ui = await $.ui.mount({ plugin: 'rook', surface: 'terminal', component: 'Pane', requestId: 'rook-bands', props: { bodyColumns: 60 } as never })
  await ui.press({ key: 'refresh' })
  await ui.press({ key: 'tab:sessions' })
  expect(await ui.find({ key: 's:a1:claude:s1' })).toBeDefined()
  expect(calls.map(one => one.cap)).toContain('sessions.list')
  await ui.unmount()
})
