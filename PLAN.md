# Rook Core Plan

Decided with Bake, 12 November 2026. LOCKED 12 November 2026. Amended 12 November 2026 by Bake: Telesthete is updated additively to accommodate this design, the Rook hub implements the Telesthete relay as its base, and user login is by password for now. Amended 13 November 2026 by Bake: the Rook hub is its own Telesthete terminating endpoint, one process with no relay (Topology; docs/link-design.md, D8). Core only. Agents and Workbench are separate plans built on top once core is tested. Claude (the voice session that wrote this) reviews every PR against this document; coding agents build, they do not decide.

## Build rules

- Core is written from scratch. The existing Rook repo is reference only; no code is carried forward.
- Nothing is built that is not in this document. Anything new is proposed, signed by Bake, and added here first.
- No gates, limits, expiries, approvals or review processes until core is stable. Hardening is a later pass. The one exception is the knock-and-approve step for new workers (R2), which stays.
- Core is tested as the minimal power level before anything is built on it.
- Rook work happens only in the mainline Bake-Ware/rook repo. Stale worktrees in `.worktrees` and `.claude/worktrees` are to be removed.
- Coding agents run in auto permission mode. No half starts.

## Layering

- Telesthete is the vendor spec Rook lives on. It owns framing, encryption, sessions, transport modes (UDP, WebSocket fallback), discovery, NAT traversal and relay.
- Rook owns workers, caps, calls, streams, bands and access.
- Rook pins a Telesthete version and uses only its public surface. Telesthete is updated, additively only, to accommodate this design; this design is the reason both projects exist. Existing Telesthete behaviour does not change. Keys: Telesthete authenticates frames with a key the application supplies; Rook decides which keys are accepted.

## Topology

- Hub and spoke only. Every worker holds one connection to its hub; calls route through the hub. The Rook hub is its own Telesthete terminating endpoint: one process, no relay.
- Uptime is the host's problem. A free Oracle VM is the recommended path in the setup docs.
- Peer to peer is a Telesthete capability, not Rook architecture. Rook may use it later for heavy streams with the hub as introducer.
- Hubs have names. The whole system is "the hub".

## Workers

- **W1.** The protocol defines messages only. Any implementation that speaks them is a worker, hardware or software.
- **W2.** Minimum worker: register, heartbeat, answer calls. Streams are optional, so an ESP32 qualifies.
- **W3.** Caps carry a schema and a one-line description. Dot-separated names (`shell.exec`); hyphens only inside a segment.
- **W4.** Hub-decided attributes (owner, band name, opt-outs, shares) live on the hub record. The worker never sends them.
- **W5.** Workers announce everything they have; calls that cannot be served fail with a reason.
- **W6.** Every worker has an owner: the user who approved it or whose login provisioned it. Reassignable. Owner is a fact, not a gate, this round.
- **W7.** Identity and credential are separate. Identity is the worker's GUID and three-word moniker, minted by the hub at first approval and persisted by the bootstrap in state that survives core reinstalls. The key is a credential on that identity: rekeying attaches a new key and revokes the old; owner, config, shares and journal history stay. The hub keeps a seen-workers record per identity: monikers, every MAC address reported, hostnames, keys held with dates. A fresh knock whose MAC or name matches a known identity is flagged in the pending view as a likely rekey; attaching it is a deliberate act by a person or agent, never automatic.

## Heartbeat

- **H1.** The heartbeat is the only announce. Every heartbeat carries the full state: identity (name, key), facts (OS, arch, hostname, worker form, version), caps, band fingerprint, health. Connect is the first heartbeat. A change sends a heartbeat immediately.
- **H2.** The hub sets the heartbeat interval per worker and sends it back on connect.
- **H3.** Miss three heartbeats and the worker is marked offline; its record and caps are kept.
- **H4.** Health block, filled as far as the device can: CPU, RAM, disk, GPU, VRAM, NPU, battery, temperature. An ESP32 sends free heap and uptime.
- **H5.** The hub keeps heartbeat history: raw for a day, hourly for a month.
- **LATER.** Per-host service status, as a plugin.

## Delegates and devices

