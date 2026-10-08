import { asRecord } from './sessions'

/**
 * The session mirror: this session's events, written to a spool the Rook
 * worker on this host serves as `sessions.mirror`, so the Sessions page can
 * watch a Claude Code started in any terminal (docs/design/sessions.md §3.4).
 *
 * The mod API's file system has no append, so the spool is cut into chunks and the newest is
 * rewritten whole on each flush: `<id>.jsonl`, then `<id>.1.jsonl`, … each
 * up to CHUNK_BYTES. Writes are batched (one per FLUSH_MS at most) and never
 * awaited by a hook, so the turn never waits on the disk.
 */

export const SPOOL_VERSION = 1
export const CHUNK_BYTES = 256 * 1024
/** Chunks kept per session; older ones are emptied (the mod cannot delete). */
export const KEEP_CHUNKS = 64
export const FLUSH_MS = 250
export const INPUT_CLIP = 2_000
export const RESULT_CLIP = 4_000
const TEXT_CLIP = 100_000
const AGENT = 'claude'

/**
 * What the mirror needs from Claude Code, as plain functions: register.tsx
 * builds it from `$` (a module may hand `$` only to its own functions), and a
 * test can build it from anything.
 */
export type MirrorIO = {
  env: (
    name: 'OS' | 'ROOK_WORKER_HOME' | 'USERPROFILE' | 'HOME' | 'USERNAME' | 'USERDOMAIN' | 'CLAUDE_CONFIG_DIR',
  ) => Promise<string | undefined>
  exists: (path: string) => Promise<boolean>
  list: (path: string) => Promise<ReadonlyArray<{ name: string }>>
  read: (path: string) => Promise<string>
  write: (path: string, text: string) => Promise<void>
  run: (argv: readonly string[], init?: { timeoutMs?: number }) => Promise<{ exitCode: number; stdout: string }>
  now: () => Promise<number>
  after: (ms: number, fn: () => void) => void
  sessionId: () => Promise<string>
  cwd: () => Promise<string>
  model: () => Promise<string>
  version: () => Promise<string>
  settings: () => Promise<unknown>
}

export type SpoolEvent = { type: string; ts?: number } & Record<string, unknown>

/** Cuts text to `room` characters, saying how much was cut. */
export function clip(text: string, room: number): string {
  return text.length > room ? `${text.slice(0, room)}… [+${text.length - room} chars]` : text
}

/** UTF-8 length of a string, without TextEncoder. */
export function utf8Bytes(text: string): number {
  let bytes = 0
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)
    if (code < 0x80) bytes += 1
    else if (code < 0x800) bytes += 2
    else if (code >= 0xd800 && code < 0xdc00) {
      bytes += 4
      i++
    } else bytes += 3
  }

  return bytes
}

export const chunkName = (id: string, index: number): string =>
  index === 0 ? `${id}.jsonl` : `${id}.${index}.jsonl`

/** The chunk index a file name holds for this session, or undefined. */
export function chunkIndex(name: string, id: string): number | undefined {
  if (name === `${id}.jsonl`) return 0
  const match = /^(.*)\.(\d{1,6})\.jsonl$/.exec(name)

  return match !== null && match[1] === id ? Number(match[2]) : undefined
}

/** The last whole line's `seq` in a chunk's text; 0 when there is none. */
export function lastSeq(text: string): number {
  const lines = text.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]?.trim() ?? ''
    if (line === '' || i === lines.length - 1) continue // the last piece has no newline yet
    try {
      const seq = asRecord(JSON.parse(line)).seq
      if (typeof seq === 'number') return seq
    } catch {
      // A torn line: look further back.
    }
  }

  return 0
}

const PEER_ORIGINS = new Set(['peer', 'peer-send-message', 'coordinator', 'projects-relay', 'channel'])

/** Who a prompt is from, in the spool's words: another session, or the person. */
export const promptFrom = (kind: string): 'person' | 'peer' => (PEER_ORIGINS.has(kind) ? 'peer' : 'person')

const INBOUND_VALUES = ['accept', 'hold', 'refuse', 'default']

/** `crossSessionInbound` from settings as read; `default` when unset or unknown. */
export function inboundOf(settings: unknown): string {
  const value = asRecord(settings).crossSessionInbound

  return typeof value === 'string' && INBOUND_VALUES.includes(value) ? value : 'default'
}

/** A tool call's own arguments: the event less the keys the engine reserves. */
export function toolInput(e: unknown): string {
  const { tool: _tool, tool_use_id: _id, consent: _consent, agentId: _agent, ...rest } = asRecord(e)
  try {
    return clip(JSON.stringify(rest), INPUT_CLIP)
  } catch {
    return ''
  }
}

