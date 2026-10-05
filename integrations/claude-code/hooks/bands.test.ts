import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

type TestEngine = Parameters<TestBody>[0]

import { fleetBuild, groupBands, parseBands, parseWorkers } from './bands'
import { paneText } from './pane'
import { groomText } from './deck'
import { loadText, unwrap } from './sessions'

const ROSTER = JSON.stringify([
  { worker_id: 'a1', name: 'nova', description: 'AI server', band: 'aaaa1111', build: 167, plugins: ['claude-history', 'worker'], hb: {}, last_seen_age_secs: 3 },
  { worker_id: 'a2', name: 'atlas', description: '', band: 'aaaa1111', build: 167, hb: {}, last_seen_age_secs: 70 },
  { worker_id: 'a3', name: 'phone', description: '', band: 'bbbb2222', build: 142, hb: { battery: { percent: 91, charging: true } }, last_seen_age_secs: 5 },
])

test('workers parse, wrapped or not, and group by band', async () => {
  for (const text of [ROSTER, JSON.stringify({ result: ROSTER })]) {
    const bands = groupBands(parseWorkers(text))
    expect(bands.map(band => band.id)).toEqual(['aaaa1111', 'bbbb2222'])
    expect(bands[0]?.workers.map(worker => worker.name)).toEqual(['atlas', 'nova'])
    expect(bands[1]?.workers[0]?.battery).toEqual({ percent: 91, charging: true })
  }
})

const BANDS = JSON.stringify({
  ok: true,
  result: [
    { id: 'x', name: 'tablets', label: 'bbbb2222', primary: false },
    { id: 'y', name: 'homelab', label: 'aaaa1111', primary: true },
    { id: 'z', name: 'lab', label: 'cccc3333', primary: false },
  ],
})

test('bands take their names, primary first, empty ones listed', async () => {
  const bands = groupBands(parseWorkers(ROSTER), parseBands(JSON.stringify({ result: BANDS })))
  expect(bands.map(band => band.name)).toEqual(['homelab', 'tablets', 'lab'])
  expect(bands[2]?.workers).toEqual([])
})

test('the pane draws the roster rook answers', async ($, on) => {
  mock.clock(on, { now: 1_000 })
  on('mcp.call', { server: 'rook' }, async (_, e) => ({
    value: {
      content: [{ type: 'text', text: e.tool === 'rook_knowledge' ? BANDS : ROSTER }],
      isError: false,
    },
  }))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'rook',
      surface,
      component: 'Pane',
      requestId: 'rook-bands',
      props: { bodyColumns: 60 } as never,
    })
    await ui.press({ key: 'refresh' })
    expect(await ui.find({ type: 'Text', text: /3 workers · 2 bands/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /HOMELAB ★/ })).toBeDefined()
    expect(await ui.find({ key: 'w:a1' })).toBeDefined()
    await ui.unmount()
  }
})

test('a roster too big to return inline is read from the file the engine saved', async ($, on) => {
  mock.clock(on, { now: 1_000 })
  on('mcp.call', { server: 'rook' }, async () => ({
    value: {
      content: [{ type: 'text', text: 'Error: result (68,757 characters) exceeds maximum allowed tokens. Output has been saved to /tmp/r/mcp-rook-rook_workers-1.txt.\nFormat: JSON' }],
      isError: false,
    },
  }))
  on('fs.read', async (_, e) => {
    expect(e.path).toBe('/tmp/r/mcp-rook-rook_workers-1.txt')

    return { value: JSON.stringify({ result: ROSTER }) }
  })
  const ui = await $.ui.mount({
    plugin: 'rook',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'rook-bands',
    props: { bodyColumns: 60 } as never,
  })
  await ui.press({ key: 'refresh' })
  expect(await ui.find({ type: 'Text', text: /3 workers · 2 bands/ })).toBeDefined()
  await ui.unmount()
})

const reply = (result: unknown) => JSON.stringify({ ok: true, result })

