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
  tooling here, not shipped in the `rook` package, run once in step 2 and
  deleted after cutover. It moves what "Fly it" says must survive and
  `--verify` proves it; whatever the move leaves rough, an agent reshapes
  on 0.5.
- `copy04.py` takes the read-only 0.4 copy; `smoke.py` checks a running
  0.5 hub serves the moved data and its workers answer.

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
waiting period, and are built after cutover, on 0.5. Bake (castling-switchover-
decisions, "Fly it"): "as long as all the artifacts survive transfer and the
hub and bands work correctly we can build the rest of the plane while we're
flying it".

| Gate | What must be true | Why it matters | Blocks step |
|---|---|---|---|
| R1 | The hub has a vault: `{{secret:}}` and secret settings read `<data>/vault.db` | The migration re-seals all 65 secrets into a file that nothing reads yet. Jobs, bots and plugins that need a secret can't run without it. | 2 (the move) |
| R2 | Worker events reach the hub (`TODO(castling:events)`) | The house agent's attention job on the hub (castling-attention: "an aggregation job on the hub run by the house agent"), `sms.received`, phone notifications and claudecode rules all depend on events. | 2 (the move) |
| R3 | Streams have a hub index (N28) | `stream.list`/`stream.search` over every stream (castling-caps rule 7: sessions, terminals, rooms and history readers become `stream.*`), and owners reading each other's streams. | 2 (the move) |
| R4 | Change records are records: the `change` plugin declares a `change` record and has no apply/revert runner | castling-caps C2 (Bake, Oct 30): "records only, with apply, test and rollback as a job the record links"; castling-changes: a lasting change gets a record (what, why, risk, testing, rollback, steps), "the change then proceeds", and ceremony scales with the project's trust level. Core's plugin pushes and worker updates don't depend on the plugin (drift #2). | 2 (the move) |
| R5 | Off-site hub backup, sealed with the recovery key | 0.5's only backup today is "stop the hub and copy the folder". That isn't enough once the band depends on this hub. | 2 (the move) |
| R6 | A 0.5 release format: `worker.update`, `update.*` (N27) | Without it, every core fix means reinstalling by hand on every machine. Plugins can already be pushed. | none; 2.4 for worker updates |
| R7 | Speech on the hub | The voice assistant. | none; 2.4 for kaiju's speech |
| R8 | The phone app runs 0.5 | The phone is the main surface. | none; 2.4 for bakephone |
| R9 | The desktop app runs 0.5 | Laptops add themselves through it. Passes today. | none; 2.4 for laptops |
| R10 | The desktop app updates on 0.5 | Without it, a desktop fix means reinstalling by hand. | 2 (the move) |
| R11 | Soak results written up (`docs/castling-soak.md`) | see "Soak bars" | none (follow-up, D5) |
| R12 | The hardening review's report (`castling/review.md`) | see "Review findings" | none (follow-up, D5) |
| R13 | The MCP door speaks OAuth | Done (`rook/core2/hub/doors/mcp_oauth.py`, the 0.4 shim's shape): with `oauth: <public address>` on the `mcp` door, claude.ai's connector signs in with the person's user name and password (`/authorize`) and gets a token for them acting through the agent `claude-web`. Needs the door reachable over public HTTPS. | 2.5 (for claude.ai) |
| R14 | Agents land work on 0.5 without 0.4: a task's sign-off (by its project's trust level), then `github.merge` at the signed-off head | Agents land PRs today through the 0.4 huddle's `huddle.vote` and the chair's pin. That flow ends when the 0.4 hub stops. On 0.5 sign-off replaces merge votes (castling-workbench). | 2 (the move) |

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

That rehearsal left jobs, settings, chat, huddles, console rooms, roles and
approvals behind. Since the fly-it rework every one of them is moved or
kept in the 0.5 hub's archive, and `--verify` proves it: see "The fresh
copy, verified" below.

That dry run turned up one bug, fixed in #307. The re-enroll checklist
printed names that 0.5 enrollment refuses, in commands that broke on the
apostrophe. Each machine now gets a valid 0.5 name, shell-quoted, and the
sacred list uses the same names (`castling/switchover/migrate/convert.py`
`worker_name`, tests in `tests/test_castling_migrate.py`).

### The fresh copy, verified (2026-10-31)

