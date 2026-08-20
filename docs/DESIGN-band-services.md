# Band Services — Design

*Settled 2026-08-20 in conversation between jamix and Claude. This is the durable
record; the conversation it came from is gone. Statuses below track implementation.*

## First principles

1. **The hub stays dumb.** The telesthete hub is a band_id forwarder: no keys, no
   storage, no smarts. Everything stateful lives on the **site** (the Python
   `CombinedServer` — installer, dashboard, band client — currently
   rook.bakeforge.com). Site + hub ship, install, and configure as **one unit**,
   but the roles never blur.
2. **Workers don't care how it gets there.** Configuration reaches workers the
   same way code does: signed, over the band, automatically. A worker is
   installed once and never hand-tended again.
3. **Acting agents are never asked to do maintenance.** Every design below moves
   bookkeeping (memory upkeep, output capture, audit) out of the acting agent's
   loop and into automatic capture or a dedicated curator.
4. **Identity is breadcrumbs first, walls second.** Default-open with an audit
   trail; ACLs come later on the same envelope field.

---

## 1. Settings wizard + config OTA ("commit-confirmed")

- The site's first-run wizard and settings page cover **every** site/worker
  setting, not just PSK/domain. Changing a setting in the web UI is the only
  admin gesture; workers converge on their own.
- Config is versioned by **epoch** and rides the existing signed-manifest
  machinery (same trust channel as OTA builds). Dashboard shows config epoch
  next to build number per worker.
- **Dangerland fields only** (hub address, PSK, ports — anything that can strand
  a worker) get the commit-confirmed dance: worker applies epoch N+1, must
  re-establish contact with the site *under the new config* within a window
  (~60s), else reverts to N and reports the failure. Cosmetic settings apply
  directly. Sometimes we break eggs — the revert path is the safety net, not a
  guarantee.
- Coordinated moves (e.g. everyone to a new hub) require old + new endpoints
  alive during the window. That's an ops procedure; the wizard warns on those
  fields.

## 2. Call output journal

- Every `rook_call` gets a call ID. Full output is captured server-side into a
  **size-capped ring buffer per worker** (oldest evicted; cap by size, not age).
- Queryable by call ID / worker / time range. Long-running or failed work never
  falls into the ether — the tool call may time out, the journal keeps rolling.
- Journal entries carry the **thread ID** when the call originated from a chat
  room, stitching "what did that agent actually do" into the conversation
  record.

## 3. Chat v2 — rooms, voicemail, wake

### Rooms
- Presence registry of online identities (TTL-based). Anyone starts a room and
  invites participants. Room = session = thread ID.
- **Mention is routing metadata on the send call, not message text.** In a
  2-party room every message implicitly addresses the other party; in 3+ rooms
  only mentioned participants are expected to respond. Mentioning a
  non-participant **auto-invites** them.
- `expects_reply` flag on sends as a first-pass tuning knob.
- **No housekeeping.** Rooms have `last_activity` and sort by it; stale rooms
  sink naturally. Nothing is archived or closed by machinery.
- Room lifespan is socially tied to the initiator's local session (a new claude
  code session starts a new room), but nothing enforces this.

### Voicemail — delivery is passive by default
- Message to an offline identity → deposited. On the recipient's **next rook
  MCP call for any reason**, the tool-response envelope carries
  `unread: [{thread_id, from, count, last_at}]`. `chat.read(thread_id)`
  collects. No polling, no push, no new transport; covers web agents, CLI
  agents, and agents offline for a week identically.

### Wake — explicit, a cap, not a chat feature
- `agent.wake` is a **worker capability**: workers with machine access to spawn
  an agent (hermes session, `claude -p`) expose it; web agents can't be woken
  and that's fine. Mention ≠ wake; wake is a deliberate act by the sender.