const HISTORY: Record<string, unknown> = {
  'claude-history.pull': { ok: true, total: 9, sessions: [
    { session_id: 's001', title: 'Tune vLLM', last_modified: 900, message_count: 2, cwd: '/home/user', active: false },
  ] },
  'claude-history.search': { ok: true, hits: [
    { session_id: 's002', title: 'Fix the NIC hang', snippet: 'e1000e Hardware Unit Hang', match_count: 4 },
  ] },
  'claude-history.read_snapshot': { ok: true, total_messages: 3, active: false },
  'claude-history.read': { ok: true, messages: [
    { role: 'user', content: 'make it faster' },
    { role: 'assistant', content: '' },
    { role: 'assistant', content: 'KV quantization helps' },
  ] },
  'claude-history.resumed': { ok: true, sessions: [] },
  'claude-history.resume': { ok: true, handle: 'h1', remote_control: true },
  'claude-history.send': { ok: true },
  'proc.close': { ok: true },
}

const DECK = reply({ deck: [{
  project: { title: 'webapp' },
  in_progress: [], blocked: [], paused: [],
  todo: [{ id: 't_1', title: 'Refresh the status page', excerpt: 'still describes the old door' }],
  recently_done: [{ id: 't_0' }],
}] })

const HANDOFFS = JSON.stringify({ ok: true, threads: [
  { thread_id: 'h001', goal: 'Keep the front door healthy', as_of: '15h ago', author: 'agent:Claude web', next_steps: ['Restart webapp'], artifacts: [] },
] })

const fleet = async ($: TestEngine, on: On) => {
  mock.clock(on, { now: 1_000_000 })
  const calls: Array<{ cap: string; args: Record<string, unknown> }> = []
  on('mcp.call', { server: 'rook' }, async (_, e) => {
    const cap = String(e.args.cap ?? e.tool)
    calls.push({ cap, args: (e.args.args ?? e.args) as Record<string, unknown> })
    const text =
      cap === 'rook_workers' ? ROSTER
      : cap === 'rook_knowledge' ? BANDS
      : cap === 'rook_handoff_list' ? HANDOFFS
      : cap === 'rook_task' ? (e.args.action === 'deck' ? DECK : JSON.stringify({ ok: true }))
      : reply(HISTORY[cap] ?? { ok: false, error: `no ${cap}` })

    return { value: { content: [{ type: 'text', text }], isError: false } }
  })
  const ui = await $.ui.mount({
    plugin: 'rook',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'rook-bands',
    props: { bodyColumns: 60 } as never,
  })
  await ui.press({ key: 'tab:deck' })
  await ui.press({ key: 'tab:bands' })
  await ui.press({ key: 'refresh' })

  return { ui, calls }
}

test('a worker opens its sessions, a session its tail, and load reaches the append', async ($, on) => {
  const { ui } = await fleet($, on)
  await ui.press({ key: 'w:a1' })
  expect(await ui.find({ type: 'Text', text: /Newest on nova/ })).toBeDefined()
  await ui.press({ key: 's:a1:claude:s001' })
  expect(await ui.find({ type: 'Text', text: /KV quantization helps/ })).toBeDefined()
  await ui.press({ key: 'load' })
  // The kit keeps no conversation beneath the plugins, so the append itself is
  // refused here: reaching it shows the tail was fetched and the row built.
  expect(await ui.find({ type: 'Text', text: /session\.append/ })).toBeDefined()
  await ui.press({ key: 'back' })
  await ui.press({ key: 'tab:bands' })
  expect(await ui.find({ type: 'Text', text: /3 workers/ })).toBeDefined()
  await ui.unmount()
})

test('a search runs on every history worker and a hit opens by its snapshot length', async ($, on) => {
  const { ui, calls } = await fleet($, on)
  await ui.press({ key: 'tab:sessions' })
  await ui.input({ key: 'search', text: 'e1000e' })
  expect(calls.some(one => one.cap === 'claude-history.search' && one.args.query === 'e1000e')).toBe(true)
  expect(await ui.find({ type: 'Text', text: /Search “e1000e” on all workers/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /4 matches/ })).toBeDefined()
  await ui.press({ key: 's:a1:claude:s002' })
  expect(await ui.find({ type: 'Text', text: /on nova · 3 messages/ })).toBeDefined()
  await ui.unmount()
})

