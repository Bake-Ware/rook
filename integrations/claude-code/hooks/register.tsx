import { atom, read, update } from 'claude-code'
import type { EngineInterface, McpToolResult, Register } from 'claude-code'

import type { BandInfo, Item, MovePlan, Roster, SessionMeta, SettingRow, View, Worker } from '../types'
import { groupBands, fleetBuild, HUB_BAND, parseBands, parseWorkers, STALE_SECS } from './bands'
import { deckItems, findWorker, hostedCount, hostingConfig, hostingPrompt, paneText } from './pane'
import type { HostingConfig } from './pane'
import {
  GROOM_PROMPT,
  groomText,
  handoffItem,
  parseDeck,
  parseHandoffs,
  taskItem,
} from './deck'
import {
  ago,
  home,
  recency,
  agentIcon,
  FLEET_LIMIT,
  itemText,
  LOAD_MESSAGES,
  loadText,
  parseHits,
  parseCatalog,
  parseMessages,
  parseReply,
  parseSessions,
  PREVIEW_MESSAGES,
  SCOPED_LIMIT,
  SEARCH_LIMIT,
  SHOWN_SESSIONS,
  unwrap,
} from './sessions'
import type { Raw, Source } from './sessions'
import { INBOUND, INBOUND_HELP, INBOUND_MENU, inboundFromFile, pickSettings, showValue } from './settings'
import { asRecord } from './sessions'
import {
  assistantText,
  claudeDir,
  emit,
  emitDelta,
  emitPrompt,
  emitState,
  endMirror,
  newMirror,
  sessionPid,
  startMirror,
  toolInput,
  toolOutput,
  workerPlace,
} from './mirror'
import type { MirrorIO } from './mirror'

const PANE = 'rook-bands'
const TITLE = 'Rook'
const POLL_MS = 30_000
const FOLLOW_MS = 6_000
const CONNECT_TRIES = 15
const CONNECT_WAIT_MS = 2_000
const TABS = ['bands', 'sessions', 'deck', 'settings'] as const
const TAB_LABEL = { bands: 'BANDS', sessions: 'SESSIONS', deck: 'DECK', settings: 'SETTINGS' } as const
const PANE_TOOL = 'mcp__rook__pane'
const PANE_TOOL_SPEC = {
  name: 'pane',
  description:
    'The rook pane the person sees beside this chat (bands and workers, sessions across the fleet, the work deck, settings). ' +
    'action "read" (default) returns what it shows now as text, rows numbered where they open. ' +
    'The other actions drive it, then return the new text: "tab" {tab: bands|sessions|deck|settings}; ' +
    '"worker" {worker: name} opens a worker\'s detail (what it hosts); ' +
    '"sessions" {worker?: name, query?: text} lists or searches sessions; ' +
    '"open" {index: n} opens row [n] of the sessions list or the deck; "back"; "refresh". ' +
    'The settings tab is read-only here: only the person changes a setting, in the pane.',
  inputSchema: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['read', 'tab', 'worker', 'sessions', 'open', 'back', 'refresh'] },
      tab: { type: 'string', enum: ['bands', 'sessions', 'deck', 'settings'] },
      worker: { type: 'string', description: 'A worker name, as the bands tab lists it' },
      query: { type: 'string', description: 'Words or a regex to search sessions for' },
      index: { type: 'integer', minimum: 1, description: 'The [n] of a numbered row' },
    },
  },
} as const
// The hub page's visual language (rook/web/theme.css): olive, amber, schematic rules.
const C = {
  accent: '#d8ad6d',
  green: '#a4bc92',
  bad: '#dca58d',
  dim: '#818b75',
  line: '#444a3c',
  active: '#292b20',
} as const
const TASK_STATE: Record<string, { label: string; color: string }> = {
  in_progress: { label: 'doing', color: C.green },
  blocked: { label: 'blocked', color: C.bad },
  paused: { label: 'paused', color: C.accent },
  todo: { label: 'todo', color: C.dim },
}
const roster = atom({ plugin: 'rook', key: 'roster' } as const, {
  workers: [],
  fetchedAt: 0,
} as Roster)
const view = atom({ plugin: 'rook', key: 'view' } as const, { tab: 'bands' } as View)

type Patch = { [K in keyof View]?: View[K] | undefined }

/** Merges into the view; a key given `undefined` is dropped. */
function set($: EngineInterface, patch: Patch): Promise<unknown> {
  return update($, view, last => {
    const next: Record<string, unknown> = { ...last, ...patch }
    for (const key of Object.keys(next)) if (next[key] === undefined) delete next[key]

    return next as View
  })
}

const CLEAR: Patch = { busy: undefined, error: undefined, note: undefined, confirm: undefined }

const mcpText = (result: McpToolResult): string =>
  result.content.map(block => (block.type === 'text' ? block.text : '')).join('')

const reason = (error: unknown): string =>
  (error instanceof Error ? error.message : String(error)).slice(0, 200)

// A reply too big for the engine to hand back inline (the roster and the deck
// are ~70k characters) is saved to a file, and the answer is a notice naming it.
async function replyJson($: EngineInterface, text: string): Promise<string> {
  const head = text.trimStart()
  if (head.startsWith('[') || head.startsWith('{')) return text
  const saved = /saved to (\/\S+?\.(?:txt|json))/.exec(text)?.[1]
  if (saved === undefined) throw new Error(head.slice(0, 160) || 'empty reply')
  const file = await $.fs.read(saved)

  return typeof file === 'string' ? file : ''
}

// The hub is a local `rook` server in some projects and the claude.ai
// connector everywhere else; use whichever this session has connected.
const SERVERS = ['rook', 'claude.ai Rook']
let server: string | undefined

async function callRook(
  $: EngineInterface,
  tool: string,
  args: Record<string, unknown>,
): ReturnType<EngineInterface['mcp']['call']> {
  if (server !== undefined) return $.mcp.call(server, tool, args)
  let missing: unknown
  for (const name of SERVERS) {
    try {
      const result = await $.mcp.call(name, tool, args)
      server = name

      return result
    } catch (error) {
      if (!/no connected MCP tool/.test(reason(error))) throw error
      missing = error
    }
  }
  throw missing
}

async function rookTool(
  $: EngineInterface,
  tool: string,
  args: Record<string, unknown> = {},
): Promise<string> {
  const result = await callRook($, tool, args)
  const text = mcpText(result)
  if (result.isError) throw new Error(text || `${tool} failed`)

  return replyJson($, text)
}

async function rookCall(
  $: EngineInterface,
  worker: string,
  cap: string,
  args: Record<string, unknown> = {},
): Promise<Raw> {
  return parseReply(await rookTool($, 'rook_call', { cap, worker, args }))
}

const historyCall = (
  $: EngineInterface,
  at: Source,
  cap: string,
  args: Record<string, unknown> = {},
): Promise<Raw> => rookCall($, at.workerId, `${at.agent}-history.${cap}`, args)

async function requestId($: EngineInterface): Promise<string> {
  return `ccmod-${await $.clock.now()}-${Math.random().toString(36).slice(2, 10)}`
}

// ---- bands