/** A tool result as one text: the model's view of it, the refusal, or the record. */
export function toolOutput(result: unknown): { ok: boolean; text: string } {
  const r = asRecord(result)
  if (typeof r.deny === 'string') return { ok: false, text: clip(r.deny, RESULT_CLIP) }
  let text = typeof r.text === 'string' ? r.text : ''
  if (text === '' && r.result !== undefined) {
    try {
      text = typeof r.result === 'string' ? r.result : JSON.stringify(r.result)
    } catch {
      text = ''
    }
  }

  return { ok: r.isError !== true, text: clip(text, RESULT_CLIP) }
}

export type Place = {
  /** The worker's state folder: `ROOK_WORKER_HOME`, else `~/.rook-band-worker`. */
  state: string
  sep: string
  isWindows: boolean
}

/** Where this host's worker keeps its state, or undefined when there is no worker. */
export async function workerPlace(io: MirrorIO): Promise<Place | undefined> {
  const isWindows = (await io.env('OS')) === 'Windows_NT'
  const sep = isWindows ? '\\' : '/'
  const override = (await io.env('ROOK_WORKER_HOME'))?.trim()
  const home = isWindows
    ? ((await io.env('USERPROFILE')) ?? (await io.env('HOME')))
    : ((await io.env('HOME')) ?? (await io.env('USERPROFILE')))
  const state =
    override !== undefined && override !== ''
      ? override.replace(/[\\/]+$/, '')
      : home === undefined || home === ''
        ? undefined
        : `${home.replace(/[\\/]+$/, '')}${sep}.rook-band-worker`
  if (state === undefined) return undefined
  try {
    if (!(await io.exists(state))) return undefined
  } catch {
    return undefined
  }

  return { state, sep, isWindows }
}

/** The pid Claude Code recorded for this session in its PID markers, if any. */
export async function sessionPid(
  io: MirrorIO,
  claudeDir: string,
  sessionId: string,
): Promise<number | undefined> {
  try {
    const dir = `${claudeDir}/sessions`
    const entries = (await io.list(dir)).filter(entry => entry.name.endsWith('.json')).slice(0, 200)
    for (const entry of entries) {
      try {
        const marker = asRecord(JSON.parse(await io.read(`${dir}/${entry.name}`)))
        const id = marker.sessionId ?? marker.session_id
        const pid = Number(marker.pid ?? entry.name.replace(/\.json$/, ''))
        if (id === sessionId && Number.isInteger(pid) && pid > 0) return pid
      } catch {
        // Another session's marker, mid-write: skip it.
      }
    }
  } catch {
    // No markers on this build or platform.
  }

  return undefined
}

/** Claude Code's configuration folder: `CLAUDE_CONFIG_DIR`, else `~/.claude`. */
export async function claudeDir(io: MirrorIO): Promise<string> {
  const set = await io.env('CLAUDE_CONFIG_DIR')
  if (set !== undefined && set !== '') return set
  const home = (await io.env('HOME')) ?? (await io.env('USERPROFILE')) ?? ''

  return `${home}/.claude`
}

const wallClock = (): number | undefined => (typeof Date === 'undefined' ? undefined : Date.now())

/** The SID in `whoami /user /fo csv /nh` output (`"domain\\user","S-1-5-21-…"`). */
export function parseSid(stdout: string): string | undefined {
  return /\bS-1-5-[0-9-]+\b/.exec(stdout)?.[0]
}

/** The SID for LocalSystem: a band worker runs as the person (a logon task), a service as SYSTEM. */
export const SYSTEM_SID = 'S-1-5-18'

/**
 * The `icacls` call that makes the spool folder the person's and SYSTEM's
 * alone: inheritance off, full control for the person (by SID, so a domain
 * account is never ambiguous; `DOMAIN\user` when the SID is unknown) and for
 * SYSTEM, so a worker running as either can read it. Undefined when the
 * person cannot be named: then the folder keeps what it inherits.
 */
export function windowsAcl(
  root: string,
  sid: string | undefined,
  domain: string | undefined,
  user: string | undefined,
): string[] | undefined {
  const who =
    sid !== undefined
      ? `*${sid}`
      : user !== undefined && user !== ''
        ? domain !== undefined && domain !== ''
          ? `${domain}\\${user}`
          : undefined
        : undefined
  if (who === undefined) return undefined

  return ['icacls', root, '/inheritance:r', '/grant:r', `${who}:(OI)(CI)F`, `*${SYSTEM_SID}:(OI)(CI)F`, '/T', '/Q']
}

/**
 * One session's spool: batches events and writes them, in order, never in a
 * hook's way. Plain data; the functions below take the hook's MirrorIO.
 */