- **D1.** A device is anything with an API that cannot host a worker. It appears to agents as a worker with caps.
- **D2.** Drivers run at the hub. A driver turns a cap call into network primitives.
- **D3.** Every worker exposes dumb network primitives (HTTP request, one-shot TCP/UDP send) and can serve as the jump box for devices on its band. This includes ESP32 workers, which are full workers with their own firmware and caps; the primitives are an addition, not their role.
- **D4.** Worker records carry a route: direct, pinned (via worker X, for anything bound to hardware or a held connection), or floating (via any eligible worker on the device's band).
- **D5.** Floating selection: last worker that succeeded, else the next eligible one, one retry. No load balancing.
- **D6.** Device status is polled by the hub through the same route, at an interval set per device. Devices have no heartbeat of their own.
- **D7.** Device config and credentials live at the hub.
- **D8.** Per-worker opt-out from serving devices. Default on. Administered on the worker's row.

## Bands

- **B1.** A band is a physical network. Membership is automatic.
- **B2.** A band is a set of gateway MAC addresses the user has named. Match is exact on gateway MAC; subnet and SSID are shown as context, never used to match. A new gateway MAC is unrecognised: the hub prompts to add it to an existing band and never guesses. Cellular has no gateway MAC, so a phone on LTE is roaming unless the user names that carrier as a band. In one sentence: a band is a router.
- **B3.** Workers on unnamed networks show as roaming. Unnamed networks do not become bands.
- **B4.** Bands carry no permissions.
- **B5.** Agents see each worker's band and roaming state. Presence detection is LATER.
- **B6.** One band per worker at a time.

## Hubs and federation

- **F1.** A worker belongs to exactly one hub.
- **F2.** Bands exist per hub, discovered independently. Same network, same name; no sharing protocol.
- **F3.** Hub data is portable: backup, restore, move. Failover and whole-hub federation are LATER.
- **F4.** Hubs federate by sharing individual workers. A share is a worker plus allowed caps; default is everything the owner can do, including shell and streams, and the owner trims from there.
- **F5.** Share flow: pick worker, tick caps, hub mints an invite link; receiver pastes it into "Add shared worker" on their hub; hubs exchange identities once.
- **F6.** Calls go hub to hub. The owning hub enforces the cap list and journals every call. Owner can change or revoke any time; receiver can drop.
- **F7.** Shared workers show on the receiving hub as remote via the owning hub, outside any band.
- **F8.** The share cap list is the only permission mechanism this round.

## Caps, calls and streams

- **C1.** Caps are agent-facing. The measure is how much an agent gets done per call.
- **C2.** The MCP surface stays small and generic: list workers, list caps, call, stream, read journal. Caps are discovered, not registered as tools.
- **C3.** Errors say what to try next.
- **S1.** A cap declares whether it returns a result or opens a stream. Opening a stream is a normal cap call that returns a stream handle.
- **S2.** Workers own streams. The hub fans out to any number of attached clients: agents, browser, apps. A stream outlives whoever opened it.
- **S3.** Streams have a title and a description, free text. No reference IDs.
- **S4.** Streams never time out. They live until intentionally closed. Open stream count is part of worker status.
- **S5.** Housekeeping is semantic: an agent on a job reads titles and descriptions and closes what is done.

## Registration

- **R1.** Install is one command; the hub URL is the only input.
- **R2.** Default is knock and approve. The worker knocks, sits pending, and is approved from any client, including an agent through MCP.
- **R3.** Tokens for unattended joins. Any authenticated agent can mint one. Dongle scenario: the dongle is itself a worker (ESP32-S3, tty and HID to the attached host) with its own identity and key; press its button and it installs a worker on the attached host from the core payload it carries, then mints a fresh one-use join token through its own identity and hands that to the new worker. The host never receives the dongle's key or token, and the new worker is owned by the dongle's owner.
- **R4.** A pending worker shows its name and offered caps, read-only, before approval.
- **R5.** The worker generates a keypair locally and knocks with the public half. Approval is the hub signing that key and, for a new worker, minting its identity (W7). The key is the credential; either side can revoke it, and rekeying attaches a new key to the same identity. No re-pairing after approval.

## Auth and tokens

- **T1.** One token mechanism, JWT, for everything: agent MCP access, unattended joins, dongle provisioning, app devices.
- **T2.** Purpose, expiry and scope are claims. A token may carry several scopes (a desktop app is worker and client at once). Defaults are set by the hub owner, not by agents: agent tokens long-lived and revocable; join tokens minutes and one use; dongle tokens tied to the device.
- **T3.** Humans never type tokens. Humans approve; agents and devices carry tokens. The one exception is copying the first MCP token from the hub page.
- **T4.** Two front doors: user login for humans, token for everything else. Nothing else authenticates. User login is by password for now.
- **T5.** Every token is owned by a user. A user mints directly; an agent mints under the user who owns the agent's own token, so ownership always chains back to a person. Revoking a user revokes every token in that chain.
- **T6.** The Auth page shows users, and tokens with scope, owner and last-used.

## Jobs and events

- **J1.** A job is an event match plus one cap call with fixed arguments, plus title and description.
- **J2.** One event bus. Sources: hub state changes (worker online or offline, band change, stream open or close, token used), schedule, webhooks (a URL on the hub, token-scoped), workers (a cap can emit), agents (through MCP).
- **J3.** Every job completion emits success or fail with the result; jobs can match on those, so jobs chain.
- **J4.** No recursion limit. Cycles are detected and shown as a warning when a chain is saved or viewed.
- **J5.** Chain runs share a run ID so any view can group them.
- **J6.** No workflow DSL in the hub. Multi-step work is a cap that runs steps in code and emits events.
- **J7.** Jobs run at the hub. Agents create, pause and delete jobs through MCP. Delete is a hard delete: the job row is gone, no soft-delete flag, no archive. Its past runs stay in the journal.
- **J8.** Events are journaled.

## Secrets

- **X1.** Secrets are scope plus name. Scope is a worker, a band, the hub, or a plain string. Resolution walks worker, then band, then hub.
- **X2.** Encrypted at the hub. Used by reference (`secret:name`) in call arguments and config; the hub substitutes in flight. Values never appear in the journal.
- **X3.** Agents and jobs can create and reference secrets; only users can view values. The Secrets page has a reveal button on every secret that shows its value to the signed-in user; the reveal is journaled.
- **X4.** The vault key is separate from hub data and unlocked at start. Backups are encrypted.
- **X5.** No worker-side vault. The single attack surface is accepted and addressed in the hardening pass.
- **X6.** Designed to hold thousands: indexed scope and name, hashed values for scrubbing.

## Journal

- **L1.** One flat journal in a database table: calls, streams, events, job runs, auth. Each entry carries actor (token and user), worker, cap, why text, result.
- **L2.** Searchable, and readable by agents through MCP.
- **L3.** Retention is a setting. The hub never summarizes; an agent does.
- **L4.** Scrubbed on write for known secret shapes and for vault values. Treated as a reducer, not a guarantee.
- **L5.** A redact cap is available to agents and the site; it replaces a value everywhere in the journal and stream recordings.
- **L6.** The scrubber emits `secret.detected` carrying the value. That event is never journaled and is delivered only to secrets-scoped tokens, so a job can wake an agent to name and vault it.

## Records

- **K1.** Record = type, ID, title, description, owner (user), creator (user or agent), JSON body, timestamps. No schema registry in core.
- **K2.** Records are never exposed to agents as a raw cap: there is no `records.create` or `records.query`. The store is reached only through plugin code via the plugin library, and each record type is declared and owned by a plugin, which exposes its own typed caps (`task.create`, `knowledge.search`). Types cannot appear without a plugin owning them, so types do not creep. Core offers CRUD, search by type and by text, and links to plugin code only.
- **K3.** Records link to records, many to many. Blobs are a record type whose bytes live on hub disk, content-addressed; the body holds hash, size and MIME type.
- **K4.** Nothing is auto-deleted. Unlinked blobs are shown as unlinked and an agent decides.
- **K5.** Everything above core stores in records: tasks, knowledge, agent definitions, huddle episodes, config.
- **K6.** Jobs, tokens, journal and heartbeats stay as real tables.
- **K7.** Single records table with indexed columns, JSON column for body, separate links table, full-text index.

## Identifiers

- **G1.** Every hub object has a GUID and a unique three-word moniker. Both are accepted anywhere an ID is: site, MCP, voice.
- **G2.** Builds carry a two-word moniker alongside semver.

## Config

- **CF1.** Each worker has a config record at the hub. Defaults per client type; only overrides are stored.
- **CF2.** Clients fetch config after connecting, apply it, and write user changes back. Hub is the source of truth.
- **CF3.** Config references secrets by `secret:`; the hub substitutes on delivery.

## Updates and artefacts

- **U1.** Three artefacts: protocol library per language that a core is written in (Rust and Python to start), core per platform, clients per host. Clients pin only the bootstrap contract; they carry no protocol library.
- **U2.** Every client is a bootstrap plus a core. The bootstrap holds the worker identity and key (W7), downloads over plain HTTPS, verifies, swaps, supervises, rolls back and declares permissions. It never speaks Telesthete. UI and platform services live in the swappable layer. The bootstrap is kept generic enough that it almost never changes; the contract between bootstrap and core is small and versioned.
- **U3.** Core update is start-new, hand over, exit-old. The previous core is kept. If the new core does not heartbeat within a window set by the hub, the bootstrap restarts the old one and reports the failure over HTTPS so the Workers page shows it.
- **U4.** Bootstraps self-update the same way, except Android, where the hub serves the new APK and the app prompts to install it. The Android APK declares every permission up front so this is rare.
- **U5.** ESP32 is bootstrap and core in one: dual-partition firmware with the same rollback behaviour. The dongle also carries a Linux core payload for offline install.
- **U6.** One release is one artefact set. The hub holds one set per channel: dev and stable. Workers report their version; the Workers page shows stragglers.
- **U7.** Rollout is a job. Nothing updates on its own by default.
- **U8.** Compatibility is carried by the protocol version and the bootstrap contract version, so a lagging worker still connects.
- **U9.** Builds are signed by the hub; a worker applies only what its hub signed.

## Android onboarding

- **A1.** The hub hosts the APK and stamps its own URL into the APK signing block on download. The app reads it on first run.
- **A2.** Flow: install, open, sign in with Google, app presents the login to the hub, hub matches an existing user or provisions one if open provisioning is enabled, hub issues the device a token scoped worker and client under that user, app fetches its config. No code entry.
- **A3.** Join approval is implicit: a logged-in user approving their own device is the approval.

## Plugins

- **PL1.** Plugins add, never replace: caps, pages, settings sections, events, record types, Vue components.
- **PL2.** No plugin hooks into routing, auth or the journal. If a higher layer ever needs one, it is a core change made on purpose.
- **PL3.** One plugin format: a folder with a manifest declaring what runs at the hub (Python), what runs on workers (per core language), and what it adds to the site. The hub installs it and pushes worker parts to the right worker forms. Hot-loaded.
- **PL4.** Install from a folder or a URL, by a user in Settings or by an agent through MCP. Registry is LATER; device drivers are expected to be the first canned plugins.
- **PL5.** A plugin may ship a skill that teaches an agent how to set it up and use it.
- **PL6.** Agents write plugins, guided by the skill. A shared helper library per language holds what every plugin repeats.
- **PL7.** Core's own caps (shell, files, delegate primitives, health) ship as plugins installed by default.

## Site

- **P1.** Vue 3 without a build step, served by the hub. Plugin UI and core UI are the same mechanism.
- **P2.** Nav: a Pinned strip, then segments. A segment appears only when something has installed a page into it. Core installs only Hub.
- **P3.** Actions live where the thing is shown. No separate management pages for a worker's shares, devices or plugins.
- **P4.** Core pages, eight: Workers (grouped by band, devices as a filter, pending approvals, band rename and merge on the group header, share and plugin opt-out on the row, expand for health charts, streams, caps, devices), Auth (Users, Tokens), Secrets, Jobs (list, chains as a tree, runs), Data (Journal, Records), Install (one-line command, APK, desktop installers, dongle provisioning), Settings (hub name, channel, retention, provisioning mode, Plugins).
- **P5.** Core has no chat pane. The agent chat pane (on every page, movable left or right, pop-out to its own window) arrives with the Agents plugin as an ordinary Vue component. Settings stay real pages.
- **P6.** A pin is a saved view: a page plus its active filter, tab or selection. Every page header has a pin control; pinning a single row pins a view filtered to that item. The Pinned strip is reorderable.
- **P7.** Workers page carries a stats strip (online, stale, pending, devices), the heartbeats graph, and list, grid and 3D views ported from 0.4. No versions card.
- **P8.** Streams attach inline as a terminal on the worker card; job steps open whatever is behind them (terminal, agent transcript, or call result).
- **P9.** Install page: the APK is a public link with a QR code; installers listed with OS icons, no macOS; the dongle is flashed from the browser over WebSerial with worker name, SSID and Wi-Fi password, with image download as the fallback for browsers without WebSerial.
- **P10.** The 0.4 logo and the three.js background scene are carried over.
- **P11.** Rookie is not part of core; it arrives with the Agents plugin. Carry forward to that plan: Rookie uses the full set of generated assets, including the recent enter and exit animations, not placeholders.

## Explicitly out this round

- Approvals, security gates, elevation leases, change classes
- Home Assistant. Not a target, not an integration, not a consideration. Core does not know it exists.
- Multi-user tenants and permissions beyond share cap lists
- Telesthete v2 extras (signatures, multi-band workers, negotiated extensions)
- macOS
- Multi-hub per worker; hub failover; whole-hub federation
- Plugin registry
- Presence detection
- Stream timeouts, blob garbage collection, any automatic deletion

## Open items

- Read the Telesthete repo and the 0.4 stream code before locking the stream frame design (intent is set; implementation is to be checked against it).
- Inventory of 0.4 caps, pages and jobs from the rook04 site, so the baseline comes from the system rather than recall.

## Proposed amendments, unsigned

These lines describe what core does today with no signed plan line behind it. Each line can be struck on its own; whatever Bake strikes is removed from the code in a later pull request, and what he leaves stands as part of this plan. Each entry gives the plan section or line it belongs to, the text as the plan would read, and the pull request that built it.

### 1. Bake's rulings, built, never written into the plan

- **1.1** (Auth and tokens, T4) While the hub has no user, the site shows a setup page that makes the first user. Built in PR 404. Bake, 13 Nov 06:36: "First user needs a setup wizard if no user is created."
- **1.2** (Auth and tokens, T4) The setup page asks for the password twice. Built in PR 412. Bake, 13 Nov 20:15: "Your judgements seems correct for everything."
- **1.3** (Secrets, X4) The vault key is a file the hub makes on the first start of a vault never keyed, outside the data directory, and reads on every start; a missing or wrong key file for a vault keyed before stops the start with "locked", and no new key is made. Built in PR 404. Bake, 13 Nov 07:08: "Key file on disc."
- **1.4** (Caps, calls and streams, C3 and S1) A call not answered within 45 seconds answers "running" with its ID and a stream; the call keeps running and its reply arrives on that stream. Built in PR 407. Bake, 13 Nov 07:11: "Take your recommendations" (ruling 6).
- **1.5** (Caps, calls and streams, S2) A terminal cap takes `cols` and `rows` when it opens, and the hub sends the optional `stream_resize` message when a client's terminal changes size. Built in PR 409. Bake, 13 Nov 07:11: "Take your recommendations" (ruling 8).
- **1.6** (Secrets, X4) Backups are encrypted and authenticated with a key derived from the vault key; the vault key is never in the backup. Built in PR 410. Bake, 13 Nov 07:11: "Take your recommendations" (ruling 7).
- **1.7** (Heartbeat, H5, replacing "raw for a day, hourly for a month") The hub keeps every raw heartbeat and an hourly roll-up of them, and deletes neither. Built in PR 408. Bake, 13 Nov 07:11: "Take your recommendations" (ruling 5: "build the hourly roll-up, delete nothing").
- **1.8** (Hubs and federation, F3) The hub's data directory and everything in it are readable by the hub's owner only: the folder 0700, files 0600, tightened at every start. Built in PR 412. Bake, 13 Nov 20:15: "Your judgements seems correct for everything."
- **1.9** (Topology) The hub listens on port 8700 by default. Built in PR 404. Bake, 13 Nov 20:15: "Your judgements seems correct for everything." (default port stays 8700).
- **1.10** (Explicitly out this round) A hub on Windows; the hub runs on POSIX systems only (it locks its data directory with `fcntl`). Built in PR 407. Bake, 13 Nov 20:15: "Your judgements seems correct for everything." (the hub on Windows is not in this round).
- **1.11** (Caps, calls and streams; docs/link-design.md D4) An encoded message is at most 16,000,000 bytes in either direction, or, hub to worker, the worker's `max_message` if smaller; the hub refuses a larger call or input with `message_too_large`, and a worker whose result would be larger answers `cap_failed`. Built in PRs 413 and 414. Bake signed the link design 13 Nov 20:15: "Your judgements seems correct for everything."
- **1.12** (Caps, calls and streams; docs/link-design.md section 4.3) One `stream_data` message carries at most 65,536 bytes of data; a worker splits larger output into several messages. Built in PRs 413 and 416. Signed with the link design, 13 Nov 20:15.
- **1.13** (Topology; docs/link-design.md section 5) The hub closes a `/band` connection that sends nothing for 60 seconds. Built in PR 417. Signed with the link design, 13 Nov 20:15.
- **1.14** (Topology; docs/link-design.md section 5) Each `/band` connection queues at most 256 frames to send; frames past that are dropped. Built in PR 417. Signed with the link design, 13 Nov 20:15.
- **1.15** (Registration; docs/link-design.md section 3.2) A worker whose attempt to reach the hub fails waits 0.5 seconds, doubling to 30 seconds, each wait varied by up to 20 % either way, and tries again, without end. Built in PR 416. Signed with the link design, 13 Nov 20:15.
- **1.16** (Caps, calls and streams, S2; docs/link-design.md D5) A worker heartbeats `open_streams`, the streams it opened and still has; the hub closes, as closed by the worker, every open stream that worker opened and did not list. Built in PR 415. Signed with the link design, 13 Nov 20:15.

### 2. Faults found in review and fixed without asking Bake

- **2.1** (Hubs and federation, F3) One hub serves a data directory: a second hub started on the same folder exits with "Another hub is serving" (`hub.lock`). Built in PR 407.
- **2.2** (Registration; docs/link-design.md section 3.2) The worker's wait between attempts resets only after a session whose link delivered a `heartbeat_reply`; a session lost before that counts as a failed attempt. Built in PR 416.
- **2.3** (Registration) The worker's knock and `/link` requests wait at most 10 seconds for the hub. Built in PR 416.
- **2.4** (Workers, W1) A message whose JSON is nested too deep is answered `bad_message`, not a server error. Built in PR 416.
- **2.5** (Topology; docs/link-design.md sections 2.5 and 3.3) A pending link session not used within 60 seconds of its `link_open` is removed. Built in PR 417.
- **2.6** (Hubs and federation, F1) When the hub stops, each `/band` connection gets at most 0.25 seconds to take the frames queued for it and is then cut; a `/band` request that arrives during the stop is answered 503. Built in PR 419.
- **2.7** (Caps, calls and streams, C3 and S2) When the hub stops, a waiting stream read answers with what it has, and a waiting call answers "running" with its stream, as at the end of their wait; a call cut by the stop is left running, and the next start ends it with `hub_restarted`. Built in PR 420.
- **2.8** (Layering) Telesthete's Channel keeps each message in two queues; Rook's hub and worker read both. Built in PRs 417 and 418.
- **2.9** (Hubs and federation, F3) A file in the data directory that another user owns is skipped at start with a line in the log, and the hub keeps starting. Built in PR 412.
- **2.10** (Hubs and federation, F3) The backup file is written readable by its owner only, whatever the umask. Built in PR 412.

### 3. Additions decided by the reviewer without Bake's signoff

Lines 3.1 to 3.18, except 3.9, are the rows of the reviewer's "Core creep" list not already above. Lines 3.9 and 3.19 to 3.28 are in neither that list nor the plan audit; they were found in the review log and the code, and were merged in review without Bake's signoff.

- **3.1** (Open items) A read-only audit of core against this plan, written 14 November 2026 (`PLAN-STATUS-2026-11-14.md`), changing no code. Brief 36, no pull request.
- **3.2** (docs/link-design.md) The signed link design carries a "Decided after signing" list for the places where the code differs from the signed text, in place of an amendment to this plan. Built in PR 418.
- **3.3** (Registration, R5) A worker key approved before link keys existed is refused, and the worker must be rekeyed. Built in PR 414.
- **3.4** (Hubs and federation, F3) A backup without `hub_link.key` is refused on restore. Built in PR 414.
- **3.5** (docs/link-design.md, build plan) The worker's `link_key` file and the knock's two new fields are built in L1, not in L3 as the design's build plan says. Built in PR 413.
- **3.6** (Site, P1) `POST /api/call`: the site calls any cap as the signed-in user through one generic route. Built in PR 406.
- **3.7** (Build rules) Seven old worktrees are kept, though stale worktrees are to be removed: c13-revision with its _deploy and _verify, c15-net, remote, library-folder-cache and tv-keep-pointer. No pull request.
- **3.8** (Auth and tokens, T4) The setup page asks for no setup code: whoever reaches a hub that has no user makes the first user. Built in PR 404.
- **3.9** (Plugins, PL3) The worker and site sections of the plugin manifest are as written in `docs/plugins.md`. Built in PRs 405 and 406.
- **3.10** (Plugins, PL3) Plugin worker parts reach a worker by their own protocol message, `plugins`. Built in PR 405.
- **3.11** (Plugins, PL3) The hub sends each worker the list of plugins it should have, and the worker drops the rest. Built in PR 405.
- **3.12** (Plugins, PL1) Plugin pages call caps by the same route core pages use. Built in PR 406.
- **3.13** (Site, P6) A pin names the plugin and the page. Built in PR 406.
- **3.14** (Site, P4) The `worker.plugins` hub cap: an agent can opt a worker out of plugins. Built in PR 405.
- **3.15** (Plugins, PL4) `POST /api/plugins/check` says what an install would replace, before the person confirms. Built in PR 402.
- **3.16** (Auth and tokens, T4) A signed-in user changes their own password, without giving the current one (`Auth.change_password`, `POST /api/password`). Built in PRs 388 and 400.
- **3.17** (Jobs and events, J7) The `job.list` and `job.unpause` hub caps. Built in PR 392.
- **3.18** (Workers, W7) A worker's name is the name it installs with; its three-word moniker is separate. Built in PR 385.
- **3.19** (Caps, calls and streams, C3) A call that was running when the hub stopped is ended at the next start with the error `hub_restarted`, and so is the last entry of its stream. Built in PR 407.
- **3.20** (Journal, J8) The `token.used` event is put on the event bus but not journaled. Built in PR 392.
- **3.21** (Records, K2) The site's Data page reads records and blob bytes from the store directly, not through plugin code. Built in PR 399.
- **3.22** (Journal, L4) Vault values shorter than 8 characters are not scrubbed. Built in PR 394.
- **3.23** (Workers, W3) Cap names are lowercase letters and digits only, in dot-separated segments with hyphens inside a segment. Built in PR 385.
- **3.24** (Heartbeat, H2) The heartbeat interval is 15 seconds unless the hub sets another for a worker. Built in PR 385.
- **3.25** (Auth and tokens, T2) Token defaults are fixed rows with no way to set them: agent tokens scope `client`, no expiry; join tokens scope `join`, 600 seconds, one use; webhook tokens scope `webhook`, no expiry; site sessions no expiry. Built in PR 386.
- **3.26** (Caps, calls and streams, C2) A stream read waits at most 40 seconds for more. Built in PR 396.
- **3.27** (Site, P8) A terminal reads at most 1,000 stream entries per request and reads on until it has them all. Built in PR 401.
- **3.28** (Site, P4) The Jobs page shows the newest 50 chain runs; older ones stay in the journal. Built in PR 400.
