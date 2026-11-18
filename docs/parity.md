# Parity checklist: what an agent does on 0.5, done on core

Brief 44. Bake's measure, 17 November: "is it reaching usable parity? Can you, as an agent, interface with everything that's on Rook in the same way or improved, given what is available to you?" Scope this round: the hub, the site as it stands on rookbeta.bake.systems, and the standard Python worker. Phones, TVs, microcontrollers and other hubs are not in it.

One row per thing an agent does through 0.5's MCP door, taken from `docs/inventory.md` (sections 3.1 and 3.3) and from 0.5's own door and manifests on `origin/castling` (`rook/core2/hub/doors/mcp.py`, `rook/core2/builtin/*.yaml`, `rook/core2/hub/**/plugin.yaml`, `rook/plugins/{shell,file,jobs}/plugin.yaml`). The 0.5 column is read from that code; the live 0.5 hub was not touched.

Anything in 0.5 that is a gate is not a row: `holds`, `session` (it narrows what a caller may call), approvals, trust, elevation, the security gate, sacred machines, risk tiers. Bake took those out of core on purpose.

## How it was run

- **Local**: a hub and a worker started from this branch (first on 7ed52527; rows 36 to 38 and 49 to 51 again on aaab7fcc, with briefs 40, 41 and 42 merged) with the README's own commands. The hub ran on an ephemeral port, the worker under a throwaway `HOME`, so `~/.rook-worker` and the live worker were left alone. A person's steps went through the site's API: the first user through `/api/setup`, the agent token through `/api/tokens`. After that, everything went through `POST /mcp`, with the agent token, as JSON-RPC, the way an agent does it. Times are wall-clock, measured at the client. Machine: cachyrig, Python 3.14, Telesthete at the pinned commit.
- **Beta** (`https://rookbeta.bake.systems`): run on 18 November, 00:30 to 00:39 UTC, with the door token the reviewer gave (`patient-steady-spruce`, owned by Bake), on its one worker, cachyrig (`quiet-calm-candle`), at one agent's pace. The reviewer said the beta ran core 7b0d7608. A redeploy by another session happened during the run (Found waits 3); afterwards the hub still served the same caps, and the door cannot say which commit it runs (`serverInfo` is `0.1.0` on every build). Only light rows ran there, and nothing was changed on it but what the rows make: one secret, `parity_check_20261117_183022`, is left there because core cannot delete a secret (row 56); the two jobs were deleted. The results are in "The beta run" below.
- **Tests**: `tests/test_parity.py` covers rows 1 to 25, 36 to 38, 50 and 51 against a real served hub and a real worker over the real carrier, through the MCP door over HTTP. Each test names the rows it covers.

Verdicts: **same**, **improved**, **missing** (0.5 does it and core cannot), **broken** (core has it and it does not work), **pending brief N** (another brief this round builds it), **not in plan** (0.5 does it and PLAN.md never mentions it: a gap for Bake to decide, not a fault).

## The rows