export type Spool = {
  place: Place
  id: string
  seq: number
  chunk: number
  text: string
  pending: SpoolEvent[]
  delta: string
  isReady: boolean
  isScheduled: boolean
  isSecured: boolean
  secured: Set<number>
  chain: Promise<void>
}

export const newSpool = (place: Place, id: string): Spool => ({
  place,
  id,
  seq: 0,
  chunk: 0,
  text: '',
  pending: [],
  delta: '',
  isReady: false,
  isScheduled: false,
  isSecured: false,
  secured: new Set(),
  chain: Promise.resolve(),
})

const spoolRoot = (s: Spool): string => `${s.place.state}${s.place.sep}mirror`
const spoolFolder = (s: Spool): string => `${spoolRoot(s)}${s.place.sep}${AGENT}`
export const spoolPath = (s: Spool, index: number): string =>
  `${spoolFolder(s)}${s.place.sep}${chunkName(s.id, index)}`

/** Picks up where an earlier load of the mod (or of this session) left the spool. */
async function openSpool(io: MirrorIO, s: Spool): Promise<void> {
  try {
    const indexes = (await io.list(spoolFolder(s)))
      .map(entry => chunkIndex(entry.name, s.id))
      .filter((index): index is number => index !== undefined)
    if (indexes.length > 0) {
      s.chunk = Math.max(...indexes)
      const text = await io.read(spoolPath(s, s.chunk))
      // A torn tail from a crash mid-write is dropped; whole lines are kept.
      s.text = text.slice(0, text.lastIndexOf('\n') + 1)
      s.seq = lastSeq(s.text)
      for (let index = s.chunk - 1; s.seq === 0 && index >= 0; index--) {
        s.seq = lastSeq(await io.read(spoolPath(s, index)).catch(() => ''))
      }
    }
  } catch {
    // No folder yet: a fresh spool.
  }
  s.isReady = true
  schedule(io, s)
}

function closeDelta(s: Spool): void {
  if (s.delta === '') return
  s.pending.push({ type: 'assistant.delta', text: s.delta, ts: wallClock() })
  s.delta = ''
}

function pushEvent(io: MirrorIO, s: Spool, event: SpoolEvent): void {
  closeDelta(s)
  s.pending.push({ ...event, ts: event.ts ?? wallClock() })
  schedule(io, s)
}

function schedule(io: MirrorIO, s: Spool): void {
  if (s.isScheduled || !s.isReady) return
  s.isScheduled = true
  io.after(FLUSH_MS, () => {
    s.isScheduled = false
    void flushSpool(io, s)
  })
}

/** Writes what is pending; resolves once it is on disk. Flushes run one at a time. */
export function flushSpool(io: MirrorIO, s: Spool): Promise<void> {
  s.chain = s.chain.then(() => writeSpool(io, s)).catch(() => undefined)

  return s.chain
}

async function writeSpool(io: MirrorIO, s: Spool): Promise<void> {
  if (!s.isReady) return
  closeDelta(s)
  if (s.pending.length === 0) return
  const now = await io.now()
  const events = s.pending
  s.pending = []
  const writes: Array<[number, string]> = []
  for (const event of events) {
    const line = `${JSON.stringify({ v: SPOOL_VERSION, seq: s.seq + 1, ...event, ts: (event.ts ?? now) / 1000 })}\n`
    if (s.text !== '' && utf8Bytes(s.text) + utf8Bytes(line) > CHUNK_BYTES) {
      writes.push([s.chunk, s.text])
      s.chunk += 1
      s.text = ''
      if (s.chunk >= KEEP_CHUNKS) writes.push([s.chunk - KEEP_CHUNKS, ''])
    }
    s.text += line
    s.seq += 1
  }
  writes.push([s.chunk, s.text])
  await secureFolder(io, s)
  for (const [index, text] of writes) {
    await io.write(spoolPath(s, index), text)
    if (text !== '' && !s.secured.has(index)) {
      s.secured.add(index)
      await secureFile(io, s, spoolPath(s, index))
    }
  }
}

