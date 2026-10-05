/** Something a worker hosts: a public site or a service, as written on the hub. */
export type Hosted = { name: string; url: string; note: string }

export type Worker = {
  id: string
  name: string
  description: string
  band: string
  build: number
  ageSecs: number
  battery?: { percent: number; charging: boolean }
  /** The agents whose session history this worker serves: `claude`, `codex`. */
  history: string[]
  /** What it hosts (`serves` on its rook_workers row); absent when nothing is recorded. */
  serves?: { sites: Hosted[]; services: Hosted[] }
}

export type SessionMeta = {
  id: string
  title: string
  /** `claude` or `codex`: the client whose history holds it. */
  agent: string
  workerId: string
  workerName: string
  /** Epoch seconds; 0 when the source does not say (a search hit). */
  modified: number
  /** 0 when the source does not say (a search hit). */
  count: number
  cwd: string
  active: boolean
  messageable: boolean
  snippet?: string
  matches?: number
}

export type SessionMessage = { role: string; text: string }

export type DeckTask = { id: string; title: string; state: string; excerpt: string }

export type DeckProject = { title: string; tasks: DeckTask[]; done: number }

export type Handoff = {
  threadId: string
  goal: string
  asOf: string
  author: string
  nextSteps: string[]
  artifacts: string[]
}

/** A task or a handoff opened from the deck. */
export type Item = { title: string; meta: string; body: string; claimId?: string }

export type View = {
  tab?: 'bands' | 'sessions' | 'deck'
  screen?: string
  scope?: { id: string; name: string }
  query?: string
  sessions?: SessionMeta[]
  session?: SessionMeta
  /** The worker whose detail screen is open on the bands tab. */
  worker?: Worker
  messages?: SessionMessage[]
  /** The `follow` version of the open session's log, to ask only for changes. */
  version?: string
  /** The proc handle of the open session when this worker relaunched it. */
  handle?: string
  deck?: DeckProject[]
  handoffs?: Handoff[]
  item?: Item
  busy?: boolean
  error?: string
  note?: string
}

export type BandInfo = { id: string; label: string; name: string; primary: boolean }

export type Roster = {
  workers: Worker[]
  bands?: BandInfo[]
  fetchedAt: number
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    rook: { roster: Roster; view: View }
  }
}