// A newer hub trims the roster unless asked; an older one takes no arguments.
const ROSTER_FIELDS = [
  'worker_id',
  'name',
  'description',
  'band',
  'build',
  'plugins',
  'serves',
  'hb',
  'last_seen_age_secs',
]

// Three hubs in the field: one that knows `serves`, one that takes `fields`
// without it, one that takes nothing.
async function rosterText($: EngineInterface): Promise<string> {
  for (const fields of [ROSTER_FIELDS, ROSTER_FIELDS.filter(field => field !== 'serves')]) {
    try {
      return await rookTool($, 'rook_workers', { fields })
    } catch {
      // Ask for less.
    }
  }

  return rookTool($, 'rook_workers')
}

/** Resolves whether the roster came back; `quiet` keeps a failure off the pane. */
async function refresh($: EngineInterface, quiet = false): Promise<boolean> {
  const fetchedAt = await $.clock.now()
  try {
    const workers = parseWorkers(await rosterText($))
    let bands: BandInfo[] | undefined
    try {
      // Names are a nicety: without them the pane falls back to the band ids.
      bands = parseBands(await rookTool($, 'rook_knowledge', { action: 'bands' }))
    } catch {
      bands = undefined
    }
    await update($, roster, last => ({
      workers,
      fetchedAt,
      ...((bands ?? last.bands) !== undefined && { bands: bands ?? last.bands }),
    }))

    return true
  } catch (error) {
    // Keep the last good roster on screen; only the error line changes.
    if (!quiet) await update($, roster, last => ({ ...last, error: reason(error) }))

    return false
  }
}

// A fresh session draws the pane before its MCP servers have connected: the
// first fetches fail for a few seconds. Retry quietly, and only say so if rook
// is still unreachable after the last try.
function connect($: EngineInterface, attempt = 1): void {
  const isLast = attempt >= CONNECT_TRIES
  void refresh($, !isLast).then(ok => {
    if (!ok && !isLast) $.clock.after(CONNECT_WAIT_MS, () => connect($, attempt + 1))
  })
}

const isUp = async ($: EngineInterface): Promise<boolean> =>
  (await $.ui.panes()).some(pane => pane.id === PANE && pane.isPlaced && pane.isShown)

async function poll($: EngineInterface): Promise<void> {
  const { tab } = await read($, view)
  const { fetchedAt } = await read($, roster)
  if ((await isUp($)) && (fetchedAt === 0 || (tab ?? 'bands') === 'bands')) await refresh($)
}

// ---- sessions

async function sources($: EngineInterface, scope: View['scope']): Promise<Source[]> {
  let { workers } = await read($, roster)
  if (workers.length === 0) {
    await refresh($)
    workers = (await read($, roster)).workers
  }

  return workers
    .filter(worker => scope === undefined || worker.id === scope.id)
    .flatMap(worker =>
      (worker.history ?? []).map(agent => ({
        workerId: worker.id,
        workerName: worker.name,
        agent,
      })),
    )
}

// Workers whose build answers no `sessions.list`: asked through `<agent>-history.pull`.
const NO_CATALOG = new Set<string>()

/** One worker's newest sessions from its `sessions.list`; undefined where it has none. */
async function catalog(
  $: EngineInterface,
  group: Source[],
  limit: number,
): Promise<SessionMeta[] | undefined> {
  const first = group[0]
  if (first === undefined || NO_CATALOG.has(first.workerId)) return undefined
  try {
    const result = await rookCall($, first.workerId, 'sessions.list', { limit })

    return parseCatalog(result, first, group.map(source => source.agent))
  } catch (error) {
    if (/no such cap|unknown cap/i.test(reason(error))) NO_CATALOG.add(first.workerId)

    return undefined
  }
}

/** A worker's sources, one group per worker, in roster order. */
function byWorker(from: Source[]): Source[][] {
  const groups = new Map<string, Source[]>()
  for (const source of from) groups.set(source.workerId, [...(groups.get(source.workerId) ?? []), source])

  return [...groups.values()]
}

/** Every history worker's newest sessions, or the hits of a search, merged. */
async function listSessions(
  $: EngineInterface,
  scope: View['scope'],
  query: string | undefined,
): Promise<void> {
  await set($, {
    ...CLEAR,
    tab: 'sessions',
    screen: 'list',
    scope,
    query,
    busy: true,
    session: undefined,
    messages: undefined,
  })
  try {
    const from = await sources($, scope)
    const limit = scope === undefined ? FLEET_LIMIT : SCOPED_LIMIT
    // A worker with the session catalog answers for all its agents at once;
    // an older one is asked agent by agent. A search goes agent by agent.
    const groups = query === undefined ? byWorker(from) : from.map(source => [source])
    const settled = await Promise.allSettled(
      groups.map(async group =>
        query === undefined
          ? ((await catalog($, group, limit)) ??
            (
              await Promise.all(
                group.map(async source =>
                  parseSessions(await historyCall($, source, 'pull', { limit }), source),
                ),
              )
            ).flat())
          : (
              await Promise.all(
                group.map(async source =>
                  parseHits(await historyCall($, source, 'search', { query, limit: SEARCH_LIMIT }), source),
                ),
              )
            ).flat(),
      ),
    )
    const failed = settled.flatMap((one, index) =>
      one.status === 'rejected' ? [`${groups[index]?.[0]?.workerName}: ${reason(one.reason)}`] : [],
    )
    const sessions = settled
      .flatMap(one => (one.status === 'fulfilled' ? one.value : []))
      .sort((a, b) => (b.matches ?? 0) - (a.matches ?? 0) || b.modified - a.modified)
      .slice(0, SHOWN_SESSIONS)
    await set($, {
      busy: undefined,
      sessions,
      error: failed.length > 0 ? failed.join(' · ').slice(0, 300) : undefined,
    })
  } catch (error) {
    await set($, { busy: undefined, error: reason(error) })
  }
}

const readTail = async ($: EngineInterface, session: SessionMeta, count: number) =>
  parseMessages(
    await historyCall($, session, 'read', {
      session_id: session.id,
      offset: Math.max(0, session.count - count),
      max_messages: count,
    }),
  )

async function openSession($: EngineInterface, picked: SessionMeta): Promise<void> {
  await set($, {
    ...CLEAR,
    screen: 'session',
    session: picked,
    messages: undefined,
    version: undefined,
    handle: undefined,
    busy: true,
  })
  try {
    let session = picked
    if (session.count === 0) {
      // A search hit says nothing of the session's length; a snapshot does.
      const page = await historyCall($, session, 'read_snapshot', { session_id: session.id })
      session = {
        ...session,
        count: Number(page.total_messages ?? 0),
        active: page.active === true,
      }
    }
    const [messages, resumed] = await Promise.all([
      readTail($, session, PREVIEW_MESSAGES),
      historyCall($, session, 'resumed').catch((): Raw => ({})),
    ])
    const mine = (Array.isArray(resumed.sessions) ? (resumed.sessions as Raw[]) : []).find(
      one => one.session_id === session.id && one.running === true,
    )
    await set($, {
      busy: undefined,
      session,
      messages,
      handle: mine === undefined ? undefined : String(mine.handle),
    })
  } catch (error) {
    await set($, { busy: undefined, error: reason(error) })
  }
}

