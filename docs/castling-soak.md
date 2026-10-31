# Castling 0.5 soak on cachyrig

Load and soak tests of the side-by-side 0.5 install on cachyrig
(docs/castling-sidecar.md), driven only through its doors. The runner is
`tests/soak/soak.py`; its report math has unit tests in
`tests/test_soak_report.py`. Runs: 2026-10-31.

## How it runs

```sh
PY=~/.rook-castling/venv/bin/python          # the sidecar's Python: pyyaml + the installed rook
$PY tests/soak/soak.py setup                  # soak users + tokens, the soak plugin pushed (restarts the hub)
$PY tests/soak/soak.py burst --seconds 20 --threads 8 --out DIR
$PY tests/soak/soak.py run --minutes 45 --rate 20 --out DIR
$PY tests/soak/soak.py report DIR             # report.md again from DIR's jsonl files
$PY tests/soak/soak.py teardown               # users, tokens and the soak plugin gone (restarts hub + worker)
```

`setup` adds two people to the sidecar's `hub.yaml` (`soak-adult`, preset
adult; `soak-kid`, preset child), mints their door tokens and one more agent
token (claude-code acting for soak-adult) into `~/.rook-castling/secrets/`,
copies `tests/soak/plugin/soak` into the hub library and pushes it to the
worker. The soak plugin has `ping`, `blob` (a reply of N bytes), `sleep` and
`wipe` (destructive, so held) and `ping` as its post-install check: cheap
caps, so the load measures Rook rather than the programs it runs.

`run` starts, in one process:

| Thread | Door, identity | What it does |
|---|---|---|
| 4 × owner | HTTP, bake (owner) | soak.ping/blob (16 KiB, 256 KiB, 4 MiB), shell.exec (`true`, 1 MiB of stdout), file.stat/read/write, hub caps (hub.status, hub.workers, task.list, journal.search), the read views, refusals (no why, unknown cap), a held soak.wipe refused through `/holds`, soak.sleep 2 s, `shell.open` streams (2.7 MB of process output read to its close; a PTY shell typed into and closed) |
| 2 × adult | HTTP, soak-adult | ping, exec, file.read, journal view, a plugin push (refused: owners only) |
| 1 × kid | HTTP, soak-kid (child) | ping and exec (refused by rights), holds view |
| 4 + 2 × agent | MCP, claude-code for bake / for soak-adult | tools/list, `call` (ping, exec, 64 KiB blob, a held wipe), caps, workers, journal, a session narrowed to `soak.*` (a call inside it, one outside refused, the session ended), a `shell.open` stream read with the `stream` tool |
| probe | HTTP, owner | `GET /holds` every 0.5 s: it runs on the hub's event loop, so it shows any stall |
| holds | HTTP, owner | refuses the agents' held calls |
| plugin loop | HTTP, owner | every 2 min: push soak 1.0.1 (signed, installed, checked), then `core.plugin.rollback` to 1.0.0, with traffic on its caps |
| chaos | the sidecar's scripts | every 5 min, in turn: pause the relay 5 s (SIGSTOP/SIGCONT on `rook-castling-relay`), `restart.sh hub`, `restart.sh relay`, `restart.sh worker`, pause the relay 20 s, `restart.sh` (all three) |
| sampler | /proc | every 5 s: RSS, CPU, threads, open fds of hub, worker and relay; journal and records file sizes; the worker's replay file; `worker.status`. Every 5 min a SQLite backup of the journal and a timed `JournalStore.check()` of it |

Each operation is one line in `calls.jsonl` (time, op, identity, ok,
milliseconds, error); refusals the test asks for count as ok. Recovery after
a disruption is read from that log: the first quick worker call sent after
the disruption began that came back ok.

Nothing of 0.4 is touched: only `~/.rook-castling`, its doors, and the
`rook-castling-*` units through the sidecar's scripts (`restart.sh` is new
here: `restart.sh [relay] [hub] [worker]`).

## Results in short

Runs on cachyrig (12 cores), sidecar at castling c90a968f plus this branch's
harness (before the fixes), then at this branch (after). 0.4 kept running
beside it untouched throughout.

