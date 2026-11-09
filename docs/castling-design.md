# Castling: what goes in, and why

Bake's design doc, as agreed with him on 2026-11-09 (source: https://claude.ai/artifact/7QpPnVYQmg9mFbLnrVwvbA). Rows marked *proposed* are suggestions he has not confirmed; everything else is decided or already exists. This file is the standard all code is reviewed against.

Rook is the layer agents use to reach real machines: a small hub and band that everything else plugs into. This revision keeps what proved useful over the last year and removes what was built for scenarios that never came.

## North star

Utility over complexity. In Bake's words:

- "The only real barrier to future-tech AI agents is just access and capability." Rook is "internet for agents to real hardware, giving agents agency to act."
- Reuse code wherever we can. Clean separation of concerns. Lean YAGNI: "we typically reinvent shit a lot or set up for scenarios that are unlikely to be needed."
- Bake no longer reads the code, so the code has to be shaped by the vision and small enough to trust.
- Usable first; uptime is the focus; no gates until the hardening pass ("Remove all restrictions and gates. Stop adding complexity to the process. We will do a hardening pass once everything is stable"); the house agent guides ("I want it to guide the setup entirely").

The test for anything going in: does it give an agent access or capability it lacks today, using a mechanism Rook already has?

## The shape

Rook is a substrate with three levels on top; each level only depends on the ones below it. The hub must run with the upper levels deleted.

- Telesthete: the transport (encrypted peer-to-peer links and streams). Used by Rook, Bishop and anything else.
- MINIMAL = hub, band and core: the least an agent needs to connect over MCP and use Rook as a mesh of capabilities.
- ASSISTANT = agents: Rook-native agents that live on the band: the house agent (Rookie), huddles.
- TEAM = workbench: tasks, projects, knowledge and tracking; agent-first, with Bake as the executive audience.
- Modules: things that plug into the substrate from outside: Bishop (per-window remote control), firmware devices, the phone app.

The installer's first question becomes which level you want. A level is only which plugins are switched on; no code knows what level it is running at. Agents and workbench are Bake's example of how to use the substrate, not part of it.

## Core (the minimal level)

Core is the hub, the band, and the smallest set of facilities every level above reuses.

- Hub: one process (MCP door, site, enrollment). Running on bakenetus. Keep.
- Band and relay. Keep.
- Caps and calls: worker + cap + args + why, journaled. Keep.
- Streams: terminals, processes, sockets, chat rooms, sessions as one thing. Keep; Bishop rides these.
- Journal: every call, who and why. Keep.
- Vault: secrets by name. Band-wide only today. Keep; build per-person secrets so each family member has their own.
- Events: workers tell the hub something happened. NOT WIRED. Build: jobs, attention and incoming texts all wait on it.
- Jobs: a time or an event fires ONE call. Nothing more. Castling's jobs have more in them: cut down. Logic lives in a script the call runs. No workflow engine, no logic trees as data.
- Records: a plugin declares a record type and gets save, get, list, search and history. Built as a larger framework: keep the small version, cut the rest.
- Minimal worker profile: join, announce caps, answer calls, heartbeat. Buried inside wire spec v2 today: write it down. If firmware cannot implement it, it is not core.
- Wire spec v2 extras (signatures, workers on several bands, elevation, negotiated extensions): cut or make optional.

Core site (agreed list): tokens, agent installer, workers, users and admin, bands, jobs, journal, vault, settings. The bare shell and sign-in are core; every other page comes from a plugin.

Leaks to fix (places core knows about the levels above it):
1. The chat dock, Rookie and setup mode live in the hub's web shell. They move to the agents package and plug in.
2. Trust levels hang off workbench projects, and journal pruning refers to them. The trust map moves to the agents package (Bake: "it matters most to the agents"); core and workbench stop knowing about it.
3. The hub config names an agent engine directly.

## Workers

A worker is a thin set of primitives in Rust; the knowledge of what to do with them lives on the hub. This is the janitor pattern applied to everything: workers reach things, the hub knows what to say.

Primitives: run a command, read and write files, open a stream (terminal, process, network socket, serial port), report status, update itself. No plugins and no interpreter on the worker.

Forms, all speaking the same minimal profile:
- Service binary: servers, containers, headless boxes. One file to install.
- Desktop app: computers a person sits at. The existing Tauri app (desktop/) with the Rust core compiled in: tray icon, on/off switch, a view of what it is doing. Today it launches a separate Python worker as a child and still carries 0.4 and 0.5 code paths.
- Single-file Python script: machines where nothing can be installed. Standard library only, a few hundred lines.
- Firmware: ESP32 and other microcontrollers. Exists as the dongle firmware; the Rook remote is next.
- Phone app: Android, already its own worker.

Native modules: anything that must live on the machine brings its own program, connects to the local worker and announces its caps, speaking the same minimal profile a worker speaks to the hub. Bishop is the first (a module of the desktop app). No separate plugin interface for modules; a module can crash or update without taking the worker down.

Cost: a worker rewrite, and about 40 worker-side plugins moving to hub scripts. Browser automation stays local as a native module. The Rust telesthete transport already exists (github.com/Bake-Ware/telesthete, rust/telesthete crate: bands, channels, streams, crypto, transport), so the worker builds on it directly.

Enrollment: one tap on the Workers page when a person runs the installer (the machine shows as "wants to join"). An agent already connected to the hub (e.g. Claude Code on that machine) installs and joins a worker with no tap. The device-code step goes away.

## Plugins

Everything above core is a plugin, and there is one way to write one: a manifest plus Python scripts that run on the hub.