### The door, getting in, workers and caps

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 1 | Connect to the door and see the tools | `POST /mcp`; 7 tools: `call caps workers journal holds session stream` | `POST /mcp`; 5 tools: `workers caps call journal stream`. `holds` and `session` are gates, left out on purpose | same | `initialize` 12 ms, with the server's instructions; `tools/list` gives the five |
| 2 | Get in with a token; be refused without one | Bearer token, agent acting for a person | Bearer agent token (T1, T4). Missing, wrong and revoked tokens get 401 with a code and a next step | same | No token: 401 `token_missing` 13 ms. Junk token: 401 `token_invalid`. A token revoked on the Auth page: 401 `token_revoked` 13 ms |
| 3 | List workers with their state | `workers`: name, id, plugins, description, problems, `last_seen`, facts | `workers`: moniker, GUID, owner, online, `last_heartbeat`, facts, health, gateway MAC, band or roaming, cap names, open stream count, devices, and the machines knocking | improved | 11 to 14 ms. Description and problems are not in core (rows 40, 47) |
| 4 | List caps with schemas, by worker and by name | `caps`: one line per cap, full detail for an exact match or `full`; prefix and glob match | `caps`: name, description, schema and returns for every match; `worker` (or `hub`) and substring `match` | same | `caps worker=hub` 15 ms, 16 hub caps. `caps match=shell.` gives the three shell caps. Core always sends the full schema (no one-line mode, no glob) |
| 5 | See a worker's heartbeat and health | `worker.status` cap; `last_seen` in `workers` | In `workers`: `last_heartbeat` and `health` (cpu, ram, disk, uptime) from every heartbeat (H1, H4) | improved | `health: {cpu: 75.0, disk: {...}, ram: {...}, uptime: 1096545}`. More H4 fields are pending brief 43 |
| 6 | See a worker go offline and get a useful error | Offline workers listed; a call fails with the real error | Offline after three missed heartbeats (H3); its record and caps are kept; a call answers `worker_offline` with a next step | same | Worker stopped: marked offline 35 s later (interval 15 s). `call` then answers in 12 ms: `worker_offline: … Next: Wait for … or call another worker that announces shell.exec.` Started again, it linked at once with no second knock |

### Calls and commands

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 7 | Call a cap and get its result | `call` → `{id, ok, result}` | `call` → the result itself | same | Calls 11 to 24 ms |
| 8 | Run a command: stdout, stderr, exit code | `shell.exec`: argv, cmd, shell, stdin, cwd, env, **timeout (default 30 s, at most 3,600 s)**; output cut at 8 MiB per stream | `shell.exec`: argv, cmd, shell, stdin, cwd, env. No timeout and no cut | improved (no bound, as Bake asked) | `cmd: "echo out; echo err >&2; exit 3"` → `{code: 3, ok: false, stdout: "out\n", stderr: "err\n"}` 17 ms. argv, cwd, env, stdin and `shell: bash` all as given. Windows is not built in core's shell; it is outside this round |
| 9 | Find a program | `shell.which` | `shell.which` | same | `git` → `"/usr/bin/git"`; a missing name → `null` |
| 10 | Get errors that say what to try next | `{ok: false, error}` | Every refusal has `code: message Next: …` (C3) | improved | `unknown_cap`, `unknown_worker`, `bad_message` (no why), an unknown argument, a missing cwd: each one has a next step |
| 11 | Large output | Cut at 8 MiB per stream (`truncated`) | Whole, up to the 16,000,000-byte message cap (docs/protocol.md "Sizes"). Above it: `cap_failed` | improved, one bound | 3,000 lines 24 ms. 10 MB of text whole, 2.1 s. 20 MB: `cap_failed: its result is 20000111 bytes encoded, over the 16000000 a message may be`, and its next step ("Check the arguments against the schema") does not help. NUL-heavy output grows sixfold in JSON: 10 MB of `/dev/zero` is 60 MB encoded and refused |
| 12 | Follow a long call as a stream | Call still running after 45 s → `{id, running, worker: rook, stream}`; follow `status` events | Same shape after 45 s; the stream's last entry is the reply | same | `sleep 50; echo slept`: the call answered `running` at 45,014 ms; one `stream read wait=40` gave the reply 2 s later and `open: false` |

