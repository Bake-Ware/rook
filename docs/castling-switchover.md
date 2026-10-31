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

## Where things stand

| Piece | Live (0.4) | On `castling` (0.5) | State |
|---|---|---|---|
| Hub | rook.bakeforge.com on bakenetcanada (Oracle VM), releases through the CR runner | `python -m rook.core2.hub`: HTTP door, dashboard, MCP door, enrollment | runs as the cachyrig sidecar (#286), plain HTTP, localhost relay |
| Band relay | telesthete-hub behind mcp.bakeforge.com:443 | the same Telesthete wire (`TelestheteLink` reuses `rook.worker`'s transport) | a 0.5 band can share the live relay under its own band id |
| Workers | ~38 machines with OTA updates | `python -m rook.core2.worker`, enroll by device code | cachyrig only; no update path (N27) |
| Plugins | 63 | 50 plugin folders in `rook/plugins/`, the rest binned or core (castling/coverage.md) | see "Not ported" below |
| Vault | 65 secrets | `{{secret:NAME}}` reads `ROOK_SECRET_*` from the hub's environment as a stand-in | **no vault yet** |
| Speech / voice | kaiju Chatterbox + Whisper, voice front | `speech` + `speech-engine` + `speech-elevenlabs` | PR #287 open |
| Phone app | APK 0.4.23 stable, beta channel | 0.4/0.5 switch, 0.5 runs in Chaquopy | PR #298 open, emulator only |
| Desktop app | 0.2 | 0.4/0.5 switch, token sign-in, worker05 | merged (#290); no update check on 0.5 |
| Watch | 0.2.1 through the phone | wave 6 lane in progress | not on 0.5 |
| Security | trust map, hard stops | `security` (canary + taint) and `security-ml` (shadow) | merged (#284) |
| Agents | house agent, huddles, CR runner, sessions | `agents` + `agents-claude-code`, 8 personas | merged (#280, #288); huddles not ported |
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
the final cutover doesn't hold up the earlier side-by-side steps.

| Gate | What must be true | Why it matters | Blocks step |
|---|---|---|---|
| R1 | The hub has a vault: `{{secret:}}` and secret settings read `<data>/vault.db` | The migration re-seals all 65 secrets into a file that nothing reads yet. Jobs, bots and plugins that need a secret can't run without it. | 5 |
| R2 | Worker events reach the hub (`TODO(castling:events)`) | Attention and the Feed, `sms.received`, phone notifications and claudecode remote rules all depend on events. Without them the phone is a remote, not an assistant. | 4 |
| R3 | Streams have a hub index (N28) | The sessions list, history importers, the desktop Sessions tab, and owners reading each other's streams. | 5 |
| R4 | Change records exist (`TODO(castling:changes)`) | Plugin pushes and risky changes go through them. Without them nothing replaces the 0.4 CR runner. | 5 |
| R5 | Off-site hub backup, sealed with the recovery key | 0.5's only backup today is "stop the hub and copy the folder". That isn't enough once the band depends on this hub. | 5 |
| R6 | A 0.5 release format: `worker.update`, `update.*` (N27) | Without it, every core fix means reinstalling by hand on every machine. Plugins can already be pushed. | 3 (wave B onwards) |
| R7 | Speech on the hub | The voice assistant. | 4 |
| R8 | The phone app runs 0.5 | The phone is the main surface. | 4 |
| R9 | The desktop app runs 0.5 | passes | 3 |
| R10 | The desktop app updates on 0.5 | Without it, a desktop fix means reinstalling by hand. | 5 |
| R11 | Soak results written up (`docs/castling-soak.md`) | see "Soak bars" | 1 |
| R12 | The hardening review's report (`castling/review.md`) | see "Review findings" | 1 |
| R13 | The MCP door speaks OAuth | claude.ai's web and mobile connectors only do OAuth. 0.4 has `rook/band_mcp/oauth_shim.py` for them. The 0.5 door takes `Authorization: Bearer` only, so the Claude web key can't move. | 2 (for claude.ai) |
| R14 | Huddles with a merge vote | Agents land PRs today through the 0.4 huddle's `huddle.vote` and the chair's pin. That flow ends when the 0.4 hub stops. | 5 |

These are also open and should be done or explicitly dropped before the
cutover. They come from castling/coverage.md and the lanes' notes:

- Not ported: attention (N21/N23), huddles, `show.present`, `trust.check`,
  the appliance registry on the hub (N26), the 0.4 history importers, the
  session shim, conversation modes, streaming TTS, and speaker enrollment
  caps (#287).
- Windows: the 0.5 e2e suite is POSIX only, ConPTY isn't ported (N19), and
  the desktop app's 0.5 worker has never run on Windows.
- No in-app QR scanner on the phone. Android device names with spaces fall
  back to the name `worker` (`rook.core2.enroll.device.default_name`).
- Guests get no `agent.backend` and no `speech.*` (Bake decides; #280, #287).

### Soak bars (for Bake to set; proposed)

Soak PR #297 is still running its 45-minute pass. It has already found two
hub problems: sessions piled up, and the journal chain check stalled the
doors. Both are fixed on that branch. Proposed bars before step 1 puts real
traffic on a 0.5 hub:

- 24 hours on the sidecar with real use after the scripted soak: no lost or
  duplicated calls, and the journal chains still verify at the end.
- Memory, file descriptors and threads stay within ±10% after the first hour.
- After a hub, relay or worker restart, calls work again within 60 s with no
  manual step.
- Door latency at p95 for a read cap on a LAN worker: set from #297's numbers
  once they land.

### Review findings

Every finding in `castling/review.md` is either merged or accepted in
writing by Bake. The fix PRs open on 2026-10-31 are #292 (plugin rollback
re-verifies), #293 (linear glob), #294 (agent-made pages), #295 (secret
scope), #296 (enrollment is for people), #299 (elevation layers), #300
(hold release re-checks), #301 (argv/URL injection), #302 (file robustness)
and #303 (HTTP door bodies and timeouts).

### On-device checks (a person ticks these)

- The phone's 0.5 mode on bakephone: sign-in, worker online, a call each to
  battery, sms (refused without a grant), notification, voice.
- The watch through the phone on 0.5.
- The desktop app's "Add this computer" on one Linux and one Windows machine.
- A Windows 0.5 worker that has run for a week.

## The data move, rehearsed

The migration tool (`rook.core2.migrate`, dry run only) was run against the
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
  0.4 had already pruned everything older. The hub snapshots hold the rest
  if it's ever wanted.
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
| chat + huddles | 37 rooms, 1,568 messages, 25 huddles | Nothing (huddles aren't ported). Readable in the snapshot. |
| console rooms | 546 rooms, 238k lines | Nothing (streams start fresh) |
| roles, guidance, approvals, work sessions | 25 roles, 1 guidance override, 10 holds, 1,515 work sessions | Nothing. Personas replace roles. |

The dry run turned up one bug, fixed in this PR. The re-enroll checklist
printed names that 0.5 enrollment refuses, in commands that broke on the
apostrophe. Each machine now gets a valid 0.5 name, shell-quoted, and the
sacred list uses the same names (`rook/core2/migrate/convert.py`
`worker_name`, tests in `tests/test_core2_migrate.py`).

### The copy for the real move

The 2026-10-27 copy is fine for rehearsal but stale for the move, since 0.4
has kept running. Step 1 and step 5 each need a fresh copy, made on the live
hub by Bake, or by the house agent through a change request on 0.4, using
the commands in `docs/castling-migrate.md` §1: an `sqlite3 .backup` of every
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

`castling` → `master` (D1). Recommended: fast-forward `master` to `castling`
at the end of step 2, when the 0.5 hub has run for a week. Push a
`release/0.4` branch at d15e79bf first, so 0.4 hotfixes and the 0.4 APK and
bundle builds still have a base. Until the switch, the phone and desktop
apps default to 0.4, so a 0.4 release built from the new master behaves as
before. Keep the 0.4 code in the tree until step 6. The 0.5 transport still
imports `rook.worker` (`TODO(castling:transport)`).

Rollback: reset `master` to d15e79bf (nothing has built on it yet), or keep
deploying 0.4 from `release/0.4`.

### Step 1: a production 0.5 hub, side by side

Needs R11, R12, the soak bars and the review fixes.

- Where (D2). Recommended: bakenetcanada, next to 0.4, as its own system
  user and unit (`rook05-hub`), with data in `/var/lib/rook-castling`. It
  gets a new band on the same telesthete relay (its own band id, so neither
  side sees the other), and new hostnames through the existing cloudflared
  tunnel, e.g. `rook05.bakeforge.com` for the door and dashboard. Cloudflare
  provides TLS, and the door gets `secure_cookie: true`. The VM ran at
  95–98% disk in late October, so check the headroom first. If it is short,
  use bakenetus.
- `init` with owner `bake`. Put the recovery key somewhere off the VM.
- Migrate from a fresh copy: dry run, then `--apply` with the hub stopped.
  This gives 0.5 the real knowledge, tasks and memory to work with. Workbench
  records changed later on 0.4 don't follow, but a re-run at step 5 adds what
  is new.
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
- claude.ai web and mobile wait for R13.
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

Wave B (everything after this) waits for R6. Without it, each core fix means
visiting every machine. Plugin fixes can be pushed already.

Rollback per machine: `systemctl --user disable --now rook05-worker`. The
0.4 worker never stopped. Never restart cachyrig's 0.4 worker: it kills
every agent on the machine.

### Step 4: kaiju, Windows, phones, watch (wave B)

Needs R2, R6, R7, R8, plus the on-device checks.

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

Needs R1, R3, R4, R5, R10, R14, a week of steps 2–4 under real use, and
Bake's go (D5).

1. Announce a quiet window. Pause 0.4 jobs and stop agent writes on 0.4.
2. Take a fresh 0.4 copy, then `migrate` dry run and `--apply` again. This
   adds what is new since step 1. Records edited on both sides keep the 0.5
   version.
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
- Delete the migration copies. Optionally point `rook.bakeforge.com` at the
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
| D5 | Go for cutover, plus the soak bars, the review acceptances and the not-ported list (attention, huddles, `show.present`, history importers) | Hold the cutover for R1–R5, R10 and R14; the rest can follow |
| D6 | Which of the ~14 service containers still need a worker | Keep only the ones agents actually act on; the rest become appliances or nothing |
| D7 | The cachyrig sidecar after step 1 | Keep it as staging for 0.5 releases |
| D8 | Whether the 16 or so 0.4-only secrets move at all | Move everything (the tool does), retire them after cutover |
| D9 | Guests' `agent.backend` / `speech.*` | Open from #280/#287; not needed for the switch (one owner) |

## Reproducing this

```sh
python castling/switchover/readiness.py --prs
python castling/switchover/dryrun.py --snapshot ~/rook-backups/hub-offload/<newest>.tar \
    --out ~/rook-backups/castling-switchover/dryrun-$(date +%Y%m%d)
```

Use a Python with the repo's dependencies, such as the sidecar's
`~/.rook-castling/venv/bin/python` or `rook-pytest`'s venv. `dryrun.py`
also takes `--from <copy folder>`. Tests are in
`tests/test_castling_switchover.py`.
