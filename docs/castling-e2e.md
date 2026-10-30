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
| Enrollment | every worker enrolls with a one-time code (the QR); a fresh worker enrolls with a device code the owner approves on a hub made by `init`; it joins a second hub's band as a guest under the home owner's ceiling (the journal keeps the PSK as a reference), the guest hub is refused outside it, and the worker leaves the band when removed |
| Real ports | the worker also loads `rook/plugins/battery`, `torrent`, `shell` and `file`; what the machine can't serve is listed as unavailable, with the reason, and a call says why; `shell.exec` runs through the ticket, the worker gate and core's launch path, and `file.read` reads its output back |
| Jobs | `rook/plugins/jobs` in the hub library: a job every second calls `e2e.ping` on the worker as its creator; an owner's script job fires on `hold.created` (filtered to `e2e.wipe`) and its call reaches the worker through dispatch with the script's why |

## A 0.5 hub and worker by hand

From a checkout with the dev install (`pip install -e '.[dev]'`, which
brings telesthete), in an empty folder:

```sh
python path/to/rook/tests/e2e/relay.py 7474 &        # or a real telesthete-hub
python -m rook.core2.hub --config hub/hub.yaml init --owner me --relay 127.0.0.1:7474
#   prints the owner's sign-in token and the recovery key, once; the config holds the band key
python -m rook.core2.hub --config hub/hub.yaml &

# A machine signs in as a person (spec 12). It shows a code; approve it as the owner.
echo 'plugins: [path/to/rook/tests/e2e/plugins]' > worker.yaml
python -m rook.core2.worker --config worker.yaml enroll http://127.0.0.1:8740 --name laptop
#   "To add laptop, approve code BCDF-GHJK on hub 1a2b…"; in another shell:
#   curl -s -H "Authorization: Bearer $TOKEN" -d '{"code":"BCDF-GHJK"}' localhost:8740/enroll/approve
python -m rook.core2.worker --config worker.yaml &
```

Enrollment writes `anchor.json`, the home band into
`data/bands.json` and `name`/`worker_id` into `worker.yaml`. Instead of a
device code, the owner can make a one-time code
(`curl -s -H "$H" -d '{}' localhost:8740/enroll/code`) and the worker claims
its QR text: `... enroll 'rook://enroll?url=…&code=…&hub=…'`. A hub whose
config has `enroll: {url: https://…, discover: true}` answers local network
discovery (`... enroll --discover`). Off this machine, enrollment needs https
(`--insecure` overrides).

Then, as the owner (or in a browser: open `http://127.0.0.1:8740/` and sign in
with the token init printed; see docs/castling-dashboard.md):

```sh
H="Authorization: Bearer $TOKEN"       # the token init printed
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

Another hub's band, as a guest (spec 9.1): its owner makes an invite
(`POST /bands/invite {"role": "guest", "ceiling": {"grants": ["e2e.*"]}}`),
the home hub's owner accepts it for a worker with a ceiling of their own
(`POST /memberships {"worker": "laptop", "invite": {…}, "ceiling": {…}, "why": "…"}`)
and takes it off again with `{"worker", "band", "role": "removed", "why"}`.
`{"worker", "role": "home", "ceiling"}` narrows the home band's ceiling.

More tokens: `python -m rook.core2.hub --config hub/hub.yaml token --person me --agent claude-code`
(add the agent under `agents:` first). `... recovery-key` shows the owner's recovery key again.

Agents: add `mcp: { bind: "127.0.0.1:8741" }` under `doors:` and give the
agent its own token. Claude Code:
`claude mcp add --transport http rook http://127.0.0.1:8741/mcp --header "Authorization: Bearer <token>"`;
stdio-only agents run `ROOK_TOKEN=<token> python -m rook.core2.hub.doors.mcp_stdio`.