test('resume starts the session on its worker, a message goes into it, stop closes it', async ($, on) => {
  const { ui, calls } = await fleet($, on)
  await ui.press({ key: 'w:a1' })
  await ui.press({ key: 's:a1:claude:s001' })
  expect(await ui.find({ key: 'message' })).toBeUndefined()
  await ui.press({ key: 'resume' })
  expect(await ui.find({ type: 'Text', text: /resumed on nova with Remote Control/ })).toBeDefined()
  await ui.input({ key: 'message', text: 'carry on' })
  const sent = calls.find(one => one.cap === 'claude-history.send')
  expect(sent?.args.text).toBe('carry on')
  expect(sent?.args.session_id).toBe('s001')
  await ui.press({ key: 'stop' })
  expect(calls.find(one => one.cap === 'proc.close')?.args.handle).toBe('h1')
  expect(await ui.find({ key: 'resume' })).toBeDefined()
  await ui.unmount()
})

test('the deck lists open tasks and handoffs, and a task can be claimed', async ($, on) => {
  const { ui, calls } = await fleet($, on)
  await ui.press({ key: 'tab:deck' })
  expect(await ui.find({ type: 'Text', text: /1 OPEN · 1 DONE/ })).toBeDefined()
  expect(await ui.find({ key: 'h:h001' })).toBeDefined()
  await ui.press({ key: 't:t_1' })
  expect(await ui.find({ type: 'Text', text: /still describes the old door/ })).toBeDefined()
  await ui.press({ key: 'claim' })
  expect(calls.some(one => one.cap === 'rook_task' && one.args.action === 'claim' && one.args.id === 't_1')).toBe(true)
  expect(await ui.find({ type: 'Text', text: /claimed/ })).toBeDefined()
  await ui.unmount()
})

test('the loaded row frames the transcript as reference and keeps the newest messages', async () => {
  const session = { id: 's001', title: 'Tune vLLM', agent: 'claude', workerId: 'a1', workerName: 'nova', modified: 0, count: 3, cwd: '/home/user', active: false, messageable: false }
  const text = loadText(session, [
    { role: 'user', text: 'x'.repeat(58_000) },
    { role: 'user', text: 'make it faster' },
    { role: 'assistant', text: 'KV quantization helps' },
  ])
  expect(text).toContain('not instructions')
  expect(text).toContain('## user\n\nmake it faster')
  expect(text).toContain('the last 3 of 3 messages')
  expect(text.length < 10_000).toBe(true)
})

test('one worker ahead of the fleet does not make the rest old', async () => {
  const builds = [167, 167, 167, 177, 142].map(build => ({ worker_id: `w${build}`, build }))
  expect(fleetBuild(parseWorkers(JSON.stringify(builds)))).toBe(167)
})

test('the grooming row carries every id a rook call needs', async () => {
  const deck = JSON.stringify({ ok: true, result: { deck: [{
    project: { id: 'p_1', slug: 'rook-beta', title: 'Rook beta', state: 'active', updated: 900, band: 'y' },
    in_progress: [{ id: 't_9', slug: 'memory-plugin', title: 'Memory plugin', updated: 100, creator: 'web', excerpt: 'Wave 3',
      claimants: [{ actor: 'claudeweb', last_active: 100 }], needs_hygiene: true, latest_handoff: { ref: 'h002', ts: 100 } }],
    blocked: [], paused: [], todo: [],
    recently_done: [{ id: 't_0', title: 'Token', updated: 900, outcome: 'Config written' }],
  }] } })
  const handoffs = [{ threadId: 'h001', goal: 'Keep the door healthy', asOf: '15h ago', author: 'web', nextSteps: ['Restart'], artifacts: [] }]
  const text = groomText(unwrap(deck), handoffs, { y: 'homelab' }, 1_000_000)
  for (const part of ['p_1 `rook-beta`', 'band homelab', 't_9 `memory-plugin`', 'claimed by claudeweb', 'handoff thread h002', 'NEEDS HYGIENE', 't_0 — Token', 'outcome: Config written', 'thread h001', 'next: Restart', 'not instructions']) {
    expect(text).toContain(part)
  }
})