/** The spool folder is the owner's alone: 0700 on POSIX, an owner-only ACL on Windows. */
async function secureFolder(io: MirrorIO, s: Spool): Promise<void> {
  if (s.isSecured) return
  s.isSecured = true
  try {
    if (s.place.isWindows) {
      await io.write(`${spoolFolder(s)}${s.place.sep}.keep`, '')
      const whoami = await io.run(['whoami', '/user', '/fo', 'csv', '/nh'], { timeoutMs: 10_000 }).catch(() => undefined)
      const argv = windowsAcl(
        spoolRoot(s),
        whoami?.exitCode === 0 ? parseSid(whoami.stdout) : undefined,
        await io.env('USERDOMAIN'),
        await io.env('USERNAME'),
      )
      if (argv !== undefined) await io.run(argv, { timeoutMs: 10_000 })
    } else {
      await io.run(
        ['sh', '-c', 'umask 077 && mkdir -p "$1" && chmod 700 "$2" "$1"', 'sh', spoolFolder(s), spoolRoot(s)],
        { timeoutMs: 10_000 },
      )
    }
  } catch {
    // Best effort: the files are still written.
  }
}

async function secureFile(io: MirrorIO, s: Spool, path: string): Promise<void> {
  if (s.place.isWindows) return // inherits the folder's owner-only ACL
  try {
    await io.run(['chmod', '600', path], { timeoutMs: 10_000 })
  } catch {
    // Best effort: the folder is already 0700.
  }
}

/**
 * This process's mirror: one spool at a time, the current session's. A
 * `/clear` or `/resume` ends one session and goes on under another; the next
 * event opens the new one's spool.
 */
export type Mirror = {
  spool?: Spool
  opening?: Promise<Spool | undefined>
  isOff: boolean
  /** Events that came while the spool was opening. */
  early: SpoolEvent[]
}

export const newMirror = (): Mirror => ({ isOff: true, early: [] })

/** Starts mirroring this session; nothing is written when no worker lives here. */
export function startMirror(io: MirrorIO, m: Mirror, cwd: string): void {
  m.isOff = false
  m.spool = undefined
  m.early = []
  m.opening = beginMirror(io, m, cwd)
}

async function beginMirror(io: MirrorIO, m: Mirror, knownCwd?: string): Promise<Spool | undefined> {
  try {
    const place = await workerPlace(io)
    const id = await io.sessionId()
    if (place === undefined || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(id)) {
      m.isOff = true

      return undefined
    }
    const s = newSpool(place, id)
    const [cwd, model, version, settings, pid] = await Promise.all([
      knownCwd ?? io.cwd(),
      io.model().catch(() => undefined),
      io.version().catch(() => undefined),
      io.settings().catch(() => ({})),
      claudeDir(io).then(dir => sessionPid(io, dir, id)),
    ])
    pushEvent(io, s, {
      type: 'session.start',
      cwd,
      ...(model !== undefined && { model }),
      pid: pid ?? null,
      version: version ?? null,
      inbound: inboundOf(settings),
    })
    for (const event of m.early) pushEvent(io, s, event)
    m.early = []
    await openSpool(io, s)
    m.spool = s

    return s
  } catch {
    m.isOff = true

    return undefined
  }
}

/** Adds an event; after a /clear, the first one opens the new session's spool. */
export function emit(io: MirrorIO, m: Mirror, event: SpoolEvent): void {
  if (m.isOff) return
  if (m.spool !== undefined) return pushEvent(io, m.spool, event)
  m.early.push({ ...event, ts: event.ts ?? wallClock() })
  if (m.opening === undefined) m.opening = beginMirror(io, m)
}

/** Streamed text: consecutive pieces become one `assistant.delta` per flush. */
export function emitDelta(io: MirrorIO, m: Mirror, text: string): void {
  if (m.isOff) return
  if (m.spool === undefined) return emit(io, m, { type: 'assistant.delta', text })
  m.spool.delta += text
  schedule(io, m.spool)
}

export const emitPrompt = (io: MirrorIO, m: Mirror, text: string, origin: string): void =>
  emit(io, m, { type: 'prompt', text: clip(text, TEXT_CLIP), from: promptFrom(origin), origin })

export const emitState = (io: MirrorIO, m: Mirror, state: 'working' | 'idle' | 'waiting'): void =>
  emit(io, m, { type: 'state', state })

/** Writes `session.end` and waits for it to land. */
export async function endMirror(io: MirrorIO, m: Mirror, reason: string): Promise<void> {
  if (m.isOff) return
  const s = m.spool ?? (await m.opening)
  m.spool = undefined
  m.opening = undefined
  // /clear and /resume go on under another session id; any other end is the process's.
  if (reason !== 'clear' && reason !== 'resume') m.isOff = true
  if (s === undefined) return
  pushEvent(io, s, { type: 'session.end', reason })
  await flushSpool(io, s)
}

/** Resolves once what is pending is on disk (for tests, and before an exit). */
export async function settleMirror(io: MirrorIO, m: Mirror): Promise<void> {
  const s = m.spool ?? (await m.opening)
  if (s !== undefined) await flushSpool(io, s)
}

export const assistantText = (text: string): string => clip(text, TEXT_CLIP)
