# Castling 0.5 sidecar on cachyrig

A real 0.5 hub and worker running next to the live 0.4 system, so 0.5 can be
tried for real. Scripts: `deploy/castling-sidecar/`. It shares nothing with
0.4: its own code (an exported tree, not a clone), venv, data, keys, band,
relay, ports and user units. Nothing of 0.4 (`~/.rook-band-worker`, its venv,
`rook-band-worker` and the other `rook-*` units, port 7474, its keys) is read,
stopped or changed, and the install never restarts cachyrig's 0.4 worker.

## What runs where

| Piece | Unit | Address |
|---|---|---|
| Band relay (UDP, the e2e relay) | `rook-castling-relay` | `127.0.0.1:7475` |
| Hub: HTTP door + dashboard | `rook-castling-hub` | `0.0.0.0:8750`: http://127.0.0.1:8750/ here, http://192.168.1.185:8750/ on the LAN once ufw allows it (below) |
| Hub: MCP door | (same) | `http://127.0.0.1:8751/mcp`, this machine only |
| Worker `cachyrig` | `rook-castling-worker` | dials the relay |

The units are written to `~/.config/systemd/user/` but **not enabled**:
nothing starts at boot. `systemctl --user enable rook-castling-relay
rook-castling-hub rook-castling-worker` makes them start with the session;
`disable` undoes it.

```
~/.rook-castling/
  src/          the castling tree (git archive of the installed commit; src/.castling-sha)
  venv/         Python 3.12, `pip install -e src[manifest,desktop]`
  hub/          hub.yaml (0600, holds the band key), data/ (keys, journal, records), library/ (symlinks into src/rook/plugins)
  worker/       worker.yaml, anchor.json, data/ (installed plugins, replay cache, Claude Code work folder)
  secrets/      0700; each file 0600
    owner.token        bake's dashboard sign-in token
    recovery-key.txt   the hub owner's recovery key: needed only if this hub dies; keep a copy somewhere safe
    mcp.token          a door token for claude-code acting for bake
    claude-mcp.json    a Claude Code MCP config holding that token
```

## Install, upgrade, stop, remove

From any checkout of Bake-Ware/rook that has `deploy/castling-sidecar/`:

```sh
deploy/castling-sidecar/install.sh                    # first install, from origin/castling
deploy/castling-sidecar/install.sh --upgrade          # new origin/castling code; same data, keys and tokens
deploy/castling-sidecar/install.sh --upgrade <ref>    # any commit or branch
```