- Manifest: name, caps, settings, record types, pages. Firmware and native modules announce the same manifest without running a script.
- Scripts: Python, on the hub. A script can call any worker, check results, branch and loop; every step lands in the journal. This is also where a job's logic goes.
- One skill teaches agents to write plugins, since agents will write most of them.
- Cut: the second plugin format (config "actions"), and anything in the records and contracts machinery that only one plugin uses.

Python stays for the hub because agents write it best and it is where plugins live. It leaves the workers.

## Devices and delegates

A passive device (anything that cannot host its own worker but can respond over some sort of network call) is a first-class Rook target: you call it by name, and a worker that can reach it does the talking. Home Assistant is one way to reach devices: supported later as ONE OPTIONAL plugin, never required, never a dependency, never the default. Bake: "The idea is to support HA, but not need it instead leveraging the janitor/delegate pattern for passive devices so they are like first class rook targets basically."

- Appliance (a device with a name, type and settings; call appliance=<name>): built for TV (webOS, Roku), KVM, CEC, Android over adb. Keep; this is the pattern.
- Delegate (the worker that reaches the device; janitor for most of the house): built. Keep.
- Discovery (SSDP, mDNS, adb) and guided pairing: keep.
- No device list: an agent writes the hub script when a device shows up.
- Cameras: deferred, not part of this revision.

## Agents (the assistant level)

- House agent and Rookie (avatar, chat beside every page): on PR #383. Keep; move out of core's web shell into this package.
- Guided first run in two panes: on PR #383. Keep. A new hub is open only until it is claimed: creating the first user claims it; after that the site asks for sign-in even while guided setup continues (NOT yet built on #383).
- Engines: OpenCode (free) and Claude Code work. Add Codex, Gemini, API key, local model.
- Model providers: several model sources at once, with a page to add them and pick which one each agent uses. 0.4 had this; castling cut it to one endpoint with no page. Bring back: each provider a plugin, one page.
- Voice providers: speech engines (kaiju Chatterbox and Whisper, ElevenLabs) as plugins; build the Voice page; move enrollment out of the old service.
- Memory: ONE store: a notepad by day, a dream cycle that prunes and corrects, sharpened by real use. Notepad built; dream cycle not.
- Long context: one continuing conversation. Not built.
- Attention: built, starved without core events.
- Hygiene: a scheduled sweep the house agent runs, not a system of findings.
- Reaching Bake: voice, phone notification, Telegram, Discord. Keep.
- Huddles: plain threads; votes stay dropped.
- Roles: bring over the 0.4 role presets (rook/hub/plugins/roles/presets: analyst, architect, cad, comms, community, content, editor, fab, firmware, household, launch, lead, ml, pcb, pm, product, qa, release, research, security, skeptic, social, sre, style, writer). Castling has 8 personas.
- Trust map (sacred machines and project levels): lives here.

## Workbench (the team level)

Agent-first; pages show Bake the state. Every item is a record type on core's records facility.

- Tasks and projects: keep. Knowledge wiki: keep.
- Handoffs: the task whiteboard IS the persistent handoff; no separate handoff records.
- Landing work: task sign-off then merge. No votes.
- Change records: records only.
- Sessions and terminals page: build (watching agents work is daily use).
- Own Claude/Codex session history import: *proposed*, after cutover.
- Claims checks, hygiene findings, routines, policy caps, inbox: stay dropped.
- A new record type (a storyboard, say) should need only a manifest and a page.

## Phone, voice and glasses

Phone app on 0.5 with all phone caps: keep. Incoming texts and notifications to the agent: blocked on core events. Desktop app sign-in needs a pasted token: fix. Watch: later. Glasses: design with the voice loop. Tablets and TVs need a person present.

## Security

Security is switched off on the hub and stays off until the hardening pass. DO NOT add any security, permission check, owner-only rule, validation, limit, expiry, binding restriction, approval or review gate that this document does not ask for. If you think one is needed, list it under "For the hardening pass" and do not build it.

Keep (Bake's): trust map; the four hard stops (change on a sacred machine, erase for good, reveal a secret, send as Bake) as the only holds, at hardening; the journal; the vault; sign-off before work lands; sign-in, sessions and connector tokens, minimal; the recovery key; signed updates.
Cut: one-time code approval for connectors; device-code enrollment; the ML classifier; approval cards and the approvals page ("no approvals at all, no approvals page").
Cut down: the eight risk/effect flags to what the hard stops read; the gate to the hard stops.
Park (leave dormant or remove, never extend): layered rights, elevation and leases, canary and taint tracking, roles/permissions/policy.

## Order of work

1. Cut over to castling and turn 0.4 off. BAKE'S GO ONLY. Not part of the team's work.
2. Delete the 0.4 code, the migration tooling and their tests from the branch (migration tooling only after the cutover has happened).
3. Fix the three leaks so core runs alone; move the house agent into the agents package.
4. Wire events from workers to the hub; cut jobs down to trigger-plus-call.
5. Write the minimal worker profile and trim wire spec v2 to it.
6. One plugin format: scripts on the hub, records cut to size, the plugin-writing skill.
7. Rust worker against the profile, starting inside the desktop app; then the service binary and the single-file script.
8. Move worker plugins to the hub, one at a time.
9. Code review of what is left, then the hardening pass.

The first-run work on PR #383 and the daily-driver agent (voice, memory, long context) run alongside from step 3.

## Cut list

All 0.4 code still in the castling branch (~118k lines); the migration/switchover tooling (castling/) once cutover is done; tests for anything cut; Mac support (never asked for); merge votes, huddle tables and pins, claims and hygiene findings, routines, policy caps, the notification inbox, worker-side memory and persona.

## Still unverified

Which security mechanisms trace to a decision of Bake's is the author's read, not a verified trace. The 151 caps dropped in the port have not been gone through one by one.
