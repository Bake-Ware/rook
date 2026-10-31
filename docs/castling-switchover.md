# Switching production to Rook 0.5

The plan for moving the live band from 0.4 to Castling (0.5), and for making
`castling` the new `master`. It is based on what the `castling` branch builds
as of 2026-10-31 (a387b537 plus the open wave 6 PRs). This document only
reports. Nothing in it has been run against the live hub, a live worker, the
vault or `master`.

Scripts in `castling/switchover/`:

- `dryrun.py` runs the migration's dry run on a 0.4 hub snapshot that is
  already on this machine. It writes the full report privately and prints a
  summary that has counts only.
- `readiness.py` lists which code gates below this checkout meets.
  `--prs` also lists the PRs still open into `castling`.
- `migrate/` (`python -m castling.switchover.migrate`, how-to in
  `migrate.md`) and `import_04.py`: the one-off 0.4 move. castling-design
  (Bake, Oct 30): "Migration from 0.4 is not a product feature ... a one-off
  move ... an agent imports and reshapes them into 0.5". So it is repo
  tooling here, not shipped in the `rook` package, run once at step 5 and
  deleted at step 6. It gets no more features: whatever the move leaves
  rough, an agent reshapes on 0.5.

## Where things stand

| Piece | Live (0.4) | On `castling` (0.5) | State |
|---|---|---|---|
| Hub | rook.bakeforge.com on bakenetcanada (Oracle VM), releases through the CR runner | `python -m rook.core2.hub`: HTTP door, dashboard, MCP door, enrollment | runs as the cachyrig sidecar (#286), plain HTTP, localhost relay |
| Band relay | telesthete-hub behind mcp.bakeforge.com:443 | the same Telesthete wire (`TelestheteLink` reuses `rook.worker`'s transport) | a 0.5 band can share the live relay under its own band id |
| Workers | ~38 machines with OTA updates | `python -m rook.core2.worker`, enroll by device code | cachyrig only; core updates by signed release and staged rollout (`update.*`, docs/castling-updates.md) |
| Plugins | 63 | 50 plugin folders in `rook/plugins/`, the rest binned or core (castling/coverage.md) | see "Not ported" below |
| Vault | 65 secrets | `<data>/vault.db` sealed under the root and recovery keys; `{{secret:NAME}}` and hub plugins' secret settings read it; `secret.*` caps | worker-side secret settings have no vault path yet |
| Speech / voice | kaiju Chatterbox + Whisper, voice front | `speech` + `speech-engine` + `speech-elevenlabs` | PR #287 open |
| Phone app | APK 0.4.23 stable, beta channel | 0.4/0.5 switch, 0.5 runs in Chaquopy | PR #298 open, emulator only |
| Desktop app | 0.2 | 0.4/0.5 switch, token sign-in, worker05 | merged (#290); update check on 0.5 (`/updates/desktop.json`) |
| Watch | 0.2.1 through the phone | wave 6 lane in progress | not on 0.5 |
| Security | trust map, hard stops | `security` (canary + taint) and `security-ml` (shadow) | merged (#284) |
| Agents | house agent, huddles, CR runner, sessions | `agents` + `agents-claude-code`, 8 personas | merged (#280, #288); huddles are plain streams; work lands by sign-off + `github.merge` (R14) |
| Soak | n/a | tests/soak on the sidecar | PR #297 open, numbers pending |
| Review | n/a | hardening review: fix PRs #292–#296, #299–#303 | open; `castling/review.md` not in yet |

`master` (d15e79bf) has no commits that `castling` lacks. Outside the new
trees, `castling` changes only the desktop app (behind its switch),
`pyproject.toml` and the sidecar scripts. Promoting it is a fast-forward and
leaves the 0.4 code paths as they are.

## Readiness

### Code gates

`python castling/switchover/readiness.py` checks these against the checkout.
Each step in "Order of moves" names the gates it needs. A gate that blocks
the final cutover doesn't hold up the earlier side-by-side steps. Only
R1–R5, R10 and R14 hold the cutover (D5: "cutover held for gates R1-R5, R10
and R14; the rest can follow"). The other gates, the soak, the review
findings and the on-device checks are follow-ups: they hold no step and no
waiting period.

| Gate | What must be true | Why it matters | Blocks step |
|---|---|---|---|
| R1 | The hub has a vault: `{{secret:}}` and secret settings read `<data>/vault.db` | The migration re-seals all 65 secrets into a file that nothing reads yet. Jobs, bots and plugins that need a secret can't run without it. | 5 |
| R2 | Worker events reach the hub (`TODO(castling:events)`) | The house agent's attention job on the hub (castling-attention: "an aggregation job on the hub run by the house agent"), `sms.received`, phone notifications and claudecode rules all depend on events. | 4 |
| R3 | Streams have a hub index (N28) | `stream.list`/`stream.search` over every stream (castling-caps rule 7: sessions, terminals, rooms and history readers become `stream.*`), and owners reading each other's streams. | 5 |
| R4 | Change records are records: the `change` plugin declares a `change` record and has no apply/revert runner | castling-caps C2 (Bake, Oct 30): "records only, with apply, test and rollback as a job the record links"; castling-changes: a lasting change gets a record (what, why, risk, testing, rollback, steps), "the change then proceeds", and ceremony scales with the project's trust level. Core's plugin pushes and worker updates don't depend on the plugin (drift #2). | 5 |
| R5 | Off-site hub backup, sealed with the recovery key | 0.5's only backup today is "stop the hub and copy the folder". That isn't enough once the band depends on this hub. | 5 |
| R6 | A 0.5 release format: `worker.update`, `update.*` (N27) | Without it, every core fix means reinstalling by hand on every machine. Plugins can already be pushed. | 3 (wave B onwards) |
| R7 | Speech on the hub | The voice assistant. | 4 |
| R8 | The phone app runs 0.5 | The phone is the main surface. | 4 |
| R9 | The desktop app runs 0.5 | Laptops add themselves through it. Passes today. | 3 |
| R10 | The desktop app updates on 0.5 | Without it, a desktop fix means reinstalling by hand. | 5 |
| R11 | Soak results written up (`docs/castling-soak.md`) | see "Soak bars" | none (follow-up, D5) |
| R12 | The hardening review's report (`castling/review.md`) | see "Review findings" | none (follow-up, D5) |
| R13 | The MCP door speaks OAuth | Done (`rook/core2/hub/doors/mcp_oauth.py`, the 0.4 shim's shape): with `oauth: <public address>` on the `mcp` door, claude.ai's connector uses an agent token as its client secret and that token as its access token. Needs the door reachable over public HTTPS. | 2 (for claude.ai) |
| R14 | Agents land work on 0.5 without 0.4: a task's sign-off (by its project's trust level), then `github.merge` at the signed-off head | Agents land PRs today through the 0.4 huddle's `huddle.vote` and the chair's pin. That flow ends when the 0.4 hub stops. On 0.5 sign-off replaces merge votes (castling-workbench). | 5 |

These are also open and should be done or explicitly dropped before the
cutover. They come from castling/coverage.md and the lanes' notes:

- Not built yet, named as Bake's decisions decide them (castling-caps
  and its per-cap table; nothing here is a 0.4 piece to port):
  - ~~the house agent's **attention job**~~ built (drift #30): the
    `attention` plugin's ready-made job runs the house agent's attention
    skill every 15 minutes, which collects through caps and curates into
    `attention.list` (castling-attention: "periodically collect everything
    trying to get the user's attention ... via caps"); the 0.5 phone does no
    sorting. The 0.4 reminder rule goes: "When to remind is the house
    agent's call";
  - core **`page.show`** (castling-caps rule 10: "show.present becomes core
    page.show"; drift #8);
  - **`trust.check`**, the one "will this run, hold or be refused, and why"
    answer (castling-caps rule 10);
  - core **`appliance.*`** on the YAML engine (castling-caps rule 7: "devkit
    and serves become core appliance.*"; N26, drift #21);
  - history readers as importers into **`stream.*`** (castling-caps rule 7),
    and the shim as `harness.shim.install/remove/status`;
  - speech's conversation modes, streaming TTS and speaker enrollment caps
    (#287 notes; no Bake decision names them yet).
- Windows: the 0.5 e2e suite is POSIX only, ConPTY isn't ported (N19), and
  the desktop app's 0.5 worker has never run on Windows.
- No in-app QR scanner on the phone. Android device names with spaces fall
  back to the name `worker` (`rook.core2.enroll.device.default_name`).
- Guests get no `agent.backend` and no `speech.*` (Bake decides; #280, #287).

### Soak bars (follow-up; proposed)

Soak PR #297 is still running its 45-minute pass. It has already found two
hub problems: sessions piled up, and the journal chain check stalled the
doors. Both are fixed on that branch. Proposed bars for the write-up. They
don't hold step 1 or the cutover (D5):

- 24 hours on the sidecar with real use after the scripted soak: no lost or
  duplicated calls, and the journal chains still verify at the end.
- Memory, file descriptors and threads stay within ±10% after the first hour.
- After a hub, relay or worker restart, calls work again within 60 s with no
  manual step.
- Door latency at p95 for a read cap on a LAN worker: set from #297's numbers
  once they land.

### Review findings (follow-up)

Every finding in `castling/review.md` is either merged or accepted in
writing by Bake. The fix PRs open on 2026-10-31 are #292 (plugin rollback
re-verifies), #293 (linear glob), #294 (agent-made pages), #295 (secret
scope), #296 (enrollment is for people), #299 (elevation layers), #300
(hold release re-checks), #301 (argv/URL injection), #302 (file robustness)
and #303 (HTTP door bodies and timeouts). They land as they're ready and
don't hold a step (D5).

### On-device checks (follow-ups a person ticks; they hold nothing, D5)

- The phone's 0.5 mode on bakephone: sign-in, worker online, a call each to
  battery, sms (refused without a grant), notification, voice.
- The watch through the phone on 0.5.
- The desktop app's "Add this computer" on one Linux and one Windows machine.

## The data move, rehearsed

The migration tool (`castling/switchover/migrate`, dry run only) was run against the
newest 0.4 hub copy on cachyrig. This copy was not pulled from the live hub:

- `~/rook-backups/hub-offload/consolewin-live-20261027.tar` (sha256
  `8e3a2983…`), taken 2026-10-27 18:52Z at release 1241.mopey.blimp, with
  `sqlite3 .backup` databases, `trust.json` and `vault.key`.
- Command: `python castling/switchover/dryrun.py --snapshot <that tar> --out ~/rook-backups/castling-switchover/dryrun-20261031`
- The full report, which names secrets, machines and people (never a value),
  is in that folder (0700/0600) on cachyrig. The unpacked copy was deleted.

Summary (counts only):

- Stores found: 9 of 9 (access, accounts, trust, vault, vault_key, memory,
  journal, knowledge, sessions).
- **People:** 1 owner (bake), with 0 member grants. `enrollment.db` has 2
  accounts, but only one is a member, so the second account doesn't move.
  If that is the "one other user" from castling-design, Bake adds them on
  0.5 by hand.
- **Trust:** 0 sacred machines and 1 project level (pianobar-super →
  ship-it). The machine-level ship-it entries (kaiju, the family devices,
  bakephone) need nothing, because ship-it is 0.5's default. Two exceptions,
  both expired, and four accepted proposals have no 0.5 equivalent. Neither
  matters.
- **Workbench:** 318 knowledge pages, 230 tasks, 13 projects, 7 concepts,
  409 comments, 65 boards.
- **Vault:** 65 secrets would be re-sealed. About 16 of them belong to 0.4
  itself and go stale at cutover: 6 band keys, 8 hub, dashboard, MCP and OTA
  credentials (including the vault key), and 2 chat-bot settings that 0.5
  takes as plugin settings. 14 are Hermes runtime copies. Until R1 lands,
  nothing on 0.5 reads them.
- **House agent memory:** 12 memories and 5 notepad items (unapproved
  procedures). 12 superseded memories stay behind.
- **Journal:** 20,117 calls, 2026-10-23 to 2026-10-27, archived read-only.
  The copy's journal starts on 2026-10-23, so older rows were pruned or
  rotated out on 0.4. Older hub snapshots hold them if they're ever wanted.
- **Machines:** 38 to re-enroll. 4 need a new name, because 0.5 refuses
  spaces and apostrophes. About 14 of the 38 are service containers whose
  calls are almost all the same periodic sweep (~186 calls each in four
  days). Bake decides which of them still need a worker at all (D6).
- **For a person:** 16 API keys to mint again as 0.5 tokens, 1 Telegram
  link, and 3 trust notes (above).

What the migration doesn't move, counted in the same copy:

| 0.4 store | Rows | On 0.5 |
|---|---|---|
| jobs | 12 jobs, 11 triggers, 367 runs | Recreate the wanted jobs in the 0.5 jobs plugin (#283). Run history stays in the snapshot. |
| hub settings | 56 settings | Re-enter the ones that matter (voice engine, front door, timezone) as 0.5 settings |
| chat + huddles | 37 rooms, 1,568 messages, 25 huddles | Nothing (0.5 huddles start fresh). Readable in the snapshot. |
| console rooms | 546 rooms, 238k lines | Nothing (streams start fresh) |
| roles, guidance, approvals, work sessions | 25 roles, 1 guidance override, 10 holds, 1,515 work sessions | Nothing. Personas replace roles. |

The dry run turned up one bug, fixed in this PR. The re-enroll checklist
printed names that 0.5 enrollment refuses, in commands that broke on the
apostrophe. Each machine now gets a valid 0.5 name, shell-quoted, and the
sacred list uses the same names (`castling/switchover/migrate/convert.py`
`worker_name`, tests in `tests/test_castling_migrate.py`).

### The copy for the real move

The 2026-10-27 copy is fine for rehearsal but stale for the move, since 0.4
has kept running. Step 5 needs a fresh copy, made on the live
hub by Bake, or by the house agent through a change request on 0.4, using
the commands in `castling/switchover/migrate.md` §1: an `sqlite3 .backup` of every
`*.db`, plus `trust.json` and `vault.key`, into a 0700 folder. It is then
moved to the new hub and deleted afterwards. The existing hub-offload tars
are mode 0644. Only the home folder's permissions keep them private, and
they hold `vault.key` beside `vault.db`, so `chmod 600` them.

## Order of moves

The rules for every step:

- 0.4 and 0.5 can't talk to each other, but a machine can run both. The
  sidecar proves this on cachyrig: separate code, venv, data, units and band.
- Nothing of 0.4 stops until its 0.5 replacement has carried real use for a
  while.
- Rolling back is always "stop the 0.5 piece", because 0.4 was never stopped.
- **Hub first:** a worker can only enroll with a hub that exists.

### Step 0: land the code

Merge the open castling PRs once they're green: review fixes, soak (#297),
speech (#287), phone app (#298), watch, and this one. Then run
`readiness.py --prs`.

`castling` → `master` (D1: "`castling` becomes `master` at the end of
switchover step 2"): fast-forward `master` to `castling` once step 2 is
done, with no waiting period after it. Push a `release/0.4` branch at
d15e79bf first, so 0.4 hotfixes and the 0.4 APK and bundle builds still have
a base. Until the switch, the phone and desktop
apps default to 0.4, so a 0.4 release built from the new master behaves as
before. Keep the 0.4 code in the tree until step 6. The 0.5 transport still
imports `rook.worker` (`TODO(castling:transport)`).

Rollback: reset `master` to d15e79bf (nothing has built on it yet), or keep
deploying 0.4 from `release/0.4`.

### Step 1: a production 0.5 hub, side by side

Needs the 0.5 hub code on `castling`. The soak, the review fixes and R11/R12
follow (D5).

- Where (D2). Recommended: bakenetcanada, next to 0.4, as its own system
  user and unit (`rook05-hub`), with data in `/var/lib/rook-castling`. It
  gets a new band on the same telesthete relay (its own band id, so neither
  side sees the other), and new hostnames through the existing cloudflared
  tunnel, e.g. `rook05.bakeforge.com` for the door and dashboard. Cloudflare
  provides TLS, and the door gets `secure_cookie: true`. The VM ran at
  95–98% disk in late October, so check the headroom first. If it is short,
  use bakenetus.
- `init` with owner `bake`. Put the recovery key somewhere off the VM.
- Backups (R5): set `settings.backup.target` and install the backup plugin's
  nightly job. Until a 0.5 worker off the VM is enrolled, use an rclone
  remote (`rclone:<remote>:rook05`); from step 3, `worker:cachyrig` with
  `backup-store` installed there. Run `backup.run` once and check
  `backup.verify` before migrating anything in.
- No migration into this hub yet: the move is one-off and happens once, at
  step 5. Rehearse it with `dryrun.py` (or `--apply` into a throwaway hub)
  as often as wanted. Until step 5, 0.4 stays the record for knowledge,
  tasks and memory.
- Load the library: agents, agents-claude-code or llm-cli, workbench, notify,
  security, security-ml, speech once it has merged. Leave telegram and
  discord off (see step 5).

Rollback: `systemctl stop rook05-hub`, drop the hostname route. Nothing on 0.4
changed.

### Step 2: agents and MCP clients, side by side

Needs step 1; R13 for claude.ai.

- Each 0.4 API key (16) gets its own 0.5 token: `... hub token --person bake
  --agent <name>`, with an `agents:` entry naming its grants. Every client
  needs a new token anyway, so a new URL costs nothing. Clients add the 0.5
  door as a second MCP server, named `rook05` as on the sidecar, next to
  `rook`. Bake's own Claude Code sessions go first, then the CC mod hosts,
  opencode, Hermes, the pianobar DJ, openwebui and bakedash.
- claude.ai web and mobile (R13): set `oauth:` on the `mcp` door (its
  public address), mint a `--agent claude-web` token, and add a custom
  connector with the door's `/mcp` address and the token as client secret.
  Rollback: remove the connector and take the token out of `tokens.json`.
- Agents do real reads on 0.5 and keep their writes on 0.4 until step 5. That
  way 0.4 stays the record and nothing splits.

Rollback: remove the `rook05` server from a client and revoke its token.

### Step 3: Linux workers, wave A

Needs step 1; R9.

Each machine gets the 0.5 worker beside its 0.4 worker, with its own venv,
config, data folder and user unit (`rook05-worker`), like the sidecar. It
enrolls with the device code that the owner approves on the dashboard.

Order: cachyrig first. Its sidecar worker re-enrolls to the production hub,
and the sidecar hub becomes staging (D7). Then janitor, soundwave and sojourn.
Then whichever service containers D6 keeps.

The sacred list is empty on 0.4. If D3 makes any machine sacred on 0.5, add
it to `sacred:` before that machine enrolls.

Wave B (everything after this) needed R6, now on `castling`: a core fix is
`update.rollout` (canaries first), and each worker
rolls itself back if the new core doesn't come up (docs/castling-updates.md).
Workers need a service manager that restarts them (the sidecar's unit does).
Plugin fixes are pushed as before.

Rollback per machine: `systemctl --user disable --now rook05-worker`. The
0.4 worker never stopped. Never restart cachyrig's 0.4 worker: it kills
every agent on the machine.

### Step 4: kaiju, Windows, phones, watch (wave B)

Each machine moves when what it runs is there: R2 for events, R6 for
updates, R7 for kaiju's speech, R8 for the phone. The on-device checks follow
(D5).

- **kaiju** (pinned on 0.4): run the `speech-engine` voice worker (#287)
  beside the running 0.4 voice service, on its own ports. Switch the
  assistant's voice only after the phone has passed on 0.5 speech. Rollback:
  stop the 0.5 voice worker.
- **Windows** (DESKTOP-M4VJVUR, the FA laptops): enroll through the desktop
  app's "Add this computer". Owner-only. The work laptop keeps the rules it
  has today (no CC mod).
- **Phone APK channels** (D4). The 0.4/0.5 switch build goes to the beta
  channel first. The next phone version code is ≥ 48, and the APK updater
  can't downgrade, so every build counts up. Bake flips bakephone to
  "0.5 preview". The phone runs one worker at a time, so this is the one
  place with no side by side. Rollback is flipping back to 0.4, and the 0.4
  enrollment is kept (checked on the emulator in #298). The stable channel
  gets the same build, still defaulting to 0.4. Family tablets and phones
  flip last, after step 5, with Bake there in person. The APKs keep updating
  from the 0.4 feed until R6 gives 0.5 one. After that, the app's 0.5 mode
  reads the 0.5 feed.
- **Watch**: follows the phone.

### Step 5: cutover

Needs R1–R5, R10 and R14, and nothing else (D5: "cutover held for gates
R1-R5, R10 and R14; the rest can follow"). Accepting D5 was Bake's go.

1. Announce a quiet window. Pause 0.4 jobs and stop agent writes on 0.4.
2. Take a fresh 0.4 copy, then the one move: `migrate` dry run, then
   `--apply` with the 0.5 hub stopped. Whatever the move leaves rough, an
   agent reshapes on 0.5 (castling-design); the tool gets no more features.
3. Move the chat bots. A Telegram or Discord bot token can only be served by
   one hub at a time (Telegram's getUpdates conflicts, and Discord would
   answer twice). Turn telegram and discord off on 0.4, then on in 0.5. Bake
   relinks (`/link`).
4. Agents switch writes to `rook05`. Remove the `rook` MCP server from each
   client and revoke its 0.4 key.
5. Recreate the 0.4 jobs that are still wanted, and re-enter settings.
6. Stop 0.4 workers machine by machine: `disable --now`, no uninstall.
   cachyrig's goes last, at a time Bake picks when no agent sessions run
   under it.
7. Stop the 0.4 hub last: disable its units, keep its data and a final
   snapshot for 30 days, and keep the hostname pointing at it read-only or
   at nothing.

Rollback (during the 30 days): start the 0.4 units again and move the bots
and agents back. Any work done on 0.5 meanwhile stays on 0.5, because there
is no 0.5 → 0.4 path. After 30 days, rollback means restoring the snapshot.

### Step 6: after

- Tag `v0.5.0` from `master`.
- Once the transport modules have moved under `rook/core2/transport`, remove
  the 0.4 code (`rook/worker`, `rook/band_mcp`, `server/`, the 0.4 halves of
  the apps and their switches) in one PR.
- Retire the 0.4-only secrets, after reading the private report.
- Rotate the credentials whose vault notes say they were reused or passed
  through chats. That is Bake's call.
- Delete the migration copies and `castling/switchover/migrate`,
  `import_04.py` and `migrate.md` with them. Optionally point `rook.bakeforge.com` at the
  0.5 door.

## Rollback summary

| Step | Undo | Cost |
|---|---|---|
| 0 | reset `master` to d15e79bf; 0.4 builds from `release/0.4` | none |
| 1 | stop `rook05-hub`, drop its route | the 0.5 hub's own data |
| 2 | remove `rook05` from a client, revoke its token | none |
| 3 | `disable --now rook05-worker` on that machine | none |
| 4 | phone: flip to 0.4; kaiju: stop the 0.5 voice worker | none |
| 5 | start 0.4 units, move bots and agents back (30 days) | work done on 0.5 stays there |
| 6 | restore the 0.4 snapshot | days of 0.5 history |

## What Bake decides

| # | Decision | Recommendation |
|---|---|---|
| D1 | When `castling` becomes `master` | At the end of step 2, with `release/0.4` at d15e79bf for 0.4 hotfixes |
| D2 | Where the 0.5 hub runs, and its hostnames | bakenetcanada next to 0.4 if disk allows (else bakenetus); new `rook05.` names, no DNS swap |
| D3 | Sacred machines on 0.5 (0.4 has none today) | Decide before step 3, since the list names machines |
| D4 | APK channel order and when family devices flip | beta (bakephone) → stable with 0.4 default → family devices after cutover, in person |
| D5 | Go for cutover, plus the soak bars, the review acceptances and the not-ported list (attention, huddles, `show.present`, history importers; in the decisions' terms since the drift audit: the attention job, sign-off, `page.show`, `stream.*`) | Hold the cutover for R1–R5, R10 and R14; the rest can follow |
| D6 | Which of the ~14 service containers still need a worker | Keep only the ones agents actually act on; the rest become appliances or nothing |
| D7 | The cachyrig sidecar after step 1 | Keep it as staging for 0.5 releases |
| D8 | Whether the 16 or so 0.4-only secrets move at all | Move everything (the tool does), retire them after cutover |
| D9 | Guests' `agent.backend` / `speech.*` | Open from #280/#287; not needed for the switch (one owner) |

## Reproducing this

```sh
python castling/switchover/readiness.py --prs
python castling/switchover/dryrun.py --snapshot ~/rook-backups/hub-offload/<newest>.tar \
    --out ~/rook-backups/castling-switchover/dryrun-$(date +%Y%m%d) [--hub-url https://<0.5 hub>]
```

Use a Python with the repo's dependencies, such as the sidecar's
`~/.rook-castling/venv/bin/python` or `rook-pytest`'s venv. `dryrun.py`
also takes `--from <copy folder>`. Tests are in
`tests/test_castling_switchover.py`.