The real move needs a copy taken in the move itself (step 2). It was
rehearsed end to end on a fresh one, read-only:
`castling/switchover/copy04.py` on bakenetcanada at 2026-10-31 22:21Z
(release 1248.grumpy.badger; 71 files, 425 MB; the 24 worker bundles and
APKs under `updates/` not copied, GitHub releases have them), migrated into
a scratch 0.5 hub on cachyrig (`--apply`, 10 s) and checked with
`--verify`: **0 missing**. The private copy, reports and scratch hub are in
`~/rook-backups/castling-cutover/` on cachyrig (0700). Started on that data,
the 0.5 hub served all of it through its caps (`castling/switchover/smoke.py`).

The rehearsal found and fixed what didn't survive: chat rooms, huddles,
jobs and change records weren't moved, project links were dropped, nothing
but the journal was archived, and `migrate.md` §1 needed a `sqlite3`
command bakenetcanada doesn't have (and its WAL-mode copies grew `-wal`
files on any open, which migrate then refused).

| 0.4 artifact | 0.4 | moved | left, archived | why left |
|---|---:|---:|---:|---|
| knowledge pages / tasks / projects / concepts | 390 / 297 / 14 / 8 | all | 0 | |
| knowledge, task and project links | 1,260 | all | 0 | |
| task notes -> comments | 575 | all | 0 | |
| open whiteboard lines -> whiteboards | 230 | all | 0 | |
| handoffs | 298 | 76 (each task's latest open one, on its whiteboard) | 222 | castling-agents: the whiteboard "is the automatic handoff, but it is never a record" |
| whiteboard activity lines (calls, consoles) | 1,145 | 0 | 1,145 | castling-agents: "Essentials only, not full session context or raw output" |
| claims / hygiene findings | 231 / 469 | 0 | all | castling-workbench: "Drop verification flags and claims."; hygiene is "candidate for the PM's periodic sweeps" |
| secrets (name + value hash) | 65 | 65 | 0 | |
| people / accounts | 1 / 2 | 1 / 1 | 0 / 1 | castling-design: "Bake helps the other user directly." |
| API keys / chat links | 16 / 1 | 0 | all | castling-design: "a clean cut"; clients get 0.5 tokens, Bake relinks |
| workers | 38 | 38 on the re-enroll checklist | | castling-design: "workers re-enroll" |
| trust: project levels / other entries | 1 / 9 | 1 / 0 | 0 / 9 | 0.5's trust map is sacred machines and project levels (castling-changes) |
| chat rooms and huddles -> streams | 38 | 38 | 0 | |
| chat messages / huddle posts | 2,158 / 99 | all | 0 | |
| merge votes (motions, rounds, ballots, cards) | 793 | 0 | all | castling-workbench: "Drop the merge-votes room" |
| jobs | 12 | 12 (disabled, 0.4 definition kept) | 0 | |
| job runs | 396 | 0 | all | history: archived like the journal |
| change records | 27 | 27 | 0 | |
| house agent memory: active / superseded | 22 / 14 | 22 / 0 | 0 / 14 | castling-agents: the dream cycle "prunes/corrects" memories |
| journal | 20,092 calls | | all | castling-design: "old journal kept read-only as an archive" |
| console rooms and lines | 238,769 | | all | castling-streams: terminals are bounded, "with an optional export to a durable stream or file" |
| settings, roles, approvals, front door, attention feed, work sessions, enrollment | 6,313 rows | | all | each named with its decision in the verify report |
| files of the copy, byte for byte | 72 | 71 | 1 (the 0.4 vault key: secrets are re-sealed, D8) | |

"Left, archived" means it is in `<data>/archive/0.4/` on the 0.5 hub,
byte for byte and read-only, for an agent to reshape later (castling-design:
"export knowledge, projects/tasks, memory, vault as plain files; an agent
imports and reshapes them into 0.5"). Nothing 0.4 has is dropped.

## Order of moves

Bake, Oct 31 16:57 CDT (castling-switchover-decisions, "Fly it"): "Yeah so
as long as all the artifacts survive transfer and the hub and bands work
correctly we can build the rest of the plane while we're flying it. 0.4 is
holding back dev with decision checks and shit". D5: "cutover held for
gates R1-R5, R10 and R14; the rest can follow." So: no soak periods, no
waves held for time. Two bars, both checked by script: `migrate --verify`
exits 0, and the smoke checks pass. Everything else follows on 0.5 (see
"After cutover").

Rules for every step:

- Never leave Rook unusable. 0.4 keeps running untouched until step 2 and
  is then kept stopped, not deleted, as the rollback.
- 0.4 and 0.5 can't talk to each other, but a machine can run both (the
  cachyrig sidecar proves it). The 0.5 band shares 0.4's relay
  (telesthete-hub on bakenetcanada) under its own band id: never stop it.
- Whoever runs step 2 works over SSH to bakenetcanada, not through the 0.4
  Rook MCP: the 0.4 hub is stopped during the move.

### Step 0: land the code

1. Merge the green castling PRs (`python castling/switchover/readiness.py --prs`
   lists the open ones). `python3 castling/switchover/readiness.py` exits 0
   when R1–R5, R10 and R14 pass (they do as of 2026-10-31).
2. `git push origin d15e79bf:refs/heads/release/0.4`: the base for 0.4
   hotfixes and its APK and bundle builds (D1).

Rollback: nothing is deployed.

### Step 1: the production 0.5 hub, side by side (D2)

On bakenetcanada next to 0.4 (`df -h /`: 8.1 GB free on 2026-10-31; the hub
plus the archive needs about 1 GB; if it is short, use bakenetus):

1. A system user `rook05`; the code at `/opt/rook05/src` (an export of
   `castling` at its head, as `deploy/castling-sidecar/install.sh` does), a
   venv at `/opt/rook05/venv` (`pip install -e /opt/rook05/src` plus the
   telesthete commit `pyproject.toml` pins); data and config in
   `/var/lib/rook05` (0700, owned by `rook05`).
2. `python -m rook.core2.hub --config /var/lib/rook05/hub.yaml init --owner bake --relay 127.0.0.1:7474 --bind 127.0.0.1:8750 --url https://rook05.bakeforge.com`.
   The recovery key goes off the VM (Bake's password manager).
3. Doors: `http` on `127.0.0.1:8750` with `secure_cookie: true`, `mcp` on
   `127.0.0.1:8751` with `oauth: https://rook05.bakeforge.com` (R13).
   Library (`/var/lib/rook05/library`, symlinks into `/opt/rook05/src/rook/plugins`):
   tasks, projects, knowledge, concepts, whiteboard, huddle, jobs, changes,
   github, agents, agents-claude-code, notify, security, security-ml,
   attention, speech, backup-store; telegram and discord stay out until
   step 2.
4. A system unit `rook05-hub.service` (`User=rook05`, `ExecStart=/opt/rook05/venv/bin/python -m rook.core2.hub --config /var/lib/rook05/hub.yaml`,
   `Restart=on-failure`). Cloudflared: an ingress rule for
   `rook05.bakeforge.com` -> `127.0.0.1:8750` (and `/mcp` -> `127.0.0.1:8751`)
   above the catch-all, `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate`,
   `cloudflared tunnel route dns <tunnel> rook05.bakeforge.com`, then
   restart cloudflared (0.4's public routes blink for a few seconds).
5. Start it, then backups (R5): `settings.backup.target` (an rclone remote
   until a 0.5 worker off the VM has `backup-store`), then
   `python -m rook.core2.hub --config /var/lib/rook05/hub.yaml backup run` and `... backup verify`.
   Stop the hub again (`systemctl stop rook05-hub`): `migrate --apply` needs it stopped.

Rollback: `sudo systemctl disable --now rook05-hub`, remove the ingress
lines, restart cloudflared. Nothing of 0.4 changed.

### Step 2: the one move

Done in one sitting. Rook is down from 2.1 until the 0.5 hub answers in 2.3 (about 15 minutes).

Before 2.1 (castling-process, "Access before any freeze"), on the new hub and
by doing each: Bake signs in from a browser with his username and password;
the supervising Claude session makes a real call through a connector to it;
`hub.security` is `move-in` (the default; Settings, or `smoke.py` prints
`[ok] hub.security: move-in`), so nothing below waits on a person: no
holds, no gate blocks, device codes approved at once. `readiness.py` lists
these three.

1. **Freeze 0.4.** Say so in the castling huddle. On bakenetcanada:
   `sudo systemctl stop rook-band-mcp`. Nothing can write to 0.4 now, so
   the copy is final. Leave `telesthete-hub`, `cloudflared` and
   `rook-remote` running.
2. **Copy, move, verify.**
   ```sh
   python3 /opt/rook05/src/castling/switchover/copy04.py /home/ubuntu/rook04-final     # as ubuntu: reads only
   sudo chown -R rook05: /home/ubuntu/rook04-final && sudo mv /home/ubuntu/rook04-final /var/lib/rook05/
   cd /opt/rook05/src     # as rook05
   M="/opt/rook05/venv/bin/python -m castling.switchover.migrate --from /var/lib/rook05/rook04-final --to /var/lib/rook05/hub.yaml"   # copy04 writes the data at the copy's top
   $M                     # dry run: read the report
   $M --apply             # the move (hub stopped)
   $M --verify            # must say VERIFIED and exit 0
   $M --verify --json > /var/lib/rook05/data/migrate/verify.json
   ```
   If verify isn't clean: stop here, `sudo systemctl start rook-band-mcp`,
   fix the move in `castling/switchover/migrate`, start again from 2.1.
3. **Start 0.5 and smoke it.** `sudo systemctl enable --now rook05-hub`,
   then from anywhere, with an owner token
   (`... hub token --person bake`):
   `ROOK_TOKEN=... python castling/switchover/smoke.py --url https://rook05.bakeforge.com --verify-json verify.json`.
   It checks the hub answers, is in move-in, and every moved store reads back in full,
   paged to the end (active and archived records): knowledge, tasks, task
   comments, projects, concepts, huddles, jobs, change records, secret
   names, rooms and huddles as streams and their text and status events;
   each count must reach what `verify.json` says moved (it names the record
   caps from the checkout's manifests, so plugin renames follow). All `[ok]`.
4. **Bands and workers.** On each machine on the report's re-enroll
   checklist, the 0.5 worker beside the 0.4 one (its own venv, data and
   unit `rook05-worker`, as the sidecar), enrolled with `worker enroll <url>`:
   in move-in the hub approves the device code at once as the owner and
   journals it with the machine name, so nobody waits. First cachyrig (its sidecar worker
   re-enrolls; the sidecar hub stays as staging, D7), then janitor,
   soundwave, sojourn, kaiju (with `speech-engine`, D6 for the service
   containers), bakephone (flip to 0.5 in the app, D4), Windows machines
   through the desktop app. Smoke:
   `smoke.py --url ... --worker cachyrig --worker janitor --worker soundwave --worker sojourn --worker kaiju`:
   each online and answering `worker.ping`. Then one real call per kind of
   worker: `shell.exec` `["echo", "ok"]` on cachyrig, `battery.status` on
   bakephone.
5. **MCP and agents.** Each 0.4 API key on the report's list gets a 0.5
   token: `... hub token --person bake --agent <name>`. Claude Code:
   `claude mcp add --transport http rook05 https://rook05.bakeforge.com/mcp --header "Authorization: Bearer <token>"`,
   then a `worker.ping` on cachyrig through it. claude.ai: a custom connector
   at `https://<host>/mcp`; sign in with Bake's user name and password, no
   token (R13). The house agent: `agent.chat.send` `{agent: house, text: "say ok"}`
   answers. Landing work: a task's sign-off, then `github.merge` (R14).
6. **Chat bots.** Telegram and Discord on 0.5 (one hub per bot token; 0.4
   is stopped), then Bake relinks with `/link`.
7. **`castling` becomes `master` (D1).** Fast-forward `master` to
   `castling`. 0.4 hotfixes build from `release/0.4`.
8. **0.4 stays stopped, intact.** `sudo systemctl disable rook-band-mcp`
   (stopped in 2.1; its data, `/opt/rook-band-mcp`, `/etc/rook-band-mcp.env`
   and the 0.4 API keys stay as they are). Each machine's 0.4 worker is
   disabled once its 0.5 worker is online (`systemctl --user disable --now rook-band-worker`
   or the machine's equivalent); cachyrig's goes last, at a time Bake picks
   when no agent sessions run under it.

**Rollback** (any time, any sub-step): on bakenetcanada

```sh
sudo systemctl disable --now rook05-hub
sudo systemctl enable --now rook-band-mcp      # 0.4 as it was at 2.1: the copy only read it
```

then re-enable each 0.4 worker that was disabled
(`systemctl --user enable --now rook-band-worker`), turn the bots back
on in 0.4, and point clients back at the `rook` MCP server (their 0.4 keys
were never revoked). Work done on 0.5 meanwhile stays on 0.5 (there is no
0.5 -> 0.4 path); `/var/lib/rook05` keeps it. Rolling `master` back:
`git push --force-with-lease origin d15e79bf:master` (Bake's call).

### After cutover (built on 0.5, holding nothing)

- Reshape the 12 imported jobs (disabled; `vars.definition04` holds each
  0.4 definition). Only `soundwave-lxc-traffic-audit` was enabled on 0.4.
- Re-enter the hub settings that matter (timezone, voice engine, front
  door) as 0.5 settings; the archive has `settings.db`.
- Windows: ConPTY (N19), the desktop app's 0.5 worker on Windows; the watch
  on 0.5; the on-device checks above.
- The soak bars, the review findings, the not-ported list above
  (`page.show`, `trust.check`, `appliance.*`, history importers into
  `stream.*`, speech modes), D3 sacred machines, D6 service containers.
- Retire the 0.4-only secrets (D8) and rotate the reused ones (Bake's call).
- The archive (`<data>/archive/0.4`, about 0.45 GB, 0.4's API tokens and
  OAuth client files among it) rides in every hub backup. Once agents have
  reshaped what they need from it, moving it out is Bake's call.
- Tag `v0.5.0`; remove the 0.4 code in one PR once the transport no longer
  imports `rook.worker`.
- Delete the migration copies and `castling/switchover/migrate`,
  `import_04.py`, `copy04.py` and `migrate.md` with them. Keep 0.4 stopped
  and intact until Bake says it can go.

## What Bake decides

| # | Decision | Recommendation |
|---|---|---|
| D1 | When `castling` becomes `master` | At the end of step 2, with `release/0.4` at d15e79bf for 0.4 hotfixes |
| D2 | Where the 0.5 hub runs, and its hostnames | bakenetcanada next to 0.4 if disk allows (else bakenetus); new `rook05.` names, no DNS swap |
| D3 | Sacred machines on 0.5 (0.4 has none today) | Decide before step 2.4, since the list names machines |
| D4 | APK channel order and when family devices flip | beta (bakephone) → stable with 0.4 default → family devices after cutover, in person |
| D5 | Go for cutover, plus the soak bars, the review acceptances and the not-ported list (attention, huddles, `show.present`, history importers; in the decisions' terms since the drift audit: the attention job, sign-off, `page.show`, `stream.*`) | Hold the cutover for R1–R5, R10 and R14; the rest can follow |
| D6 | Which of the ~14 service containers still need a worker | Keep only the ones agents actually act on; the rest become appliances or nothing |
| D7 | The cachyrig sidecar after step 1 | Keep it as staging for 0.5 releases |
| D8 | Whether the 16 or so 0.4-only secrets move at all | Move everything (the tool does), retire them after cutover |
| D9 | Guests' `agent.backend` / `speech.*` | Open from #280/#287; not needed for the switch (one owner) |

## Reproducing this

```sh
python castling/switchover/readiness.py --prs
python3 castling/switchover/copy04.py <empty folder>          # on the 0.4 hub: a read-only copy
python -m castling.switchover.migrate --from <copy> --to <0.5 hub.yaml> [--apply | --verify]
ROOK_TOKEN=... python castling/switchover/smoke.py --url <0.5 door> [--worker NAME ...] [--verify-json FILE]
python castling/switchover/dryrun.py --snapshot ~/rook-backups/hub-offload/<newest>.tar \
    --out ~/rook-backups/castling-switchover/dryrun-$(date +%Y%m%d) [--hub-url https://<0.5 hub>]
```

Use a Python with the repo's dependencies, such as the sidecar's
`~/.rook-castling/venv/bin/python` or `rook-pytest`'s venv. `dryrun.py`
also takes `--from <copy folder>`. Tests are in
`tests/test_castling_switchover.py`, `tests/test_castling_migrate.py` and
`tests/test_castling_migrate_verify.py`.