- **Asymmetric roles.** The *caller* stays in tool-land: `chat.send` is a tool
  call, replies arrive as tool results, its own agentic loop terminates the
  exchange. The *woken agent* gets a **normal session** where room messages
  arrive as user turns (attributed `[sojourn]: ...` in multi-party rooms) and
  its assistant output posts back to the room. Each end sees its native
  interface; the room is transport. Loop prevention is structural: the woken
  side only speaks when spoken to, because that's what responding to user turns
  is.
- Wake requests targeting an **already-online** agent are black-holed — the
  presence registry knows; the message lands as live delivery/voicemail
  instead, which is better than interrupting mid-task.
- A woken agent's backlog is its opening prompt (no unread badge needed).

### Site chat UI
- The site web UI grows a chat panel mirroring the rook CLI chat client, so the
  user has direct access to every room from anywhere.

## 4. Identity — tokens, audit-first

- **Agents:** the existing bakeforge bearer-token registry (`/tokens`,
  `TokenStore`, also the OAuth-shim client secret for claude.ai web) is the
  identity registry. A token's **name** is the agent's identity, shaped
  `[agent]_[hostname]` by convention. No certs, no new crypto — "secure
  enough."
- **Users:** username/password registered on the site. Must be easy from an SSH
  session on a phone.
- Every band request envelope carries the caller's identity name. Workers log
  it; the site keeps an **append-only audit log**; a `log.audit` cap answers
  "who did what when."
- **Delegation via wake chains, not crypto:** a spawned agent runs under its own
  token, but the wake record says `woken_by: <identity> (user: <user>)`.
  Breadcrumbs compose across hops.
- ACLs (per-cap identity allowlists) come later; the envelope field is the only
  prerequisite, so it lands first.

## 5. Memory — vault, post-its, curator, capstones

### Vault
- Plain-markdown, **Obsidian-compatible** vault on the site. Folders are
  namespaces (`sojourn/`, `claude/`, `shared/`); `[[wikilinks]]` intact; FTS
  index (sqlite FTS5) rebuilt on write. No Obsidian process — the real app can
  be pointed at it any time.
- Access: **read-open across the band, write-owned to your namespace**,
  `shared/` writable by all. Tightens for free once ACLs land.
- Caps: `memory.search`, `memory.get`, `memory.put`, `memory.note` (drop a
  post-it explicitly).
- Sojourn (and any agent) syncs its native memory in by cron; **different
  schemas coexist** — the vault doesn't impose one.

### Post-its — atomic facts ("factbuilding")
The unit of curated memory is the **post-it**: an append-only, immutable,
atomic record.

```
id, ts, thread_id (provenance), subjects ([[entities]]),
claim (one sentence), kind (decision|fact|change|question|capstone),
supersedes: [ids]?, author (identity)
```

- **Entities vs events:** `entities/` notes hold current state and are amended;
  everything else is historical record. State of the world lives in entity
  notes; sessions record what happened.