The first install runs `init` (owner `bake`, a new band key, the hub's keys),
puts the owner token and recovery key in `secrets/` without printing them,
starts the relay and hub, enrolls the worker with a one-time code from
`POST /enroll/code` (the QR path of spec 12), mints the MCP token and starts
the worker. It needs `uv`, `git` and `curl`; the venv's Python comes from uv.

The installed copy carries the other scripts too:

```sh
S=~/.rook-castling/src/deploy/castling-sidecar
$S/check.sh        # smoke test through both doors (prints no secret)
$S/stop.sh         # stop the three rook-castling-* units (never rook-band-worker)
$S/start.sh        # start them again
$S/uninstall.sh    # stop, delete the units, move ~/.rook-castling to ~/.rook-castling.removed-<time>
$S/uninstall.sh --purge   # ... and delete it instead
```

Logs: `journalctl --user -u rook-castling-hub -f` (and `-worker`, `-relay`).
Agent turns also leave Claude Code transcripts under
`~/.claude/projects/-home-bake--rook-castling-worker-data-plugin-data-agents-claude-code-work/`
(Claude Code's own folder; uninstall leaves it, delete it by hand).

Settings by environment, read by every script: `ROOK_CASTLING_HOME`,
`ROOK_CASTLING_OWNER`, `ROOK_CASTLING_WORKER`, `ROOK_CASTLING_RELAY_PORT`,
`ROOK_CASTLING_HTTP_BIND`, `ROOK_CASTLING_MCP_BIND`. The unit names are
fixed, so there is one sidecar per user.

## Signing in

Open http://127.0.0.1:8750/ on cachyrig and paste the token from
`~/.rook-castling/secrets/owner.token`. The site has Workers, Approvals,
Journal and Settings; plugins and agents add pages.

From another machine on the LAN, cachyrig's ufw has to let the port in
(it allows 8096 the same way). That is a firewall change, so it is yours:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 8750 proto tcp comment 'rook 0.5 sidecar'
sudo ufw delete allow from 192.168.1.0/24 to any port 8750 proto tcp     # undo
```

or tunnel instead: `ssh -L 8750:127.0.0.1:8750 cachyrig`, then
http://127.0.0.1:8750/. The door is plain HTTP: on the LAN the token and
session cookie cross the network in the clear. Fine for a trial at home;
put TLS in front before anything more. Enrolling *another* machine over
plain HTTP would send the band key in the clear too, so the worker refuses
it without `--insecure`; the band relay is also bound to 127.0.0.1, so only
this machine's worker is on the band for now.

## Claude Code over MCP

For one session (nothing written to your Claude Code config):

```sh
claude --mcp-config ~/.rook-castling/secrets/claude-mcp.json
```

The server is named `rook05` (so it doesn't clash with the 0.4 `rook`
tools); its tools are `call`, `workers`, `caps`, `journal`, `holds`, `session`
and `stream`. To add it for
good instead (this writes the token into `~/.claude.json`):

```sh
claude mcp add --transport http rook05 http://127.0.0.1:8751/mcp \
  --header "Authorization: Bearer $(cat ~/.rook-castling/secrets/mcp.token)"
```

Stdio-only agents: `ROOK_TOKEN=$(cat ~/.rook-castling/secrets/mcp.token)
~/.rook-castling/venv/bin/python -m rook.core2.hub.doors.mcp_stdio --url http://127.0.0.1:8751/mcp`.
More tokens: `~/.rook-castling/venv/bin/python -m rook.core2.hub --config
~/.rook-castling/hub/hub.yaml token --person bake [--agent claude-code]`
(add an agent under `agents:` in hub.yaml first).

## What's loaded

Worker `cachyrig` (from `src/rook/plugins`, listed in `worker.yaml`):

| Plugin | State |
|---|---|
| `shell` (`exec`, `which`, `env`, `open` = processes/PTY streams) | works |
| `file` (`read`, `write`, `list`, `search`, `stat`) | works |
| `screen-desktop` (`screen.capture`) | works (the worker reports `hardware: [display]`) |
| `notification-desktop` (`notification.post`) | works |
| `agents-claude-code` (`turn`, the `agent.backend` provider) | works: `claude -p` under the signed-in subscription, isolated from your own Claude Code settings, hooks and MCP servers |
| `llm-cli` (`complete`, the `llm.complete` provider) | works through Claude Code (System One = haiku) |
| `torrent` | loads, but deluged runs as user `deluge` and bake's `deluge-console` can't authenticate to it: calls fail with "Could not connect to daemon". Needs a deluge auth entry for bake (your call) |

There is no `info` plugin in 0.5 yet; `hub.workers` and `caps` cover it.

Hub (`hub/library`): `agents` (8 ready-made agents: house, pm, lead, qa,
writer, archivist, producer, security), `notify` (no channels set up yet),
and the workbench (`tasks`, `projects`, `knowledge`, `concepts`,
`whiteboard`: caps `task.*`, `project.*`, ..., no pages yet; a project
"Try Rook 0.5" with one task is there from the check). The library also
holds the worker plugins, so `POST /plugins/push` can reinstall them signed,
and `changes`: every push is a change record (`change.list`).

## What to try first

1. Sign in on http://127.0.0.1:8750/ and open **Workers** and **Journal**:
   the check's calls are there, each with its why.
2. `claude --mcp-config ~/.rook-castling/secrets/claude-mcp.json` and ask it
   to list the workers and run `uname -a` on cachyrig through `rook05`.
3. Talk to an agent (over MCP or the HTTP door):
   `call worker=rook cap=agents.chat.send args={"agent": "house", "text": "..."}`.
   Read back with `agents.chat.read`. A first turn takes about 10 s.
4. The workbench: `call worker=rook cap=task.list`, `project.list`, then
   `task.create {title, project: <project id>}` (a project is referred to
   by its record id).
5. Something that waits for you: a `destructive` cap (e.g. `torrent.delete`)
   is held; approve or refuse it on **Approvals**.

By hand with curl:

```sh
H="Authorization: Bearer $(cat ~/.rook-castling/secrets/owner.token)"
curl -s -H "$H" 127.0.0.1:8750/workers
curl -s -H "$H" '127.0.0.1:8750/caps?worker=cachyrig'
curl -s -H "$H" -d '{"worker":"cachyrig","cap":"shell.exec","args":{"argv":["uptime"]},"why":"Trying 0.5."}' 127.0.0.1:8750/call
curl -s -H "$H" '127.0.0.1:8750/journal?limit=5'
```

## Known rough edges

- After the relay restarts (`stop`/`start`, `--upgrade`), the worker reaches
  the hub at its first keepalive, about 20 s later; calls before that time
  out. `check.sh` waits for it.
- Agents can't reach the band themselves yet: their turns run with no MCP
  servers. To give them one, set `agents-claude-code.mcp_config` in
  `worker.yaml` (`settings:`) to an MCP config like `claude-mcp.json`; every
  turn then shares that token.
- The worker warns "anchor is writable by the worker": it runs as bake and
  owns its own folder. Harmless here.

## Tests

`check.sh` runs against this install. The e2e suite (`rook-pytest tests/e2e
-q`) runs its own band in a temp folder and doesn't touch the sidecar.