- **No failures in steady state.** About 105 minutes of soak in all. 10
  minutes without disruptions: 13,337 operations, 0 failed. 45 minutes with
  disruptions: 47,842 operations, 114 failed, every one of them inside a
  disruption window (hub restarting, relay paused or restarted). With the
  fixes, 20 minutes including a relay pause: 21,949 operations, 0 failed.
  The journal chains verified at every check.
- **Latency** of a worker call through the hub is ~7 ms at p50 and ~50 ms at
  p99 under mixed load; but a client making calls back to back on one
  connection paid ~45 ms each (Nagle + delayed ACK in the door). Fixed.
- **Memory** grew without end on both hub and worker under large replies:
  every pulled reply's chunks and every served blob sat in a reference cycle
  until a full GC. Fixed. The hub also kept every session it ever started.
  Fixed.
- **The hourly journal check blocks the hub's loop**: ~13,000 entries a
  second, so 5 s at 69,000 entries. Moved off the loop.
- **Filtered journal reads slowed with the journal**: a person's or agent's
  newest entries sorted every one of their entries (130 ms at 40,000), under
  the lock every call's journal append waits on. Indexes fixed.
- Left open by that PR, fixed in wave 7 (see "Transport fixes" below): lost
  band messages were never resent (a call in flight during a blip waited
  its whole timeout, 20 minutes for a plugin push); a relay restart cost
  ~20 s; large replies moved at ~1 MB/s; the worker's replay cache refused
  every call once it averaged more than 55 calls/s over 15 minutes.

## Throughput

`burst`: soak.ping as fast as possible through the HTTP door, owner token.

| Clients | Before | After (TCP_NODELAY) |
|---|---|---|
| 1 | 21 calls/s, p50 47 ms, p99 54 ms | 150 calls/s, p50 6.2 ms, p99 24 ms |
| 8 | 146 calls/s, p50 53 ms, p99 79 ms | 273 calls/s, p50 29 ms, p99 60 ms |

On a throwaway e2e band, 200 sequential calls on one connection: soak.ping
p50 45 ms before, 4.1 ms after; `GET /holds` 42 ms before, 0.5 ms after.

## Steady load (10 minutes, no disruptions, before the fixes)

13,337 operations at 22/s, 0 failed. Selected rows (ms):

| op | n | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| ping (soak.ping, HTTP) | 2939 | 7.4 | 36.9 | 50.8 | 79 |
| mcp_ping (soak.ping, MCP) | 2519 | 7.4 | 20.6 | 48.2 | 82 |
| exec (shell.exec `true`) | 688 | 8.7 | 24.9 | 53.8 | 101 |
| file_read | 476 | 8.2 | 25.3 | 52.1 | 90 |
| blob_16k | 421 | 17.6 | 52.1 | 71.8 | 92 |
| mcp_blob_64k | 376 | 137.6 | 272.7 | 329.8 | 332 |
| blob_256k | 118 | 422.0 | 920.2 | 1089.0 | 1100 |
| exec_1m (1 MiB of stdout) | 128 | 1574.1 | 3396.8 | 3929.5 | 3935 |
| stream_2_7m (2.7 MB process stream, read to the end) | 62 | 837.2 | 1863.8 | 2418.7 | 2419 |
| stream_term (PTY: type, read, close) | 57 | 213.8 | 250.8 | 274.1 | 274 |
| mcp_session (start, call in, call out refused, end) | 372 | 136.2 | 154.7 | 181.4 | 239 |
| held_refused (held, then refused) | 55 | 45.1 | 86.9 | 108.3 | 108 |
| hub_status / task_list (hub caps) | 351 | ~3 | ~43 | ~48 | 74 |
| refusals (no why, unknown cap, child) | 681 | ~2 | ≤22 | ≤43 | 71 |
| probe_loop (`GET /holds`, on the hub loop) | 1195 | 1.3 | 3.8 | 8.7 | 38 |