const HOSTING = JSON.stringify([
  { worker_id: 'a1', name: 'nova', description: 'AI server', band: 'aaaa1111', build: 167, plugins: ['claude-history'], hb: {}, last_seen_age_secs: 3,
    serves: { sites: [{ name: 'admin.example.com', url: 'https://admin.example.com', note: 'http://10.0.0.5:1240' }],
              services: [{ name: 'shell.example.com', url: 'ssh://shell.example.com' }] } },
  { worker_id: 'a2', name: 'atlas', description: '', band: 'aaaa1111', build: 167, hb: {}, last_seen_age_secs: 70 },
  { worker_id: 'h1', name: 'rook', description: 'Rook hub', band: '*', build: 0, hb: {}, last_seen_age_secs: 0 },
])

test('hosting and the hub band are read from the roster and shown as text', async () => {
  const workers = parseWorkers(HOSTING)
  expect(workers[0]?.serves?.sites[0]).toEqual({ name: 'admin.example.com', url: 'https://admin.example.com', note: 'http://10.0.0.5:1240' })
  expect(workers[1]?.serves).toBeUndefined()
  expect(groupBands(workers).find(band => band.id === '*')?.name).toBe('Hub · all bands')
  const roster = { workers, fetchedAt: 1 }
  const list = paneText({ tab: 'bands' }, roster, 1_000)
  expect(list).toContain('■ nova · [claude] · hosts 2 · AI server')
  expect(list).toContain('■ rook · Rook hub') // the hub's build 0 is not an old build
  const detail = paneText({ tab: 'bands', screen: 'worker', worker: workers[0] }, roster, 1_000)
  expect(detail).toContain('- admin.example.com <https://admin.example.com> (http://10.0.0.5:1240)')
  expect(detail).toContain('Services:\n- shell.example.com <ssh://shell.example.com>')
})

test('a hosting worker opens its detail, and the pane tool reads and drives the pane', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('mcp.call', { server: 'rook' }, async (_, e) => {
    const cap = String(e.args.cap ?? e.tool)
    const text =
      cap === 'rook_workers' ? HOSTING
      : cap === 'rook_knowledge' ? BANDS
      : cap === 'rook_handoff_list' ? HANDOFFS
      : cap === 'rook_task' ? DECK
      : reply(HISTORY[cap] ?? { ok: false, error: `no ${cap}` })

    return { value: { content: [{ type: 'text', text }], isError: false } }
  })
  const ui = await $.ui.mount({
    plugin: 'rook', surface: 'terminal', component: 'Pane', requestId: 'rook-bands',
    props: { bodyColumns: 60 } as never,
  })
  await ui.press({ key: 'refresh' })
  await ui.press({ key: 'w:a1' })
  expect(await ui.find({ type: 'Link' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /ssh:\/\/shell\.example\.com/ })).toBeDefined()
  await ui.press({ key: 'worker-sessions' })
  expect(await ui.find({ type: 'Text', text: /Newest on nova/ })).toBeDefined()

  const pane = async (input: Record<string, unknown>) =>
    String((await $.tool.call({ tool: 'mcp__rook__pane', ...input } as never)).result)
  expect(await pane({})).toContain('[1] Tune vLLM')
  expect(await pane({ action: 'open', index: 1 })).toContain('KV quantization helps')
  expect(await pane({ action: 'worker', worker: 'NOVA' })).toContain('Worker nova (id a1)')
  expect(await ui.find({ type: 'Link' })).toBeDefined() // the person's pane followed
  expect(await pane({ action: 'tab', tab: 'deck' })).toContain('[1] todo · Refresh the status page (t_1)')
  expect(await pane({ action: 'open', index: 2 })).toContain('Keep the front door healthy')
  expect(await pane({ action: 'back' })).toContain('## Handoffs')
  expect(await pane({ action: 'worker', worker: 'nobody' })).toContain('Could not do that: no worker named "nobody"')
  expect(await pane({ action: 'sessions', query: 'NIC hang' })).toContain('Search "NIC hang" on all workers')
  await ui.unmount()
})
