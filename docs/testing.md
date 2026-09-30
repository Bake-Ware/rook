# Testing Rook

Two layers:

| Layer | What | Needs | Runs by default |
|---|---|---|---|
| Unit | `tests/test_*.py`: in-process, no network, no relay | the repo installed with `.[dev]` | yes |
| Integration | `tests/integration/`: a real relay, MCP server, dashboard and workers, driven over MCP | the above + `telesthete-hub` + `bash` | no (opt-in) |

Neither layer may touch a live hub, a live band, the worker update feed, or a
real `~/.rook-band-worker`. The integration layer always runs against an
**isolated test hub** started by `scripts/test-hub.sh`.

## Setup

```sh
uv venv -p 3.12 .venv            # or: python3 -m venv .venv
uv pip install -p .venv/bin/python -e '.[dev]'
```

The integration layer also needs the relay binary. `scripts/test-hub.sh` looks
for it as `$TELESTHETE_HUB`, then `telesthete-hub` on `PATH`, then
`~/.cargo/bin/telesthete-hub`. To build it without touching anything else,
install it into its own root:

```sh
cargo install --locked --root ./.relay --git https://github.com/Bake-Ware/telesthete telesthitium
export TELESTHETE_HUB=$PWD/.relay/bin/telesthete-hub
```

## Unit tests

```sh
.venv/bin/python -m pytest -q --ignore=tests/browser_knowledge.py -p no:cacheprovider
```

The integration tests show up as `skipped` in this run. `tests/browser_*.py`
are manual browser scripts, not part of the suite.

## The test hub

```sh
scripts/test-hub.sh start  [--workers N] [--data DIR] [--port-base P] [--bind ADDR]
scripts/test-hub.sh status [--data DIR]
scripts/test-hub.sh stop   [--data DIR]
scripts/test-hub.sh reset  [--data DIR]    # stop, then delete DIR
```

Defaults: `--data ./test-hub-data` (git-ignored), `--port-base 17470`,
`--workers 2`, `--bind 127.0.0.1`. Other options: `--worker-prefix NAME`
(workers are `NAME-1..N`, default `testw`), `--no-knowledge`, `--no-dashboard`.
The same defaults can come from `ROOK_TEST_HUB_DATA`, `ROOK_TEST_HUB_PORT_BASE`
and `ROOK_TEST_HUB_WORKER_PREFIX`. `PYTHON` selects the interpreter (default:
the repo's `.venv/bin/python`, else `python3`).

What it starts, all in the background with `setsid` so they outlive the shell
that launched them:

| Process | Port | Notes |
|---|---|---|
| relay (`telesthete-hub`) | P/udp | peer TTL 60 s |
| MCP server (`python -m rook.band_mcp`) | P+2/tcp | static bearer token, `ROOK_KNOWLEDGE=1` unless `--no-knowledge` |
| dashboard (`python -m rook.remote.bootstrap`) | P+1/tcp | password in `secrets.env` |
| N workers (`python -m rook.worker`) | ephemeral | explicit `--hub/--psk/--name`, no update URL |

Isolation:

- A fresh band key, dashboard password and MCP token are generated into
  `DIR/secrets.env` (mode 600) on first start and reused until `reset`.
- Hub state (`ROOK_DATA_DIR`) is `DIR/hub`.
- Every process runs with a scrubbed environment and `HOME` inside `DIR`
  (`DIR/hub/home`, `DIR/workers/<name>/home`), so nothing reads or writes a
  real `~/.rook-band-worker`, `~/.config/rook` or enrollment.
- Ports 7474, 7005 and 8765 (a live hub's defaults) are refused.

Guards: `start` refuses when any of the three ports is already bound on any
address, when `DIR` is `$HOME` or a known live state directory, or when `DIR`
already holds Rook state (or anything else) but no `.rook-test-hub` marker.
`stop` and `reset` only act on a directory with that marker, and only signal
PIDs whose environment carries `ROOK_TEST_HUB_DIR=DIR`, so a recycled PID is
never killed.

Files under `DIR`: `run/*.pid`, `logs/*.log`, `secrets.env`, and
`test-hub.env` (mode 600) with the connection details the integration suite
reads: `ROOK_IT_MCP_URL`, `ROOK_IT_TOKEN`, `ROOK_IT_WORKERS`,
`ROOK_IT_DATA_DIR`, `ROOK_IT_KNOWLEDGE`, and the relay/dashboard addresses.

## Integration tests

Opt in with `ROOK_IT=1` (or `-m integration`).

Self-contained: the suite starts a throwaway hub in a temp dir on a random port
base, runs, then `reset`s it:

```sh
ROOK_IT=1 .venv/bin/python -m pytest -q tests/integration -p no:cacheprovider
```

Against a hub that is already running (faster when iterating, and how you test
a long-running instance on a dev box):

```sh
scripts/test-hub.sh start --data /tmp/th
ROOK_IT=1 ROOK_IT_HUB_ENV=/tmp/th/test-hub.env .venv/bin/python -m pytest -q tests/integration
```

Coverage: MCP initialize + `tools/list`, worker roster, `rook_call shell.exec`
on a test worker (and that its `HOME` is inside the data dir), a chat room
round trip, console open/read/close, knowledge create + search. Add new
end-to-end checks to `tests/integration/`; use the `hub` fixture's
`hub.call(tool, **args)` for one call or `hub.run(async_fn)` for several calls
in one MCP session.

## On a dev box through Rook

Agents without a local relay can run everything on a dev box worker. Use a
separate checkout so the box's own Rook install is never touched:

```text
rook_call(cap="shell.exec", worker="<dev-box>", args={"cmd":
  "git clone -b <branch> https://github.com/Bake-Ware/rook ~/rook-testhub && cd ~/rook-testhub && python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'",
  "timeout": 600})
```

Then start the hub (it daemonises, so `shell.exec` returns), or use
`rook_console_open` to keep its output in a searchable console room:

```text
rook_call(cap="shell.exec", worker="<dev-box>", args={"cmd":
  "cd ~/rook-testhub && scripts/test-hub.sh start --data ~/rook-testhub/test-hub-data --workers 2"})
rook_console_open(worker="<dev-box>", task="integration tests against the test hub",
  cmd="cd ~/rook-testhub && ROOK_IT=1 ROOK_IT_HUB_ENV=test-hub-data/test-hub.env .venv/bin/python -m pytest -q tests/integration")
```

The test hub is loopback-only, so run the integration suite on the same box
(or start with `--bind 0.0.0.0` on a trusted network). The test workers join
only the test band; they never appear on the live band.

## Cleanup

```sh
scripts/test-hub.sh stop  --data DIR   # stop processes, keep state and logs
scripts/test-hub.sh reset --data DIR   # stop and delete DIR entirely
```

The self-contained integration run cleans up after itself. If a run was
killed, `ls -d ${TMPDIR:-/tmp}/rook-it-*` finds leftovers; `reset --data` each one.
