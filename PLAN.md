# Rook Core Plan

Decided with Bake, 12 November 2026. LOCKED 12 November 2026. Amended 12 November 2026 by Bake: Telesthete is updated additively to accommodate this design, the Rook hub implements the Telesthete relay as its base, and user login is by password for now. Core only. Agents and Workbench are separate plans built on top once core is tested. Claude (the voice session that wrote this) reviews every PR against this document; coding agents build, they do not decide.

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

- Hub and spoke only. Every worker holds one connection to its hub; calls route through the hub. The Rook hub implements the Telesthete relay as its base.
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