Large results go over Drop at roughly 1 MB/s on localhost: 4 MiB took
p50 6.1 s, p99 12.6 s in the long run.

## 45 minutes with disruptions (before the fixes)

47,842 operations at 17.7/s; 114 failed, all inside disruption windows
(checked: none outside `[start - 1 s, end + 25 s]` of a disruption). Push
plus rollback of the soak plugin 42 times; 2 failed, both during relay
outages. Worker-call p50 6.7-8.3 ms, p99 ~55 ms outside the outliers that
waited out a timeout.

| Disruption | Worker calls ok again after | Calls failed or > 5 s |
|---|---:|---:|
| relay paused 5 s (SIGSTOP) | 5.4 s, 5.1 s | 7, 2 |
| `restart.sh hub` | 0.1 s, 0.1 s | 0, 0 (doors refuse for ~1 s: connection refused) |
| `restart.sh relay` | 20.2 s | 9 |
| `restart.sh worker` | 0.7 s | 9 (calls in flight when it stopped wait their timeout) |
| relay paused 20 s | 20.1 s | 11 |
| `restart.sh` (all three) | 1.7 s | 23 |

Recovery is measured from the call log: the first quick worker call sent
after the disruption began that came back ok.

## Resources

Per process run (a restart starts a new row), RSS in MiB, before the fixes:

| part | minutes | RSS first | RSS last | CPU % | threads max | fds first / last / max |
|---|---:|---:|---:|---:|---:|---|
| hub (steady run) | 10.0 | 88 | 155 | 21.7 | 22 | 37 / 22 / 39 |
| hub (long run, longest life) | 21.0 | 49 | 168 (max 189) | 17.9 | 22 | 36 / 39 / 39 |
| worker (steady run) | 10.0 | 46 | 107 (max 125) | 20.0 | 10 | 7 / 7 / 7 |
| worker (long run) | 20.4 | 99 | 160 | 15.7 | 17 | 7 / 7 / 10 |
| relay | 15 | 15-21 | flat | 7-10 | 1 | 7 / 7 / 7 |

File descriptors and threads stay flat: the door's threads follow its
keep-alive connections (~17 clients), the worker's follow its thread pool;
no leak. Streams: `worker.status` showed at most 1 open stream at any
sample and 0 at the end (every shell.open closed).

Memory did not level off, and an idle sidecar kept it (hub 117 MB, worker
133 MB ten minutes after the load stopped). Hunting it on throwaway e2e bands
(real relay, hub and worker processes; 8 clients; ping and 16 KiB-4 MiB
soak.blob replies), 6 minutes each:

| Variant | hub RSS | worker RSS |
|---|---|---|
| small replies only (ping, 16 KiB), 10 min | 46 -> 51 MB | 43 -> 46 MB |
| with 256 KiB / 4 MiB replies | 46 -> 378 MB | 43 -> 241 MB |
| same, `MALLOC_ARENA_MAX=2` | 46 -> 388 MB | 43 -> 238 MB |
| same, `MALLOC_MMAP_THRESHOLD_=131072` | 46 -> 378 MB | 43 -> 212 MB |
| **same, with the fix** | **46 -> 96 MB (flat from minute 2)** | **43 -> 105 MB (flat)** |

tracemalloc after `gc.collect()` saw nothing growing, which pointed at
garbage rather than live objects: glibc's `mallinfo2` in an in-process hub
showed bytes in use climbing to ~240 MB, then falling to 49 MB in one step
(a full collection), and `gc.collect()` took it to 34 MB. With
`gc.DEBUG_SAVEALL` the cycles were one `OtaDropReceiver` + telesthete
`_Receiver` + shim + task per pulled reply: the wrapper handed the receiver
its own bound method as the completion callback. Same on the sending side
(`OtaDropSender`, which holds the whole served blob). Between full
collections those cycles held every chunk, and the heap's high-water mark
stayed resident.

Sessions: the dispatcher never forgot a session (each MCP `session` call,
each lapsed default session), ~370 bytes each, and an agent's journal read
scanned all of them. Small per session, unbounded over weeks.

