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

RESULTS