/** Re-reads the open session's tail when its log changed; one stat when it did not. */
async function follow($: EngineInterface): Promise<void> {
  const at = await read($, view)
  const session = at.session
  if (at.tab !== 'sessions' || at.screen !== 'session' || session === undefined) return
  if (at.busy === true || !(session.active || at.handle !== undefined)) return
  try {
    const page = await historyCall($, session, 'follow', {
      session_id: session.id,
      offset: session.count,
      version: at.version ?? '',
    })
    if (page.unchanged === true) return
    const grown = { ...session, count: Number(page.total_messages ?? session.count) }
    const messages = await readTail($, grown, PREVIEW_MESSAGES)
    const now = await read($, view)
    if (now.screen !== 'session' || now.session?.id !== session.id) return
    await set($, { session: grown, messages, version: String(page.version ?? '') })
  } catch {
    // A missed poll is not worth a line; the next one tries again.
  }
}

async function appendRow($: EngineInterface, text: string, what: string): Promise<string> {
  const appended = await $.session.append({
    message: { type: 'user', content: [{ type: 'text', text }] },
  })
  if (appended.deny !== undefined) throw new Error(`refused: ${appended.deny}`)
  void $.ui.toast(`rook: ${what.slice(0, 40)} loaded`)

  return `loaded into this chat (${Math.round(text.length / 1000)}k chars)`
}

/** One action of the open session, its outcome shown as the note or the error. */
async function act(
  $: EngineInterface,
  doing: string,
  run: (at: View, session: SessionMeta) => Promise<Patch>,
): Promise<void> {
  const at = await read($, view)
  if (at.session === undefined) return
  await set($, { ...CLEAR, busy: true, note: doing })
  try {
    await set($, { busy: undefined, ...(await run(at, at.session)) })
  } catch (error) {
    await set($, { busy: undefined, note: undefined, error: reason(error) })
  }
}

const loadSession = ($: EngineInterface) =>
  act($, 'loading…', async (_, session) => ({
    note: await appendRow(
      $,
      loadText(session, await readTail($, session, LOAD_MESSAGES)),
      session.title,
    ),
  }))

const resumeSession = ($: EngineInterface) =>
  act($, 'starting it on the worker…', async (_, session) => {
    const started = await historyCall($, session, 'resume', { session_id: session.id })

    return {
      handle: String(started.handle ?? ''),
      session: { ...session, active: true },
      note:
        started.remote_control === true
          ? `resumed on ${session.workerName} with Remote Control: look for it in claude.ai`
          : `resumed on ${session.workerName}`,
    }
  })

const stopSession = ($: EngineInterface) =>
  act($, 'stopping…', async (at, session) => {
    await rookCall($, session.workerId, 'proc.close', { handle: at.handle })

    return {
      handle: undefined,
      session: { ...session, active: false, messageable: false },
      note: 'stopped',
    }
  })

const sendMessage = ($: EngineInterface, text: string) =>
  act($, 'sending…', async (_, session) => {
    if (text.trim() === '') return { note: undefined }
    await historyCall($, session, 'send', {
      session_id: session.id,
      text,
      command_id: await requestId($),
    })

    return { note: `sent to ${session.workerName}` }
  })

// ---- deck

async function loadDeck($: EngineInterface): Promise<void> {
  await set($, { ...CLEAR, tab: 'deck', screen: 'list', item: undefined, busy: true })
  try {
    const [deck, handoffs] = await Promise.all([
      rookTool($, 'rook_task', { action: 'deck' }),
      rookTool($, 'rook_handoff_list', { limit: 12 }),
    ])
    await set($, {
      busy: undefined,
      deck: parseDeck(unwrap(deck)),
      handoffs: parseHandoffs(unwrap(handoffs)),
    })
  } catch (error) {
    await set($, { busy: undefined, error: reason(error) })
  }
}

/** Hands the whole deck to the chat with its ids, then starts a grooming turn. */
async function groomDeck($: EngineInterface): Promise<void> {
  await set($, { ...CLEAR, busy: true, note: 'gathering the deck…' })
  try {
    const [deck, threads] = await Promise.all([
      rookTool($, 'rook_task', { action: 'deck' }),
      rookTool($, 'rook_handoff_list', { limit: 20 }),
    ])
    const handoffs = parseHandoffs(unwrap(threads))
    const names = Object.fromEntries(
      ((await read($, roster)).bands ?? []).map(band => [band.id, band.name]),
    )
    const text = groomText(unwrap(deck), handoffs, names, await $.clock.now())
    await appendRow($, text, 'work deck')
    void $.prompt.submit({ text: GROOM_PROMPT })
    await set($, {
      busy: undefined,
      deck: parseDeck(unwrap(deck)),
      handoffs,
      note: `deck sent to the chat (${Math.round(text.length / 1000)}k chars); grooming started`,
    })
  } catch (error) {
    await set($, { busy: undefined, note: undefined, error: reason(error) })
  }
}

async function itemAct(
  $: EngineInterface,
  doing: string,
  run: (item: Item) => Promise<string>,
): Promise<void> {
  const { item } = await read($, view)
  if (item === undefined) return
  await set($, { ...CLEAR, busy: true, note: doing })
  try {
    await set($, { busy: undefined, note: await run(item) })
  } catch (error) {
    await set($, { busy: undefined, note: undefined, error: reason(error) })
  }
}

const loadItem = ($: EngineInterface) =>
  itemAct($, 'loading…', item =>
    appendRow($, itemText(item.title, item.meta, item.body), item.title),
  )

const claimItem = ($: EngineInterface) =>
  itemAct($, 'claiming…', async item => {
    unwrap(
      await rookTool($, 'rook_task', {
        action: 'claim',
        id: item.claimId,
        request_id: await requestId($),
      }),
    )

    return 'claimed for this session’s rook identity'
  })

// ---- settings: Claude Code's /config rows that matter to rook, changed in place