### Terminals and streams

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 13 | Open a terminal | `shell.open` (pty, cols, rows, title) → `{stream}` | `shell.open` (same arguments) → `{stream, title, description}` | same | 23 ms |
| 14 | Type into it, read it, resize it | `stream` with `input={text}`, `{resize: [c, r]}`, `{signal: INT}` | `stream` actions `write`, `read` (from, wait, limit), `resize` | same, except signal | `stty size` read `30 100`, then `11 77` after resize. Ctrl-C typed as `\u0003` stops a program in a terminal (the stream closed). A pipe stream (`pty: false`) has no way to be signalled |
| 15 | Close a terminal; learn how it ended | `stream.close`; the process's exit closes the stream with its exit code (a `status` event with `exit_code`) | `stream close` with a why, journaled; a process that exits closes the stream | **missing: exit code** | `exit 5` in a terminal: the stream closed, and neither the stream, the stream list nor the journal says 5. The journal's close entry names `closed_by: "worker"` |
| 16 | Share a stream: it outlives its opener, many clients | Worker-owned streams | A second agent's token wrote to and closed a stream the first opened (S2) | same | Entries show who typed (`from`) |
| 17 | Read a 3,000-line stream | `stream.read` from a cursor | `stream read` from a position, pages of 200 entries | same | `seq 1 3000` on pipes: all 3,000 lines in order, 2 reads, 0.21 s |

### Journal

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 18 | Read the journal, narrowed | `journal`: call, cap, worker, person, agent, project, kind, session, chain, since and until (Unix time); default 20, **at most 500** | `journal`: worker, cap, user, kind, job, run, since and until (ISO 8601), text; default 50, no maximum | improved | 13 to 15 ms. Each call entry has user, token, worker, cap, why and result |
| 19 | Find one call by its id | `journal call=<id>` | `journal text=<id>` | same | The call id from a reply found exactly its entry |

### Secrets

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 20 | Make a secret and use it by reference | `secret.set`; `{{secret:NAME}}` in args | `secret.create` on worker `hub`; `secret:name` as a whole argument value (X2) | same | The worker got the value: `stdout: "got:s3cr3t-Parity-Value-91\n"` |
| 21 | Keep the value out of the journal and the list | Journaled as `[redacted]` | Journal entry reads `got:(secret:parity_pw)`; a text search for the value finds nothing; `secret.list` has no values | same | The call's own reply to the agent carries the value as the worker printed it; only the journal copy is scrubbed (L4) |

### Jobs

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 22 | Make a job and see it run on an event | `job.create`; runs in the journal | `job.create` (match, one call) and `event.emit`; the run is in the journal under `kind=job` | same | Emit to run in the same second; a secret reference in the job's args resolved, and stayed scrubbed in the journal |
| 23 | Chain jobs | Branching jobs | A job matching `job.success` with `job_moniker` runs after it; both runs share one run ID (J3, J5) | same | Both entries carry run `9309fa62…` |
| 24 | List, pause, delete jobs | `job.list`, `job.update`, … | `job.list`, `job.pause`, `job.unpause`, `job.delete` | same | 12 to 13 ms each |

### Plugins

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 25 | Install a plugin and call its caps | `POST /plugins/push`, `core.plugin.install` | `plugin.install path=<folder on the hub's disk>`; hub caps load at once, worker parts are pushed and hot-loaded | same | Install 19 ms; the hub cap answered at once and the worker cap within 1 s, with no restart |

### Rows not in the test file