- **Curator, not discipline:** acting agents are never relied on for memory
  maintenance (they demonstrably don't do it). Their work is *observable* —
  journal, rooms, handoffs land in the threads store automatically. An
  out-of-band **curator** (sojourn — free local inference) runs on cron +
  thread-idle triggers, reads the delta since its last pass, extracts post-its,
  amends entity notes, notices **loose ends** ("LXC created for calendar duty;
  calendar moved to gcal; nothing decommissioned it — orphan?"), and flags
  contradictions it can't resolve to a review queue (site UI panel + voicemail
  to the user — same write).
- Curator writes under its own token identity, **appends only, never deletes**;
  its summaries are labeled and carry provenance links. Curator error is
  contestable, not silent.

### Read path — the pile, not the dossier
- `memory.search` returns a small pile of relevant post-its in **temporal
  order**, supersede edges resolved, plus raw vault hits. **No server-side
  inference.** The *consuming agent* summarizes the pile in its own context —
  it's already running, and critically it's the only participant in a live
  conversation with a human, which is the only place adjudication can happen.
- Atomicity is load-bearing: prose smooths contradictions over; atomic notes in
  date order make a contradiction two adjacent lines that visibly disagree. The
  format refuses to pre-reconcile.
- One cheap server-side assist (pure graph heuristic, no inference): flag piles
  where notes share subject entities across a time gap with no supersede chain
  — `⚠ 4 notes touch [[calendar]] across 5 months, no resolution chain`.
- `memory.ask` (later): routes a question to sojourn for live RAG over the
  corpus — "go ask sojourn," formalized as a cap.

### Capstones — truth set by ruling
- When the consuming agent surfaces a contradiction and the **user rules**, it
  writes a `kind: capstone` post-it: claim = the ruling, supersedes = the
  conflicting ids, provenance = the thread where the ruling happened. Future
  searches return the capstone atop the pile with superseded notes folded
  beneath. The capstone is just another post-it — the system absorbs its own
  resolutions with no special machinery.
- Known residual hole: changes nobody wrote down anywhere are invisible until
  some agent notices and writes a post-it — same hole any memory has.

*Nearest prior art, for the record: generative-agents memory stream (atomic
timestamped memories + reflection) and event sourcing (append-only events,
truth as a fold). The novel part is the adjudication loop: surface the
contradiction to a live human, capture the ruling as the terminal write.*

## 6. Hub federation — registry pooling

- **Sharing hub initiates** an offer to a consuming hub. Consumer stores the
  link **default-revoked**; an admin enables it. Default accept-state is
  configurable (auto-link worlds exist).
- A per-link secret is established at offer time so enabled links can
  authenticate frames.
- **Pooled registries ARE forwarding rules.** Workers connect *out* to their
  hub, so a pooled entry is reachable only *through* its home hub: discovery
  sharing without traffic forwarding would be useless. Cross-hub traffic:
  worker → hub B → hub A → worker.
- Loop safety, one rule: **hubs never re-forward hub-sourced frames.** One hop
  across the mesh, ever. Flat pool, no spanning tree.
- Scoping is by **band name** — a "subdomain" is just a band you named for the
  purpose. Name collisions across pooled registries coexist; band_id keeps them
  from actually colliding. Discovery is not hub-aware.

## 7. Handoffs + universal sessions

- Full-transcript resume across tools is a non-goal. A **handoff record** is:
  `thread_id, as_of, status (active|superseded|closed), supersedes, goal,
  current state, decisions, artifacts touched, next steps` (+ raw transcript
  attached for reference).
- **The read path does the freshness work:** fetching a handoff always surfaces
  "as_of 6 days ago, superseded by X on Tuesday" *in the response*, so stale
  state can't be mistaken for current without the staleness being in context.
- **Rooms, journal entries, handoffs, and post-its share the thread ID
  registry.** One threads store, several entry types. Picking up a session =
  fetch thread → latest handoff + room tail + journal refs. Universal sessions
  mostly falls out of the other features.

---

## Build order (dependencies, not preference)

1. **Identity threading** — token names into the band envelope + audit log.
   Small; everything else wants it. *(in progress)*
2. **Settings wizard + config OTA** (commit-confirmed).
3. **Journal** — immediate pain relief, no dependencies.
4. **Chat v2** — rooms, voicemail, wake cap, site chat UI.
5. **Memory vault** — post-its, indexes, caps; curator cron interface documented
   for sojourn.
6. **Federation** — independent; Rust/telesthete side.
7. **Handoffs** — mostly formalization; threads exist by then.

## Deployment notes

- Site pieces deploy to bakenetca per `bakenetca-deploy` memory (pre-flight
  `import rook.band_mcp.server` / bootstrap format checks before restarting
  services — there is a history of venv landmines).
- Worker-side pieces ship as signed OTA builds; fleet converges automatically.
- The curator cron is configured on sojourn by hand (hermes owns its own box).