/** The user's settings file, where `/config` writes the inbound setting. */
async function userSettings($: EngineInterface): Promise<string | undefined> {
  const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? ''}/.claude`
  try {
    return await $.fs.read(`${dir}/settings.json`)
  } catch {
    return undefined
  }
}

async function loadSettings($: EngineInterface, done: Patch = {}): Promise<void> {
  await set($, { ...CLEAR, tab: 'settings', screen: 'list', busy: true })
  try {
    const rows = pickSettings(await $.config.list())
    if (!rows.some(row => row.key === INBOUND)) rows.unshift(inboundFromFile(await userSettings($)))
    await set($, { busy: undefined, settings: rows, ...done })
  } catch (error) {
    await set($, { busy: undefined, error: reason(error) })
  }
}

/** Only the person's press reaches this: the pane tool cannot change a setting. */
async function changeSetting($: EngineInterface, row: SettingRow, value: SettingRow['value']): Promise<void> {
  await set($, { ...CLEAR, busy: true })
  let done: Patch
  try {
    const result = await $.config.set({ key: row.key, value })
    done =
      result.deny !== undefined
        ? { error: row.key === INBOUND ? `${result.deny}: ${INBOUND_MENU}` : result.deny }
        : {
            note:
              `${row.label}: ${showValue(result.value)}` +
              (row.key.startsWith('rook.') ? ' (run /reload-plugins to apply it)' : ''),
          }
  } catch (error) {
    done = { error: reason(error) }
  }
  await loadSettings($, done)
}

async function showTab($: EngineInterface, tab: (typeof TABS)[number]): Promise<void> {
  const at = await read($, view)
  if (tab === 'sessions' && at.sessions === undefined) return listSessions($, undefined, undefined)
  if (tab === 'deck' && at.deck === undefined) return loadDeck($)
  if (tab === 'settings') return loadSettings($)
  await set($, { ...CLEAR, tab, screen: 'list' })
}

async function refreshTab($: EngineInterface): Promise<void> {
  const at = await read($, view)
  if (at.tab === 'deck') return loadDeck($)
  if (at.tab === 'settings') return loadSettings($)
  if (at.tab !== 'sessions') return void (await refresh($))
  if (at.screen === 'session' && at.session !== undefined) return openSession($, at.session)

  return listSessions($, at.scope, at.query)
}

// ---- /rook-move: this conversation goes on in a Rook terminal on this host

/** How long the worker waits for this Claude Code to exit before it gives up. */
const HANDOFF_MINUTES = 2

/** Everything a move needs, or the reason it cannot happen here. */
async function movePlan($: EngineInterface): Promise<MovePlan> {
  const inTerminal = await $.env.get('ROOK_WORK_TERMINAL')
  if (inTerminal !== undefined && inTerminal !== '') {
    throw new Error('this session already runs in a Rook terminal: it is on the Sessions page as it is')
  }
  const io = mirrorIO($)
  const place = await workerPlace(io)
  if (place === undefined) throw new Error('no Rook worker is installed on this host')
  let workerId = ''
  try {
    workerId = (await $.fs.read(`${place.state}${place.sep}worker_id`)).trim()
  } catch {
    // Said below.
  }
  if (workerId === '') throw new Error('the Rook worker on this host has not enrolled yet (no worker id)')
  const [sessionId, cwd] = await Promise.all([$.session.id(), $.session.cwd()])
  const pid = await sessionPid(io, await claudeDir(io), sessionId)
  if (pid === undefined) {
    throw new Error(
      'Claude Code did not record a process id for this session, so the move could not wait for it to end. ' +
        'Run /exit, then resume it from the Sessions page.',
    )
  }
  let { workers } = await read($, roster)
  if (workers.length === 0 && (await refresh($, true))) workers = (await read($, roster)).workers
  const workerName = workers.find(worker => worker.id === workerId)?.name ?? 'this host'

  return { workerId, workerName, sessionId, cwd, pid }
}

/** Opens the pane on the move's confirmation; nothing moves until the person says yes. */
async function askMove($: EngineInterface): Promise<string> {
  void $.ui.open({ id: PANE, title: TITLE }).catch(() => undefined)
  await set($, { ...CLEAR, confirm: 'move', move: undefined, busy: true })
  try {
    const move = await movePlan($)
    await set($, { busy: undefined, move })

    return `Move to a Rook terminal on ${move.workerName}? Confirm in the Rook pane (y), or cancel (n).`
  } catch (error) {
    await set($, { busy: undefined, move: undefined, error: `cannot move: ${reason(error)}` })

    return `Cannot move this session: ${reason(error)}`
  }
}

const OLD_WORKER =
  'the worker on this host cannot take a session over yet: it needs Rook terminals (Linux or macOS) and a build with handoff_pid'

/** The person said yes: the worker opens the terminal, then this Claude Code exits. */
async function doMove($: EngineInterface): Promise<void> {
  const { move } = await read($, view)
  if (move === undefined) return
  await set($, { ...CLEAR, confirm: 'move', busy: true, note: `starting a Rook terminal on ${move.workerName}…` })
  try {
    const opened = await rookCall($, move.workerId, 'work.stream.open', {
      harness: 'claude',
      resume: move.sessionId,
      cwd: move.cwd,
      handoff_pid: move.pid,
    })
    const terminal = String(opened.id ?? '')
    await set($, {
      busy: undefined,
      move: { ...move, terminal },
      note:
        `Moved: this conversation continues in Rook terminal ${terminal} on ${move.workerName}. ` +
        'Watch and type in it from the Sessions page. Closing this Claude Code now…',
    })
    void $.ui.toast('rook: this session continues on the Sessions page', { timeoutMs: 8000 })
    $.clock.after(1500, () => void exitHere($))
  } catch (error) {
    const why = reason(error)
    await set($, {
      busy: undefined,
      note: undefined,
      move: undefined,
      error: /no such cap|unknown cap|unexpected keyword|handoff_pid/i.test(why) ? OLD_WORKER : `move failed: ${why}`,
    })
  }
}

/** Ends this Claude Code so the Rook terminal can resume the session. */
async function exitHere($: EngineInterface): Promise<void> {
  const tell = (why: string) =>
    set($, {
      error:
        `Claude Code could not close itself (${why}). Type /exit now: the Rook terminal waits ` +
        `${HANDOFF_MINUTES} minutes for this one to end, then resumes the conversation.`,
    })
  try {
    const names = (await $.command.list()).map(command => command.name)
    if (!names.includes('exit')) return void (await tell('no /exit command here'))
    await $.command.run({ command: 'exit' })
  } catch (error) {
    await tell(reason(error))
  }
}

// ---- the session mirror: this session's events, served by the worker as sessions.mirror

const mirror = newMirror()

/** What the mirror and /rook-move read and write, through this session's `$`. */
function mirrorIO($: EngineInterface): MirrorIO {
  const env = {
    OS: () => $.env.get('OS'),
    ROOK_WORKER_HOME: () => $.env.get('ROOK_WORKER_HOME'),
    USERPROFILE: () => $.env.get('USERPROFILE'),
    HOME: () => $.env.get('HOME'),
    USERNAME: () => $.env.get('USERNAME'),
    USERDOMAIN: () => $.env.get('USERDOMAIN'),
    CLAUDE_CONFIG_DIR: () => $.env.get('CLAUDE_CONFIG_DIR'),
  }

  return {
    env: name => env[name](),
    exists: path => $.fs.exists(path),
    list: path => $.fs.list(path),
    read: path => $.fs.read(path),
    write: (path, text) => $.fs.write(path, text),
    run: (argv, init) => $.process.run(argv, init),
    now: () => $.clock.now(),
    after: (ms, fn) => void $.clock.after(ms, fn),
    sessionId: () => $.session.id(),
    cwd: () => $.session.cwd(),
    model: () => $.session.model(),
    version: () => $.session.version().then(v => v.version),
    settings: () => $.settings.read(),
  }
}

// ---- the pane as a tool: Claude reads what it shows and drives it

async function drive($: EngineInterface, input: Raw): Promise<string> {
  const action = String(input.action ?? 'read')
  const at = await read($, view)
  if ((await read($, roster)).fetchedAt === 0) await refresh($, true)
  const known = await read($, roster)
  const named = typeof input.worker === 'string' ? findWorker(known, input.worker) : undefined
  if (typeof input.worker === 'string' && named === undefined) {
    throw new Error(`no worker named "${input.worker}" on the roster`)
  }
  // Driving shows the pane; where it cannot be opened the state still moves.
  if (action !== 'read') void $.ui.open({ id: PANE, title: TITLE }).catch(() => undefined)

  if (action === 'tab') {
    const tab = TABS.find(name => name === input.tab)
    if (tab === undefined) throw new Error('tab is bands, sessions, deck or settings')
    await showTab($, tab)
  } else if (action === 'worker') {
    if (named === undefined) throw new Error('worker needs a worker name')
    await set($, { ...CLEAR, tab: 'bands', screen: 'worker', worker: named })
  } else if (action === 'sessions') {
    const query = typeof input.query === 'string' && input.query.trim() !== '' ? input.query.trim() : undefined
    await listSessions($, named === undefined ? undefined : { id: named.id, name: named.name }, query)
  } else if (action === 'open') {
    const index = Number(input.index)
    const tab = at.tab ?? 'bands'
    const picked =
      tab === 'sessions' ? at.sessions?.[index - 1] : tab === 'deck' ? deckItems(at)[index - 1] : undefined
    if (picked === undefined) {
      throw new Error(
        tab === 'bands'
          ? 'the bands tab opens a worker by name: action "worker"'
          : `no row [${input.index}] on the ${tab} list; read the pane for its rows`,
      )
    }
    if ('workerId' in picked) await openSession($, picked)
    else await set($, { ...CLEAR, tab: 'deck', screen: 'item', item: picked })
  } else if (action === 'back') {
    await set($, { ...CLEAR, screen: 'list' })
  } else if (action === 'refresh') {
    await refreshTab($)
  } else if (action !== 'read') {
    throw new Error(`unknown action "${action}"`)
  }

  return paneText(await read($, view), await read($, roster), await $.clock.now())
}

export const register: Register = (on, options) => {
  // The hosting sync is the person's own deployment's: off unless they turn it on.
  const hosting: HostingConfig | undefined = hostingConfig(options)
  // Mirroring is on unless the person turned it off (rook.mirror); off writes nothing.
  const isMirrored = options.mirror !== false

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'rook-bands',
      description: 'Show rook in a pane: bands and workers, sessions across the fleet, the work deck',
    })
    await $.command.register({
      name: 'rook-move',
      description: 'Move this conversation into a Rook terminal on this host, watchable and typeable from the Sessions page',
    })
    await $.tool.register(PANE_TOOL_SPEC)
    // Mirrored for the Sessions page when a Rook worker lives on this host.
    if (e.isInteractive && isMirrored) startMirror(mirrorIO($), mirror, e.cwd)
    // Unasked, the engine seats a pane only in a wide terminal: say how to get it.
    void $.ui.open({ id: PANE, title: TITLE }).then(opened => {
      if (opened.isPlaced) return connect($)
      void $.ui.toast('rook: /rook-bands opens the pane', { timeoutMs: 8000 })
    })
    // Nothing is fetched for a pane nobody sees: the roster only while the
    // bands tab is up, a session's tail only while that session is.
    $.clock.every(POLL_MS, () => void poll($))
    $.clock.every(FOLLOW_MS, () => void isUp($).then(up => (up ? follow($) : undefined)))

    return next(e)
  })

  // The pane is a fixture: the person's close mark and keys leave it up. Only
  // `/rook-bands off` (a plugin close) and an unload take it down.
  on('ui.close', { id: PANE }, async (_, e, next) => {
    if (e.origin.kind === 'person') return { value: undefined }

    return next(e)
  })

  // The tool's name is not in the typed tool list until the engine has loaded
  // it once, so the hook matches by comparing the name itself.
  on('tool.call', async ($, e, next) => {
    // The tool's own arguments sit beside `tool` on the event.
    const call = asRecord(e)
    if (call.tool !== PANE_TOOL) {
      // Mirrored (the main conversation's calls; a subagent's show in its result).
      if (e.agentId !== undefined) return next(e)
      const id = e.tool_use_id ?? ''
      emit(mirrorIO($), mirror, { type: 'tool.call', id, name: String(e.tool), input: toolInput(e) })
      try {
        const result = await next(e)
        emit(mirrorIO($), mirror, { type: 'tool.result', id, ...toolOutput(result) })
        emitState(mirrorIO($), mirror, 'working')

        return result
      } catch (error) {
        emit(mirrorIO($), mirror, { type: 'tool.result', id, ok: false, text: reason(error) })
        throw error
      }
    }
    let result: string
    try {
      result = await drive($, call)
    } catch (error) {
      const now = paneText(await read($, view), await read($, roster), await $.clock.now())
      result = `Could not do that: ${reason(error)}\n\n${now}`
    }

    return { result } as never
  })

  on('command.run', { command: 'rook-bands' }, async ($, e) => {
    if (e.args.trim() === 'off') {
      await $.ui.close({ id: PANE })

      return { text: 'Rook pane closed; /rook-bands opens it again.' }
    }
    await $.ui.open({ id: PANE, title: TITLE })
    void refresh($)

    return { text: 'Rook pane opened.' }
  })

  on('command.run', { command: 'rook-move' }, async $ => ({ text: await askMove($) }))

  // ---- the session mirror. Each hook only queues an event; the writes are
  // batched on a timer, so no turn waits on the disk.

  on('prompt.submit', async ($, e, next) => {
    emitPrompt(mirrorIO($), mirror, e.text, e.origin.kind)

    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    emitState(mirrorIO($), mirror, 'working')

    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (e.agentId !== undefined) return yield* stream
    for await (const chunk of stream) {
      if (chunk.kind === 'text') emitDelta(mirrorIO($), mirror, chunk.text)
      yield chunk
    }
    const step = await stream.result
    if (step.answer !== '') emit(mirrorIO($), mirror, { type: 'assistant.done', text: assistantText(step.answer) })

    return step
  })

  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    if (verdict.decision === 'ask') emitState(mirrorIO($), mirror, 'waiting')

    return verdict
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      emit(mirrorIO($), mirror, { type: 'turn.end', stop_reason: e.reason })
      emitState(mirrorIO($), mirror, 'idle')
    }

    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await endMirror(mirrorIO($), mirror, e.reason).catch(() => undefined)

    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const table = $.ui.resolve(e)
    const { Box, Text, Button } = table
    // The mobile surface has no text field: search and messaging are left out there.
    const Input = 'Input' in table ? table.Input : undefined
    const Link = 'Link' in table ? table.Link : undefined
    const at = await read($, view)
    const tab = TABS.find(name => name === at.tab) ?? 'bands'
    const columns = Math.max(24, e.props.bodyColumns ?? e.viewport?.columns ?? 40)
    const fit = (text: string, room: number): string =>
      text.length > room ? `${text.slice(0, Math.max(1, room - 1))}…` : text

    const rule = <Text color={C.line}>{'─'.repeat(columns)}</Text>
    // One heading per group of rows: its name, a count at the right, a rule.
    const heading = (name: string, right = '') => (
      <Box flexDirection="column" marginTop={1}>
        <Box flexDirection="row" justifyContent="space-between">
          <Text color={C.accent}>
            {fit(`┌ ${name.toUpperCase()}`, columns - right.length - 2)}
          </Text>
          <Text color={C.dim}>{right.toUpperCase()}</Text>
        </Box>
        {rule}
      </Box>
    )
    const tabs = (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold>♜ R O O K</Text>
          <Button key="refresh" hotkey="r" label="r · refresh" onPress={() => refreshTab($)} />
        </Box>
        <Box flexDirection="row">
          <Box flexDirection="row" columnGap={1}>
            {TABS.map((name, index) =>
              name === tab ? (
                <Text bold color={C.accent} backgroundColor={C.active}>
                  {`  ${index + 1} · ${TAB_LABEL[name]}  `}
                </Text>
              ) : (
                <Button
                  key={`tab:${name}`}
                  hotkey={String(index + 1)}
                  label={`${index + 1} · ${TAB_LABEL[name]}`}
                  onPress={() => showTab($, name)}
                />
              ),
            )}
          </Box>
        </Box>
        {rule}
      </Box>
    )
    const status = (
      <Box flexDirection="column">
        {at.busy === true && at.note === undefined && <Text color={C.accent}>working…</Text>}
        {at.note !== undefined && (
          <Text color={C.green} wrap="wrap">
            {`✓ ${at.note}`}
          </Text>
        )}
        {at.error !== undefined && (
          <Text color={C.bad} wrap="wrap">
            {`✗ ${at.error}`}
          </Text>
        )}
      </Box>
    )
    const back = (label: string) => (
      <Button
        key="back"
        hotkey="b"
        label={`b · ‹ ${label}`}
        onPress={() => set($, { ...CLEAR, screen: 'list' })}
      />
    )
    const keys = (hint: string) => (
      <Box flexDirection="column" marginTop={1}>
        {rule}
        <Text dimColor wrap="wrap">
          {hint}
        </Text>
      </Box>
    )
    const MOVE = 'tab / arrows move · enter opens · esc returns to the prompt'

    if (at.confirm === 'move') {
      const plan = at.move
      const isAsking = plan !== undefined && plan.terminal === undefined && at.busy !== true

      return (
        <Box flexDirection="column">
          {tabs}
          <Text bold>Move this conversation to a Rook terminal</Text>
          {plan !== undefined && (
            <Text wrap="wrap">
              {`Resume it on ${plan.workerName} in ${home(plan.cwd)}, then close this Claude Code. ` +
                'From then on it lives on the Sessions page: watch it, type into it, stop it from any browser. ' +
                `The terminal waits up to ${HANDOFF_MINUTES} minutes for this one to end.`}
            </Text>
          )}
          {status}
          {at.busy !== true && plan?.terminal === undefined && (
            <Box flexDirection="row" columnGap={2} marginTop={1}>
              {isAsking && (
                <Button key="move-yes" variant="primary" hotkey="y" label="y · move it" onPress={() => doMove($)} />
              )}
              <Button
                key="move-no"
                hotkey="n"
                label={isAsking ? 'n · cancel' : 'n · close'}
                onPress={() => set($, { ...CLEAR, move: undefined })}
              />
            </Box>
          )}
          {keys('y moves this conversation and closes Claude Code here · n keeps it here')}
        </Box>
      )
    }

    if (tab === 'sessions' && at.screen === 'session' && at.session !== undefined) {
      const session = at.session
      const icon = agentIcon(session.agent)
      const isLive = session.active || at.handle !== undefined
      const messages = at.messages ?? []

      return (
        <Box flexDirection="column">
          {tabs}
          {back(at.scope !== undefined ? `sessions on ${at.scope.name}` : 'all sessions')}
          <Box marginTop={1}>
            <Text bold wrap="wrap">
              {session.title}
            </Text>
          </Box>
          <Box flexDirection="row" justifyContent="space-between">
            <Box flexDirection="row">
              <Text color={icon.color}>{`${icon.glyph} ${session.agent}`}</Text>
              <Text dimColor wrap="truncate-end">
                {` on ${session.workerName} · ${session.count} messages${session.cwd ? ` · ${home(session.cwd)}` : ''}`}
              </Text>
            </Box>
            {isLive && (
              <Text bold color={C.green}>
                LIVE
              </Text>
            )}
          </Box>
          <Box flexDirection="row" marginTop={1} columnGap={1}>
            <Button
              key="load"
              variant="primary"
              hotkey="l"
              label="l · load into chat"
              onPress={() => loadSession($)}
            />
            {at.handle !== undefined && (
              <Button key="stop" hotkey="x" label="x · stop it" onPress={() => stopSession($)} />
            )}
            {at.handle === undefined && !session.active && (
              <Button
                key="resume"
                hotkey="u"
                label={`u · resume on ${fit(session.workerName, 14)}`}
                onPress={() => resumeSession($)}
              />
            )}
          </Box>
          {status}
          {isLive && Input !== undefined && (
            <Box marginTop={1}>
              <Input
                key="message"
                label="send a message"
                placeholder={
                  session.messageable || at.handle !== undefined
                    ? 'type, then enter: it goes into this session'
                    : 'this session shows no inbox; a send may be refused'
                }
                submitLabel="send"
                onSubmit={text => sendMessage($, text)}
              />
            </Box>
          )}
          {messages.length > 0 &&
            heading('Latest messages', `last ${messages.length}${isLive ? ' · updates live' : ''}`)}
          {messages.map(message => {
            const isUser = message.role === 'user'

            return (
              <Box flexDirection="column" marginBottom={1}>
                <Text bold color={isUser ? C.accent : icon.color}>
                  {isUser ? '▸ you' : `◂ ${session.agent}`}
                </Text>
                <Box paddingLeft={2}>
                  <Text wrap="wrap" dimColor={!isUser}>
                    {message.text.length > 600 ? `${message.text.slice(0, 600)} …` : message.text}
                  </Text>
                </Box>
              </Box>
            )
          })}
          {keys(`l loads the last ${LOAD_MESSAGES} messages into this chat as reference · b back`)}
        </Box>
      )
    }

    if (tab === 'sessions') {
      const now = await $.clock.now()
      const sessions = at.sessions ?? []
      const isSearch = at.query !== undefined
      const where = at.scope?.name ?? 'all workers'
      const groups: Array<{ name: string; rows: SessionMeta[] }> = []
      for (const session of sessions) {
        const name = isSearch ? 'Best matches first' : recency(now, session.modified)
        const last = groups[groups.length - 1]
        if (last !== undefined && last.name === name) last.rows.push(session)
        else groups.push({ name, rows: [session] })
      }

      return (
        <Box flexDirection="column">
          {tabs}
          <Box flexDirection="row" justifyContent="space-between">
            <Text bold wrap="truncate-end">
              {isSearch ? `Search “${fit(at.query ?? '', 20)}” on ${where}` : `Newest on ${where}`}
            </Text>
            {(at.scope !== undefined || isSearch) && (
              <Button
                key="all"
                hotkey="a"
                label="a · all sessions"
                onPress={() => listSessions($, undefined, undefined)}
              />
            )}
          </Box>
          {Input !== undefined && (
            <Input
              key="search"
              label="search"
              placeholder="words or a regex, then enter · empty shows the newest"
              submitLabel="search"
              onSubmit={text =>
                listSessions($, at.scope, text.trim() === '' ? undefined : text.trim())
              }
            />
          )}
          {status}
          {at.busy !== true && sessions.length === 0 && (
            <Text dimColor>{isSearch ? 'No session matches that.' : 'No sessions found.'}</Text>
          )}
          {groups.map(group => (
            <Box flexDirection="column">
              {heading(group.name, `${group.rows.length}`)}
              {group.rows.map(session => {
                const icon = agentIcon(session.agent)
                const facts = [
                  session.workerName,
                  session.modified > 0 ? `${ago(now, session.modified)} ago` : '',
                  session.count > 0 ? `${session.count} msgs` : '',
                  session.matches !== undefined ? `${session.matches} matches` : '',
                  session.cwd !== '' && home(session.cwd) !== '~' ? home(session.cwd) : '',
                ].filter(fact => fact !== '')

                return (
                  <Box flexDirection="column" marginBottom={1}>
                    <Box flexDirection="row">
                      <Text color={icon.color}>{`${icon.glyph} `}</Text>
                      <Button
                        key={`s:${session.workerId}:${session.agent}:${session.id}`}
                        plain
                        label={fit(session.title, columns - (session.active ? 9 : 3))}
                        onPress={() => openSession($, session)}
                      />
                      {session.active && (
                        <Text bold color={C.green}>
                          {' LIVE'}
                        </Text>
                      )}
                    </Box>
                    <Text dimColor wrap="truncate-end">{`  ${facts.join(' · ')}`}</Text>
                    {session.snippet !== undefined && session.snippet !== '' && (
                      <Text italic wrap="truncate-end">{`  “…${session.snippet.trim()}…”`}</Text>
                    )}
                  </Box>
                )
              })}
            </Box>
          ))}
          {keys(`${MOVE} · ✻ claude  ◆ codex`)}
        </Box>
      )
    }

    if (tab === 'deck' && at.screen === 'item' && at.item !== undefined) {
      const item = at.item

      return (
        <Box flexDirection="column">
          {tabs}
          {back('deck')}
          <Box marginTop={1}>
            <Text bold wrap="wrap">
              {item.title}
            </Text>
          </Box>
          <Text dimColor wrap="wrap">
            {item.meta}
          </Text>
          <Box flexDirection="row" marginTop={1} columnGap={1}>
            <Button
              key="load"
              variant="primary"
              hotkey="l"
              label="l · load into chat"
              onPress={() => loadItem($)}
            />
            {item.claimId !== undefined && (
              <Button key="claim" hotkey="c" label="c · claim it" onPress={() => claimItem($)} />
            )}
          </Box>
          {status}
          {rule}
          <Text wrap="wrap">{item.body === '' ? '(nothing more recorded)' : item.body}</Text>
          {keys('l adds this to the chat as reference · b back')}
        </Box>
      )
    }

    if (tab === 'settings') {
      const rows = at.settings ?? []

      return (
        <Box flexDirection="column">
          {tabs}
          <Text bold>Settings</Text>
          {status}
          {at.busy !== true && rows.length === 0 && <Text dimColor>No settings to show.</Text>}
          {rows.map(row => {
            const current = showValue(row.value)
            const choices =
              row.kind === 'boolean' ? ['true', 'false'] : row.kind === 'choice' ? (row.options ?? []) : []

            return (
              <Box flexDirection="column">
                {heading(row.label, row.locked ? 'locked' : current)}
                {row.changeIn !== undefined && (
                  <Text color={C.accent} wrap="wrap">
                    {`Claude Code keeps this one from plugins: change it in ${row.changeIn}.`}
                  </Text>
                )}
                {row.description !== undefined && (
                  <Text dimColor wrap="wrap">
                    {row.description}
                  </Text>
                )}
                {choices.length > 0 && (
                  <Box flexDirection="row" columnGap={1} flexWrap="wrap">
                    {choices.map(choice =>
                      choice === current || row.locked || row.changeIn !== undefined ? (
                        <Text
                          key={`set:${row.key}:${choice}`}
                          bold={choice === current}
                          color={choice === current ? C.accent : C.dim}
                          backgroundColor={choice === current ? C.active : undefined}
                        >
                          {` ${choice} `}
                        </Text>
                      ) : (
                        <Button
                          key={`set:${row.key}:${choice}`}
                          label={choice}
                          onPress={() => changeSetting($, row, row.kind === 'boolean' ? choice === 'true' : choice)}
                        />
                      ),
                    )}
                  </Box>
                )}
                {choices.length === 0 &&
                  (Input !== undefined && !row.locked && row.changeIn === undefined ? (
                    <Input
                      key={`set:${row.key}`}
                      label={row.label}
                      placeholder={current}
                      submitLabel="set"
                      onSubmit={text =>
                        changeSetting($, row, row.kind === 'number' ? Number(text) : text.trim())
                      }
                    />
                  ) : (
                    <Text>{current}</Text>
                  ))}
                {row.key === INBOUND && typeof row.value === 'string' && INBOUND_HELP[row.value] !== undefined && (
                  <Text color={C.green} wrap="wrap">
                    {INBOUND_HELP[row.value]}
                  </Text>
                )}
              </Box>
            )
          })}
          {keys(`pick a value to save it to your user settings · ${MOVE}`)}
        </Box>
      )
    }

    if (tab === 'deck') {
      const open = (item: Item) => set($, { ...CLEAR, screen: 'item', item })
      const deck = (at.deck ?? []).filter(project => project.tasks.length > 0)
      const idle = (at.deck ?? []).length - deck.length
      const handoffs = at.handoffs ?? []

      return (
        <Box flexDirection="column">
          {tabs}
          <Box flexDirection="row" justifyContent="space-between">
            <Text bold>Open work across all bands</Text>
            <Button
              key="groom"
              variant="primary"
              hotkey="g"
              label="g · groom with Claude"
              onPress={() => groomDeck($)}
            />
          </Box>
          {status}
          {at.busy !== true && deck.length === 0 && handoffs.length === 0 && (
            <Text dimColor>Nothing on deck.</Text>
          )}
          {deck.map(project => (
            <Box flexDirection="column">
              {heading(project.title, `${project.tasks.length} open · ${project.done} done`)}
              {project.tasks.map(task => {
                const state = TASK_STATE[task.state] ?? { label: task.state, color: C.dim }

                return (
                  <Box flexDirection="row">
                    <Text color={state.color}>{state.label.padEnd(8)}</Text>
                    <Button
                      key={`t:${task.id}`}
                      plain
                      label={fit(task.title, columns - 9)}
                      onPress={() => open(taskItem(project.title, task))}
                    />
                  </Box>
                )
              })}
            </Box>
          ))}
          {idle > 0 && (
            <Box marginTop={1}>
              <Text dimColor>{`${idle} more projects have no open tasks`}</Text>
            </Box>
          )}
          {handoffs.length > 0 && heading('Handoffs', `${handoffs.length} threads`)}
          {handoffs.map(handoff => (
            <Box flexDirection="column" marginBottom={1}>
              <Button
                key={`h:${handoff.threadId}`}
                plain
                label={fit(handoff.goal, columns - 1)}
                onPress={() => open(handoffItem(handoff))}
              />
              <Text dimColor wrap="truncate-end">
                {`  ${handoff.asOf} · ${handoff.author} · ${handoff.nextSteps.length} next steps`}
              </Text>
            </Box>
          ))}
          {keys(`g sends the deck to the chat; Claude verifies each item, fixes what it can prove, then asks · ${MOVE}`)}
        </Box>
      )
    }

    const { workers, bands: known, fetchedAt, error } = await read($, roster)
    const opened = at.worker
    // The roster's copy when it still lists the worker: its facts stay fresh.
    const worker =
      tab === 'bands' && at.screen === 'worker' && opened !== undefined
        ? (workers.find(one => one.id === opened.id || one.name === opened.name) ?? opened)
        : undefined

    if (worker !== undefined) {
      const history = worker.history ?? []
      const hosted = (title: string, list: NonNullable<Worker['serves']>['sites']) =>
        list.length > 0 && (
          <Box flexDirection="column">
            {heading(title, `${list.length}`)}
            {list.map(one => (
              <Box flexDirection="column" marginBottom={1}>
                {Link !== undefined && one.url.startsWith('https://') ? (
                  <Text>
                    <Link href={one.url} label={fit(one.name, columns - 1)} />
                  </Text>
                ) : (
                  <Text wrap="truncate-end">{one.url !== '' && !one.url.startsWith('https://') ? one.url : one.name}</Text>
                )}
                {one.note !== '' && <Text dimColor wrap="truncate-end">{`  → ${one.note}`}</Text>}
              </Box>
            ))}
          </Box>
        )

      return (
        <Box flexDirection="column">
          {tabs}
          {back('bands')}
          <Box marginTop={1} flexDirection="row" justifyContent="space-between">
            <Text bold>{worker.name}</Text>
            <Text color={worker.ageSecs > STALE_SECS ? C.accent : C.green}>
              {worker.ageSecs > STALE_SECS ? '□ QUIET' : '■ ONLINE'}
            </Text>
          </Box>
          {worker.description !== '' && (
            <Text dimColor wrap="wrap">
              {worker.description}
            </Text>
          )}
          <Text dimColor>{`build ${worker.build}`}</Text>
          {history.length > 0 && (
            <Box marginTop={1}>
              <Button
                key="worker-sessions"
                hotkey="s"
                label={`s · sessions on ${fit(worker.name, 16)}`}
                onPress={() => listSessions($, { id: worker.id, name: worker.name }, undefined)}
              />
            </Box>
          )}
          {status}
          {hosted('Sites', worker.serves?.sites ?? [])}
          {hosted('Services', worker.serves?.services ?? [])}
          {hostedCount(worker) === 0 && (
            <Box marginTop={1}>
              <Text dimColor>Nothing recorded as hosted here.</Text>
            </Box>
          )}
          {keys('a site opens in the browser · the arrow shows where the tunnel sends it · b back')}
        </Box>
      )
    }

    const bands = groupBands(workers, known).filter(band => band.workers.length > 0)
    const newest = fleetBuild(workers.filter(one => one.band !== HUB_BAND))
    const nameWidth = Math.min(
      22,
      workers.reduce((max, worker) => Math.max(max, worker.name.length), 8),
    )

    return (
      <Box flexDirection="column">
        {tabs}
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold>
            {fetchedAt === 0 ? 'Connecting to rook…' : `${workers.length} workers · ${bands.length} bands`}
          </Text>
          {hosting !== undefined && at.confirm !== 'hosting' && (
            <Button
              key="hosting"
              hotkey="h"
              label="h · sync hosting"
              onPress={() => set($, { ...CLEAR, confirm: 'hosting' })}
            />
          )}
        </Box>
        {error !== undefined && (
          <Text color={C.bad} wrap="wrap">
            {`✗ ${error}`}
          </Text>
        )}
        {hosting !== undefined && at.confirm === 'hosting' && (
          <Box flexDirection="column" marginTop={1}>
            <Text color={C.accent} wrap="wrap">
              {`Sync hosting from ${hosting.worker} (${hosting.cap})? Claude reads the routes, ` +
                'shows a plan and asks you before any serves.set write.'}
            </Text>
            <Box flexDirection="row" columnGap={2}>
              <Button
                key="hosting-yes"
                hotkey="y"
                label="y · start"
                onPress={async () => {
                  await set($, CLEAR)
                  void $.prompt.submit({ text: hostingPrompt(hosting) })
                  void $.ui.toast('rook: asked Claude to plan a hosting sync from the tunnel routes')
                }}
              />
              <Button key="hosting-no" hotkey="n" label="n · cancel" onPress={() => set($, CLEAR)} />
            </Box>
          </Box>
        )}
        {bands.map(band => {
          const stale = band.workers.filter(w => w.ageSecs > STALE_SECS).length

          return (
            <Box flexDirection="column">
              {heading(
                `${band.name ?? band.id}${band.primary ? ' ★' : ''}`,
                `${band.workers.length} workers${stale > 0 ? ` · ${stale} quiet` : ''}`,
              )}
              {band.workers.map((worker: Worker) => {
                const isStale = worker.ageSecs > STALE_SECS
                const history = worker.history ?? []
                const name = worker.name.slice(0, nameWidth).padEnd(nameWidth)
                const detail = [
                  worker.battery
                    ? `${worker.battery.percent}%${worker.battery.charging ? ' charging' : ''}`
                    : '',
                  worker.band !== HUB_BAND && worker.build < newest ? 'OLD BUILD' : '',
                  worker.description,
                ].filter(part => part !== '')

                return (
                  <Box flexDirection="row">
                    <Text color={isStale ? C.accent : C.green}>{isStale ? '□ ' : '■ '}</Text>
                    {history.length === 0 && hostedCount(worker) === 0 ? (
                      <Text>{name}</Text>
                    ) : (
                      <Button
                        key={`w:${worker.id}`}
                        plain
                        label={name}
                        onPress={() =>
                          hostedCount(worker) > 0
                            ? set($, { ...CLEAR, screen: 'worker', worker })
                            : listSessions($, { id: worker.id, name: worker.name }, undefined)
                        }
                      />
                    )}
                    {['claude', 'codex'].map(agent =>
                      history.includes(agent) ? (
                        <Text color={agentIcon(agent).color}>{` ${agentIcon(agent).glyph}`}</Text>
                      ) : (
                        <Text>{'  '}</Text>
                      ),
                    )}
                    <Text color={C.accent}>
                      {hostedCount(worker) > 0 ? ` ⌂${String(hostedCount(worker)).padEnd(2)}` : '    '}
                    </Text>
                    <Text dimColor wrap="truncate-end">
                      {` ${fit(detail.join(' · '), Math.max(4, columns - nameWidth - 13))}`}
                    </Text>
                  </Box>
                )
              })}
            </Box>
          )
        })}
        {keys(
          `■ online  □ quiet  ✻ claude  ◆ codex  ⌂ hosts sites or services · enter opens a marked worker` +
            (hosting !== undefined ? ' · h plans a hosting sync from the tunnel routes' : ''),
        )}
      </Box>
    )
  })
}