| # | What the agent does | 0.5 | Core | Verdict | Evidence (local) |
|---|---|---|---|---|---|
| 26 | A job on a schedule | `job.create` with cron | `match: {event: schedule, payload: {schedule: "every 1m"}}` | same | Created 23:00:02; it ran at 23:01:00 and printed `23:01:00` |
| 27 | Run a job now | `job.run` | No cap. An event job can be started with `event.emit` of its event; a schedule job cannot be | missing | Not in the J lines (inventory §1.4) |
| 28 | Stop a running call or job run | `job.cancel`; `shell.exec` timeout | Nothing. See Found waits 1 | missing | |
| 29 | Change a job | `job.update` with revision | Delete and make it again | missing | |
| 30 | Trigger a job from a webhook | `job.webhook` gives URL and HMAC key | `POST /webhook` with a webhook token (J2). Nothing mints a webhook token: the site mints only agent tokens, and no cap mints any token but join | **broken** | `hub/rook_hub/auth.py:53` has the `webhook` purpose; `site.py:705` and `caps.py:47` are the only agent-reachable mints |
| 31 | Install a ready-made job | `job.templates`, `job.install` | No | not in plan | inventory CAP-58 |
| 32 | Mint, list and revoke tokens as an agent | `token.create`, `token.list`, `token.revoke` | Auth page only. T5 says an agent mints under its user's chain, and the `token_invalid` refusal tells the user to "ask … an agent of theirs, to mint a new token", which an agent cannot do | missing | |
| 33 | Join a new worker unattended | `worker enroll --code` | `worker.join-token` mints one (expires after 10 minutes, one use: T2 names this). The worker start command takes only the hub address, so the standard worker cannot use it | missing | Mint 16 ms. `core/python/rook_start.py:43`: no token argument. `Worker(token=…)` exists in code |
| 34 | Approve a knocking worker | Enrollment by code (`token.approve`, the hub CLI) | `worker.approve key=…` | improved | 11 ms; the worker printed `Approved as nimble-snug-pigeon` and linked |
| 35 | See a pending worker's offered caps (R4) | n/a | `workers` → `knocking[].caps` | same, but empty | The standard worker knocks with `caps: []`: its caps are all default plugins, pushed after approval |
| 36 | Read and write files | `file.read/write/list/search/stat` | The same five caps, with 0.5's arguments; `max_bytes` and `limit` cut only when given | same (brief 41) | Write with `create_parents` 14 ms; read 16 ms; base64 bytes both ways; recursive list; search found `needle two` on line 2 (56 ms); stat. A 12 MB text file read whole in 2.6 s; `max_bytes: 100` gave 100 bytes and `truncated: true`. A missing file: `cap_failed … check the path with file.stat`. Beta: not served (see Gaps, default plugins) |
| 37 | Read the environment | `shell.env` | `shell.env` by name (with default) or prefix | same (brief 41) | `name: HOME` 12 ms; a missing name with `default` gave the default; `prefix: XDG_` gave an object. Beta: not served |
| 38 | Ping a worker; read its battery | `worker.ping`, `battery.status` | `worker.ping` → `{pong, at}`; `battery.status` → `{percent, status}` | same (brief 41) | Ping 15 ms. cachyrig has no battery: `cap_failed: this worker has no battery …; call battery.status on a worker with a battery`. Beta: not served |
| 39 | Read one worker's status | `worker.status` | Covered by `workers` (row 3) | improved | |
| 40 | Read the worker's own log of calls | `worker.log` | No | not in plan | inventory CAP-13 |
| 41 | Restart a worker | `worker.restart` | No | not in plan | inventory CAP-12 |
| 42 | Update a worker | `worker.update`, rollouts | No | missing (U2 to U9; no brief this round) | |
| 43 | Install a plugin from a URL | n/a | Folder only | missing (PL4 says "a folder or a URL") | `plugin.install` takes `path` |
| 44 | Opt a worker out of plugins | n/a | `worker.plugins worker=… receive=false` | improved | Out: the caps list went empty; back in: all 7 caps returned within 1 s |
| 45 | Find a stream | `stream.list` (kind, open), `stream.get`, `stream.search` | `stream list` (open filter), with title and description | same for list; search missing | S5 housekeeping works from titles; there is no text search |
| 46 | Chat rooms on the hub | `stream.open` on worker `rook` | No | not in plan | inventory CAP-53 |
| 47 | Hub summary; door kill switch | `hub.status`, `hub.door` | No | not in plan | inventory CAP-35, CAP-37 |
| 48 | Read and change hub settings as an agent | `setting.list`, `setting.set` | Settings page only; no cap | missing for agents | P4 puts settings on a page; no plan line gives agents a way in |
| 49 | Hub name | n/a | Settings page (`POST /api/hub/name`); no cap | same, site only (brief 40) | Set and read back through the site locally. Not changed on the beta |
| 50 | Reassign a worker's owner; merge bands | n/a | `worker.reassign worker owner` over MCP; band merge on the Workers page only (no band caps exist, so none for merge either) | owner: same; bands: site only (brief 40) | Reassign 14 ms. An unknown owner is refused with "List the users and use a GUID or moniker from there", and no MCP tool lists users. The site creates only the first user, so a hub has one user to reassign to. Not changed on the beta |
| 51 | Worker config | n/a | `config.get`, `config.set` (set, unset); a `secret:` value goes in on the wire only | same (brief 42) | `config.set` 12 ms answered `sent: true`; `config.get` then showed `applied` equal to `version`; the secret's value not in the journal or in `config.get`. Beta, read-only: `config.get` on quiet-calm-candle 115 ms, `values: {}`, `applied: null`, the same as a fresh local worker with nothing set |
| 52 | Revoke a worker's key | n/a | none yet | pending brief 43 | Also H4's battery, temperature and GPU in the heartbeat |
| 53 | Back up the hub as an agent | `backup.*` caps | Command line only (`python -m rook_hub backup`) | missing for agents | F3 is met by the command; no cap |
| 54 | Redact a value from the journal | n/a | `journal.redact value=…` | improved | Found by text, redacted (`redacted: 1`, 53 ms), not found again |
| 55 | Network primitives on a worker | Appliance actions at the hub | `net.http`, `net.tcp`, `net.udp` (D3) | improved | `net.http` to the hub's own `/api/setup` → status 200, headers, body, 17 ms |
| 56 | Replace or delete a secret | `secret.set` replaces; `secret.delete` | `secret.create` with a used name → `secret_exists: … secrets are not replaced`; no delete | missing | |
| 57 | Reveal a secret's value | `secret.reveal`, held for the person | Secrets page only, for the signed-in user (X3) | same, by plan | |
| 58 | A worker stops mid-call | n/a | The call ends at once with `worker_disconnected` and a next step | same | `sleep 90` call, the worker stopped at 50 s: the follow stream gave the error within 3 s |
| 59 | Change a worker's heartbeat interval (H2) | n/a | The hub sends 15 s; `Hub.set_interval` exists, but no cap or page calls it | missing | `hub/rook_hub/__init__.py:516` |
| 60 | Devices | `appliance.*` | `device.add` | not this round | TVs and devices are out of this round's scope |