Journal: ~1.6 KB on disk per entry (bodies average 751 B; columns and
indexes double it): 69,000 entries = 110 MB. A full chain check walks
~13,000 entries a second:

| entries | check (s) |
|---:|---:|
| 22,921 | 1.67 |
| 44,052 | 3.45 |
| 68,968 | 5.04 |

The hub ran that check every hour on its event loop, so every door and the
band froze for the whole walk (5 s at 69,000 entries; ~2 minutes at a day of
20 calls/s). Retention prunes by age, so it is bounded, but by days, not
size.

Replay cache: at ~20 calls/s the worker held up to 23,015 nonces (1 MB file,
one fsync per call). It keeps each for 15 minutes (`REPLAY_KEEP`, spec
600-1200 s) and holds at most 50,000 (`REPLAY_MAX`), so a worker averaging
more than ~55 calls/s over 15 minutes refuses every call ("replay cache
full") until old nonces age out. One worker served 273 calls/s in the burst
(after the TCP_NODELAY fix): eight busy clients fill the cache in about
three minutes. Computed, not run (it would lock the worker for 15
minutes).

Filtered journal reads: `/journal` p50 went 6.6 -> 20.5 -> 57.4 ms as the
journal grew 20k -> 69k -> 102k entries. The store's indexes were
`(column, ts)` while reads are `ORDER BY seq DESC LIMIT n`, so SQLite sorted
every matching row: on the 102k snapshot an agent's last 20 entries took
130 ms (40k of its own), a person's 86 ms (24k), and the store holds its lock
(which every call's journal append needs) for the whole query. With
`(column, seq)` indexes both take ~0 ms. An agent whose sessions had
projects reads `agent = ? OR project IN (...)`, which still sorts (144 ms on
that snapshot).

## After the fixes

The sidecar upgraded to this branch (`install.sh --upgrade c6/soak`), 30
minutes, the same load, one 5 s relay pause, plugin push/rollback every
2 minutes (before the journal-index and TCP_NODELAY fixes):

- 32,702 operations at 18.2/s, 6 failed, all in the relay pause (5 timeouts
  and a plugin push that hung, finding 1 below).
- **Hub memory levels off**: 73 MB at start, 88 MB at minute 2, then
  90-94 MB for the remaining 28 minutes (before: 49 -> 168 MB in 21 minutes,
  still climbing).
- The worker still rises slowly: 101 MB at minute 8, 127 MB at minute 30,
  in steps, with its thread pool at 17 threads (the plugins' sync calls run
  in threads; `soak.blob` builds 4 MiB strings there). It is no longer the
  cycle; likely per-thread malloc arenas holding those buffers. Worth a
  longer run (finding 10).
- Chain checks: 70,512 -> 102,019 entries, 5.4 -> 7.9 s each, no problems.
- Worker-call p50 6.7-8.1 ms; p99 87-100 ms (the journal reads above were
  still slow in this build).

Then with every fix (the sidecar at this branch's head), 20 minutes, the
same load and one 5 s relay pause:

- 21,949 operations at 18.3/s, **0 failed**, the relay pause included (two
  calls waited it out, none timed out); 19 plugin pushes and rollbacks, none
  failed.
- Hub 49 -> 77 MB by minute 10, 77-80 MB after. Worker 43 -> 111 MB by
  minute 8, 111-121 MB after.
- Chain checks at 110k-132k entries: ~10 s each under the load, none on the
  hub's loop.

| op | before (45 min run) p50 / p99 ms | after p50 / p99 ms |
|---|---|---|
| ping | 6.8 / 53.8 | 7.2 / 44.2 |
| mcp_session (4 sequential MCP calls) | 135.2 / 213.4 | 12.8 / 89.6 |
| held_refused (2 sequential calls) | 44.9 / 91.8 | 4.6 / 41.4 |
| stream_term (open, write, reads, close) | 209.8 / 285.6 | 87.5 / 189.0 |
| view_journal | 20.5 / 72.3 (57.4 p50 at 100k entries) | 2.5 / 25.0 |
| mcp_journal | 2.7 / 99.4 | 2.3 / 31.9 |
| probe_loop (`GET /holds`) | 1.2 / 34.8 | 1.4 / 20.9 |

The soak left ~135,000 entries (212 MB) in the sidecar's journal; they go
with retention like any others. `soak.py teardown` removed the soak users,
tokens and plugin afterwards.

## Fixed in this PR

| Fix | Where | Evidence |
|---|---|---|
| Drop wrappers register a weak completion callback: no cycle, chunks and served blobs freed as soon as the transfer is dropped | `rook/worker/ota_drop.py` (shared with 0.4; behaviour unchanged while the wrapper lives) | e2e band: hub 378 -> 96 MB, worker 241 -> 105 MB; `test_a_finished_transfer_is_freed_without_the_cycle_collector` fails without it |
| The door sets TCP_NODELAY | `rook/core2/hub/doors/http.py` (HTTP, MCP and the site) | sequential call p50 45 -> 4.1 ms (e2e band); sidecar burst, one client 21 -> 150 calls/s; `test_keep_alive_requests_do_not_wait_for_delayed_acks` fails without it |
| Journal indexes `(column, seq)` instead of `(column, ts)`; the old ones are dropped and rebuilt once at start | `rook/core2/hub/journal_store.py` | 102k-entry snapshot: agent read 130 -> ~0 ms, person 86 -> ~0 ms; `test_filtered_reads_walk_an_index_in_order_and_old_indexes_go` fails without it |
| The hourly chain check runs in a thread (the store reads in pages under its lock) | `rook/core2/hub/runtime.py` | 5 s loop stall at 69k entries; `test_hourly_check_runs_off_the_loop_and_prunes_sessions` |
| Ended and expired sessions are forgotten hourly, except those a call in flight or a hold still uses; agents keep reading their pruned sessions' projects | `rook/core2/hub/dispatch.py`, `runtime.py` | in-process hub: +3,000 sessions per 3,000 rounds before; `test_prune_forgets_ended_and_expired_sessions_only` |

## Found, not fixed

For their lanes, or for Bake:

Items 1, 2, 4 and 5 are fixed in wave 7 (next section); 3 is not (a
request the old worker acked before it died is not resent).

1. **Lost band messages are never resent.** A request or reply dropped on
   the band (relay paused: its UDP buffer overflows; relay restarted) is
   only noticed when dispatch's timeout runs out: 15 s + 5 s for most caps,
   then one retry if the cap is idempotent. `core.plugin.install` allows
   20 minutes, so a push sent during a 5 s relay pause hung until the hub
   restarted. A transport-level ack/resend, or failing fast when the link
   reconnects, would fix it (transport lane).
2. **A relay restart costs ~20 s**: hub and worker re-register at their next
   keepalive. Matches docs/castling-sidecar.md.
3. **Calls in flight when the worker stops** wait their whole timeout; a
   worker that says goodbye (or a hub that fails calls to a worker that just
   re-announced) would answer them at once.
4. **Large replies move at ~1 MB/s** over Drop on localhost (4 MiB: p50
   6.1 s). With 8 clients and occasional 4 MiB replies, 2.6 % of calls hit
   soak.blob's default timeout. Big outputs want a longer timeout, a stream,
   or a faster Drop.
5. **Replay cache ceiling**: above ~55 calls/s sustained for 15 minutes a
   worker refuses everything (see Resources). Raising `REPLAY_MAX` costs
   ~150 bytes a nonce; keeping nonces only until `exp + SKEW` (tickets live
   60 s) would need the spec's 600-1200 s window changed. Bake's call.
6. **Journal growth**: ~1.6 KB an entry and a full check at ~13k entries/s.
   A day at 20 calls/s is ~2.8 GB and a ~2 minute check (now off the loop,
   but still a core's worth of CPU each hour). Checking only what was
   appended since the last verified head would make it incremental.
7. `journal.db` and `records.db` are created 0644 inside the 0700 `hub/data`
   (harmless there, but the files themselves are world-readable if moved).
8. When a client gives up on a request, the hub logs a `BrokenPipeError`
   traceback from socketserver (seen on the plugin push that hung).
9. Two recursive inner functions (`dispatch._reveals_secret.has`,
   `Enrollment.resolve.walk`) leave a tiny reference cycle per call. No
   payloads in them; negligible.
10. **Worker memory under big replies** still creeps after the fix (see
    After the fixes). A run of a few hours, or `MALLOC_ARENA_MAX` on the
    worker unit, would tell arenas from a second leak. Since #319 each
    plugin has its own pool of up to 8 threads, so a worker can run more
    threads (and malloc arenas) than the 17 seen here.
11. An agent's journal read that includes its projects
    (`agent = ? OR project IN (...)`) still sorts every match; two indexed
    reads merged would not.

## Transport fixes (wave 7)

Four of the findings above, fixed in `rook/core2/transport`,
`rook/core2/worker/security.py` and `rook/core2/transport/drop.py` (built
on the 0.4 Drop classes, which are unchanged: no 0.4 module is edited). Before: the sidecar at
castling `1c62dedc`; after: this branch. Same load each time (`run
--minutes 11 --rate 20 --chaos-every 100 --plugin-every 120`, and `burst
--seconds 300 --threads 8`). Raw runs: `~/rook-backups/c7-transport2/` on
cachyrig.

### 1. Replay cache: bounded by memory, not by load

The cache kept every nonce 15 minutes and held at most 50,000, so a worker
averaging more than ~55 calls/s refused everything. Bake's decision
(castling-protocol): "the worker remembers used ticket nonces for 10-20
minutes (well past the one-minute expiry plus clock drift)". The store now
keeps nonces in sets per 10 s of expiry, as 128-bit ints (~90 bytes each
instead of ~200), holds up to 200,000 (~18 MB), and when full drops whole
sets whose nonces are all at least 10 minutes old: 15 minutes while there
is room, 10 always. It refuses only beyond 200,000 nonces younger than 10
minutes, ~330 calls/s sustained for 10 minutes, faster than one worker
served here (259/s). Spec 6.3's MUST (`exp + 30`) is far inside that.

| burst, 8 clients, 300 s | calls | ok/s | refused |
|---|---:|---:|---:|
| before | 81,701 | 143.5 | 38,645 (`denied by worker: replay`), every call from 186 s on |
| after (first cut: nonces dropped after 3 min when full) | 77,737 | 259.1 | 0 |
| after (final: never under 10 min; machine busier) | 66,960 | 223.2 | 0 |

Tests: `test_replay_store_never_fills_under_sustained_load` (40 calls/s for
20 minutes against a 30,000 bound: the old store refused from minute 12;
every nonce younger than 10 minutes is still caught) and
`test_replay_store_refuses_only_nonces_younger_than_its_floor`.

### 2. Acks, bounded resends, dedupe

A new transport extension, `ack` (feature in the announce, `accept` in the
request; spec 2.1, minor-compatible: peers without it see nothing new):

- The target acks a request (`{"kind": "ack", "from", "ids": [...]}`,
  batched every 0.2 s) unless its reply goes out first, so a quick call
  costs one extra small message (the caller's ack of the reply), batched.
- Caller and target resend an unacked request or reply after 1 s, then
  every 2 s, for at most 60 s, holding at most 8 MB per band; the hub stops
  resending a call when dispatch gives up on it.
- A request seen again with the same id and ticket nonce never runs twice:
  while running it is acked again, once answered its held reply goes again.
  A re-minted ticket (6.5) is a new request. A reply seen again is only
  acked.
- On the first answer after resends went unanswered, everything still
  pending goes at once and a worker re-announces.

A plugin push is a call (`core.plugin.install`), so it is covered; its
bundle already rode Drop's own re-requests. While testing, the hub turned
out never to journal late replies: `wait_for` cancels the reply future, so
`_late` never filled. Fixed with the resend cut-off.

### 3. Relay restart

UDP has no connection to lose, so a restarted relay learns about a peer
only from its next frame. Core v2 UDP links now send their keepalive (a
route declaration, which also re-registers) every 5 s instead of 20 s; a
resend that goes unanswered twice re-declares at once; the first answer
after that re-announces. WebSocket links keep 20 s: they reconnect by
themselves (jittered backoff) and re-announce on connect.

A restarted hub calls the workers it remembers before their announce
reaches its transport, so resends also go to peers not heard yet (only a
peer that announced without `ack` gets none). Before that, the first call
after `install.sh --upgrade` was lost and waited out its timeout: the
sidecar answered `soak.ping` 20.0 s after an upgrade, now 1.1 s.

### 4. Large replies over Drop

On UDP the pacer allowed 2,000 frames/s and every Drop chunk is two frames,
so ~1 MB/s whatever the link. Core v2 UDP links pace at 20,000 frames/s
(burst 256; per band `pace: [rate, burst]`), and a pull keeps 8 windows of
64 chunks in flight instead of one (one REQUEST names several ranges; every
Drop sender serves them), re-asking for every gap after 0.25 s without
progress. Memory stays bounded: the receiver holds what it pulls, the
sender's queue holds at most the requested windows (512 KiB). Two
processes on localhost, a 4 MiB pull through the UDP relay:

| | MB/s |
|---|---:|
| before (2,000 frames/s, 1 window) | 1.0 |
| unpaced, 1 window | 9.5-11.2 |
| after (20,000 frames/s, 8 windows) | 9.8-10.2 |

Both ends are then CPU-bound in Python (AEAD and framing per 1 KB frame);
8 windows matter more on a real link, where one window per round trip caps
a 20 ms path at ~3 MB/s.

### The chaos run, before and after

Before: castling `1c62dedc`. After: this branch's transport (the run with
the final resend behaviour; the machine was busier, load average ~19 from
other agents' tests, so latencies are noisier than before). "Slow" is a
call that took over 5 s and still came back ok.

| | before | after |
|---|---|---|
| operations (11 min) | 10,736 at 16.2/s | 12,006 at 18.2/s |
| `restart.sh relay` | ok again after 20.1 s; 8 slow (max 20 s), 4 timed out | 0.3 s; none |
| relay paused 5 s | 5.1 s; 2 slow (max 10.7 s) | 5.1 s; 1 slow (5.0 s) |
| relay paused 20 s | 20.1 s; 12 slow (max 20 s), 0 failed | 20.1 s; 11 slow (max 20 s), 1 timed out (`exec_1m`, sent as the pause began) |
| `restart.sh worker` | 1.1 s; 4 slow (max 65 s), 4 timed out, 1 large reply not pulled | 2.0 s; 6 slow (max 65 s), 3 timed out (finding 3, below) |
| `restart.sh` (all three) | not reached in 11 min | 0.1 s; doors refuse while the hub restarts |
| `restart.sh hub` | doors refuse ~1-2 s (connection refused) | same |
| blob_4m (4 MiB reply) p50 / p99 | 6,715 / 11,466 ms | 1,651 / 3,810 ms (818 / 2,537 on a quieter machine) |
| exec_1m (1 MiB of stdout) p50 / p99 | 1,344 / 4,051 ms | 454 / 1,333 ms |
| stream_2_7m p50 | 686 ms | 363 ms |
| blob_256k p50 | 309 ms | 98 ms |
| mcp_blob_64k p50 | 135 ms | 17 ms |
| worker-call p50 (ping) | 6.7 ms | 7.1 ms |
| plugin push + rollback | 10, 0 failed | 10, 0 failed |

An earlier after-run (before resends went out at once on recovery) had the
20 s pause cost 2 timeouts: calls waited for their next resend slot after
the relay woke. That is why the first answer after an outage now sends
everything pending at once.

Still open: calls in flight when a worker stops wait their whole timeout
(finding 3): the old worker acked them, so nobody resends them, and the
new one never heard of them. A worker that says goodbye, or a hub that
fails a worker's acked calls when it re-announces with a new boot, would
answer them at once. A resent call that does reach a restarted worker gets
`denied by worker: replay` at once (its nonce is persisted; the call may
have run).
