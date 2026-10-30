# Castling 0.5 end to end

`tests/e2e` runs a real 0.5 band on localhost: a UDP relay
(`tests/e2e/relay.py`), `python -m rook.core2.hub` and
`python -m rook.core2.worker` as separate processes, with keys and configs
generated in a temp folder, talking over the real Telesthete transport. The
tests drive the hub through its HTTP door, as a person would.

## Run the tests

```sh
python -m pytest tests/e2e -q          # on cachyrig: rook-pytest tests/e2e -q
```

About 30 seconds. Each test has a hard timeout (`E2E_TIMEOUT`, default 150 s)
and every process is killed at the end; a failing test prints the last log
lines of each process (full logs are in the test's temp folder). POSIX only:
the tests skip on Windows.

What they prove:

| Area | Tests |
|---|---|
| Announce | the hub lists the worker's caps (it fetches the manifests it doesn't hold with `core.manifests`); caps stay known with the worker offline and across a hub restart |
| A call | a door call with a why gets a ticket the worker verifies; the result comes back; the journal entry is on the `general` chain and the chain verifies |
| Refusals | no why; a child over its preset (and its first use of a cap waiting for an owner); a ticket sniffed off the band and replayed; a worker ceiling narrower than the hub's grant (refused by the hub early and by the worker for a validly signed ticket); a destructive cap held until the person approves or refuses |
| Plugins | a plugin pushed from the hub library, signed for that worker, installs and passes its `check`; a new version replaces it in the same worker process (hot reload); a broken version fails its check and is rolled back; `core.plugin.rollback` goes back by hand; installed plugins load again after a worker restart |
| MCP door | an agent token (claude-code for bake) calls a cap over MCP, its hold waits for the person, the journal reads back; the stdio shim does the same as a child process |
| Real ports | the worker also loads `rook/plugins/battery`, `torrent`, `shell` and `file`; what the machine can't serve is listed as unavailable, with the reason, and a call says why; `shell.exec` runs through the ticket, the worker gate and core's launch path, and `file.read` reads its output back |

## A 0.5 hub and worker by hand

From a checkout with the dev install (`pip install -e '.[dev]'`, which
brings telesthete), in an empty folder:

```sh
export ROOK_PSK=$(python -c 'import secrets; print(secrets.token_hex(24))')
python path/to/rook/tests/e2e/relay.py 7474 &        # or a real telesthete-hub
mkdir library
cat > hub.yaml <<'EOF'
data: ./hub-data
library: ./library            # plugins the hub may push to workers
users:
  me: { preset: owner }
doors:
  http: { bind: "127.0.0.1:8740" }
transport:
  bands: [ { psk_env: ROOK_PSK, relay: "127.0.0.1:7474" } ]
EOF
python -m rook.core2.hub --config hub.yaml anchor > anchor.json   # the worker's trust anchor
python -m rook.core2.hub --config hub.yaml &                      # first run: hub-data/owner.token

cat > worker.yaml <<'EOF'
data: ./worker-data
anchor: ./anchor.json
name: laptop
plugins: [path/to/rook/tests/e2e/plugins]
bands: [ { psk_env: ROOK_PSK, relay: "127.0.0.1:7474" } ]
EOF
python -m rook.core2.worker --config worker.yaml &
```

Then, as the owner:

```sh
H="Authorization: Bearer $(cat hub-data/owner.token)"
curl -s -H "$H" localhost:8740/workers
curl -s -H "$H" localhost:8740/caps?worker=laptop
curl -s -H "$H" -d '{"worker":"laptop","cap":"e2e.ping","args":{"text":"hi"},"why":"Trying the hub."}' localhost:8740/call
curl -s -H "$H" 'localhost:8740/journal?limit=5'
curl -s -H "$H" -d '{"worker":"laptop","cap":"e2e.wipe","why":"Trying a hold."}' localhost:8740/call   # held
curl -s -H "$H" localhost:8740/holds
curl -s -H "$H" -d '{"decision":"approve"}' localhost:8740/holds/<hold>
```

Put a plugin folder in `library/` and push it (owners only; the hub reads
the library at push time, so a new version is just new files there):

```sh
curl -s -H "$H" -d '{"worker":"laptop","plugin":"tv","why":"Install tv."}' localhost:8740/plugins/push
curl -s -H "$H" -d '{"worker":"laptop","cap":"core.plugin.rollback","args":{"plugin":"tv"},"why":"Back a version."}' localhost:8740/call
```

More tokens: `python -m rook.core2.hub --config hub.yaml token --person me --agent claude-code`
(add the agent under `agents:` first).

Agents: add `mcp: { bind: "127.0.0.1:8741" }` under `doors:` and give the
agent its own token. Claude Code:
`claude mcp add --transport http rook http://127.0.0.1:8741/mcp --header "Authorization: Bearer <token>"`;
stdio-only agents run `ROOK_TOKEN=<token> python -m rook.core2.hub.doors.mcp_stdio`. The `anchor` command stands in for
enrollment (spec 12), which will write that file onto the worker.