## The beta run

Every row run on the beta passed. Times are wall-clock from cachyrig to the beta through Cloudflare, so each is about 80 to 120 ms more than the local time.

| Row | What | Result | Time |
|---|---|---|---|
| 1 | `initialize`; `tools/list` gives the five tools | pass | 119 ms; 191 ms |
| 2 | No token → 401 `token_missing`; junk token → 401 `token_invalid` | pass | 112 ms; 105 ms |
| 3 | `workers`: quiet-calm-candle, cachyrig, online, form python; owner Bake; roaming; no one knocking | pass | 85 ms |
| 4 | `caps worker=hub` (19 caps); `caps match=shell.` | pass | 169 ms; 120 ms |
| 5 | `last_heartbeat` and health (cpu, ram, disk, uptime) | pass | in row 3 |
| 8 | `shell.exec`: out, err, code 3; argv; cwd, env, stdin | pass | 125, 114, 103 ms |
| 9 | `shell.which sh` → `/usr/bin/sh` | pass | 92 ms |
| 10 | Unknown cap, unknown worker, no why: each refused with a next step | pass | 191, 94, 98 ms |
| 11 | 3,000 lines in one reply | pass | 101 ms |
| 12 | `sleep 50`: `running` after 45.6 s; one `read wait=40` gave `slept` and `open: false` | pass (second try; Found waits 3) | 45,554 ms + 400 ms |
| 13, 14 | Terminal: open; write; `stty size` 30 100; resize; 11 77 | pass | 83 to 115 ms each |
| 15, 16 | Listed while open; closed with a why; the why in the journal | pass | 101, 96, 120 ms |
| 18, 19 | A call found by `cap`, then by its id through `text` | pass | 77 ms; 90 ms |
| 20, 21 | Secret made and used by reference; the journal reads `got:(secret:parity_check_…)`; the value is in neither the journal nor the list | pass | 101 to 176 ms |
| 22, 23, 24 | Job on an event ran (`parity-job`); the chained job ran under the same run ID; pause; both deleted | pass | run seen 220 ms after emit |
| 51 | `config.get` on quiet-calm-candle (read only) | pass | 115 ms |

Not run on the beta, and why: rows 6, 58 and the freeze (they stop or freeze Bake's worker); 11's 10 MB and 20 MB, and 17 (heavy rows, local only by the brief); 25 and 43 (`plugin.install` needs a folder on the hub's disk, which would change Bake's beta); 36 to 38 (the beta's worker does not serve those caps; see Gaps); 49 and 50 (they change Bake's hub name and owner); 26 (a minute-long schedule adds nothing over local).

Found on the beta, not in core: Cloudflare in front of the beta refuses a client whose User-Agent is Python's default (`Python-urllib/3.x`) with 403, error 1010 "browser_signature_banned", before the hub sees it. An agent written with the standard library is turned away at the door. The run sent `User-Agent: rook-parity-check/1.0` and went through. curl's default passes.

## Found waits

Calls that can wait for ever. Each is recorded as found; none is fixed (RULES.md, 17 November).

1. **A command that never ends cannot be stopped through the door.** Called `shell.exec cmd="sleep infinity"`. After 45,020 ms the door answered `running` with follow stream `snappy-nimble-crow`. `stream close` on that stream is refused: `call_stream: Stream … carries the reply of a call; it takes no input and closes itself when the call ends.` There is no cancel. The journal entry's result stays `{type: "running"}`. The process (`sleep infinity`, a child of the worker) kept running. The only way out was a second call, `shell.exec cmd="kill <pid>"`, after which the follow stream gave `{code: -15, ok: false}` and closed. The hub and the worker logged nothing. 0.5 has a 30 s default timeout on `shell.exec` and `job.cancel`; core has neither, as Bake asked. What, if anything, stops such a call is Bake's to decide.
2. **A frozen worker holds a call open for as long as it is frozen.** Called `shell.exec cmd="sleep 30; echo done"` and sent the worker SIGSTOP 5 s in. At 45.5 s the door answered `running`. At 49.7 s the hub marked the worker offline (H3). For the 185.8 s watched, the call stayed running and its follow stream stayed open, and so did a terminal stream on the same worker. The hub logged `Peer timeout: cachyrig` once; the worker logged nothing while frozen. After SIGCONT the worker logged `The link to the hub was lost; linking again.` and linked. The call then ended as `worker_disconnected: The worker's link closed before it answered.`, although the command had run to the end on the worker; its output was lost. The terminal survived and answered input. With no SIGCONT the call would have stayed running.
3. **On the beta: a call in flight while the hub was redeployed was never answered and never journaled.** At 00:30:24 UTC the run sent `call shell.exec cmd="sleep 50; echo slept"` to quiet-calm-candle. The journal's last entry from the run is 45 (00:30:24, `job.delete`). The next entries are another session's, from 00:34:42, with the why "after a redeploy": `worker_offline` twice, then `worker.online` at 00:34:43. The call has no journal entry at all: not `running`, not `hub_restarted`. The request got no answer and no error in about 6 minutes, until the run was stopped. Its HTTP client had no timeout, and the hub stopped while the request was waiting for it. Whether the request reached the old hub before it stopped, or was held by the proxy, cannot be told from the door; the beta's own logs would say. The same row run again at 00:37:39 passed. Locally, a hub that stops wakes a waiting call and answers it `running` (brief 35); this did not happen here.
4. **Seen, not a wait:** a stream `read` waits at most 40 s (`hub/rook_hub/streams.py:78`, `MAX_WAIT`). A longer `wait` is cut to 40 without saying so.

## Gaps

One line each: what 0.5 does, then what core does.

- Terminal and process exit code: 0.5 closes the stream with `exit_code`; core closes it and drops the code (row 15).
- Signals to a pipe stream: 0.5 `input={signal: INT}`; core has no signal action (row 14).
- Stop a running call or job: 0.5 `shell.exec` timeout and `job.cancel`; core cannot (row 28, Found waits 1).
- Run a job now: 0.5 `job.run`; core only through `event.emit` of an event job's event (row 27).
- Change a job: 0.5 `job.update`; core delete and make again (row 29).
- Webhook trigger: 0.5 `job.webhook` URL and HMAC; core's `POST /webhook` needs a webhook token that nothing mints (row 30, broken).
- Agent-minted tokens: 0.5 `token.create/list/revoke`; core Auth page only, while its own refusal text says an agent can (row 32).
- Unattended join: 0.5 `enroll --code`; core mints a join token that the start command cannot take (row 33).
- Replace or delete a secret: 0.5 `secret.set` and `secret.delete`; core refuses to replace and cannot delete (row 56).
- Stream search: 0.5 `stream.search`; core lists with titles only (row 45).
- Hub settings as an agent: 0.5 `setting.list/set`; core page only (row 48).
- Backups as an agent: 0.5 `backup.*`; core command line only (row 53).
- Plugin from a URL: PL4 names it; core installs from a folder only (row 43).
- Heartbeat interval: H2 says the hub sets it per worker; core sends 15 s and has no way to change it (row 59).
- Worker update: 0.5 `worker.update`; core none, and no brief builds it this round (row 42).
- New default plugins never reach an existing hub: `install_defaults` runs only when a data directory is new (`hub/rook_hub/__init__.py:216`, `hub/rook_hub/plugins.py:266`). A hub made before brief 41 keeps only `net` and `shell` after it upgrades: the beta's `plugin.list` shows only those two after the redeploy, and a local data directory made on 7b0d7608 and opened on aaab7fcc lists `net` and `shell`. So the file, env and health caps (rows 36 to 38) are not on the beta. 0.5 had no such case: its worker caps came from manifests it shipped. Getting them there means a `plugin.install` of each folder on the beta's disk, which is not this brief's to do.
- Users over MCP: refusals say "List the users", and nothing in the door lists them (row 50).
- Next steps on `cap_failed`: every worker-side failure gets the same "Check the arguments against the schema of …", even where the cause is not the arguments (no battery, a result too large) (rows 11, 38).
- The beta's Cloudflare refuses Python's default User-Agent with error 1010 (see The beta run); a deployment matter, not core.
- Not in plan, for Bake to decide: worker log, worker restart, worker description and problems, hub summary and door switch, chat rooms, ready-made jobs (rows 3, 31, 40, 41, 46, 47).
- Oversized results: 0.5 cuts at 8 MiB; core refuses above 16,000,000 encoded bytes with a next step that does not help, and NUL-heavy output grows sixfold in encoding (row 11).

## Bounds met while testing

Listed here because the testing ran into them. Brief 44 (stall test and bounds) lists every bound in core; this list does not replace it.

| Bound | Where | What it did here |
|---|---|---|
| A call answers `running` after 45 s | `hub/rook_hub/__init__.py:35` `CALL_WAIT` | Rows 12, Found waits 1 and 2 |
| A stream read waits at most 40 s; 200 entries a page unless asked | `hub/rook_hub/streams.py:78`, `:250` | Row 17, Found waits 4 |
| The journal gives 50 entries unless asked | `hub/rook_hub/door.py:185` | Row 18 |
| A message is at most 16,000,000 bytes | `docs/protocol.md`, "Sizes" | Row 11 |
| Offline after 3 heartbeats of 15 s | `hub/rook_hub/__init__.py:33`; H2, H3 | Row 6 |
| A join token lasts 10 minutes | `hub/rook_hub/auth.py:52`; T2 names "minutes and one use" | Row 33 |
