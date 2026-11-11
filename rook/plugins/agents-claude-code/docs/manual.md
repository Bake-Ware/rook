# Claude Code backend

`agents-claude-code` lets agents think with the Claude Code CLI on one of the
band's workers, under whatever subscription `claude` is signed in with there.
It provides the `agent.backend` contract, so the agents package uses it for
every agent whose backend is `default` (when it is the chosen provider) or
`agents-claude-code.turn`.

The plugin runs on the hub; the CLI runs on the worker its `worker` setting
names, as a process there (`shell.open`). One call is one turn:
`claude -p --output-format json`, the prompt on stdin, `--resume` with the
session id from the last turn, the agent's system prompt appended with
`--append-system-prompt`. The reply, the session id and token usage come
back. A turn that runs past its timeout, or is cancelled, ends the process.

`agents-claude-code.models` answers the models its `model` setting and an
agent's model take, without asking the worker: Claude Code's aliases
(`sonnet`, `opus`, `haiku`, `fable`; the CLI has no command that lists
models), and `default`, the `model` setting. A full model id works too.

Settings (the hub's: Settings page, or `setting.set`):

| Setting | Default | What |
|---|---|---|
| `worker` | none | The worker where Claude Code is installed and signed in. Turns fail until it is set. |
| `claude` | `claude` | The program on that worker, on its PATH or a full path (`~` is its home). |
| `model` | `opus` | System Two's model when the agent names none. |
| `model_three` | `haiku` | System Three's model, for dream cycles. |
| `permission_mode` | Claude Code's | `--permission-mode` for turns. |
| `allowed_tools` | none | `--allowedTools`: tools a turn may use without asking. A turn given Rook may always use Rook's (`mcp__rook`). |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as the worker reaches it. |
| `mcp_config` | none | `--mcp-config`: a file on the worker with other MCP servers for turns. |
| `isolate` | on | Leave out the signed-in user's own settings, hooks and MCP servers (`--setting-sources project,local --strict-mcp-config`), so a turn reaches only Rook and what `mcp_config` gives it. Off: turns run with the user's whole Claude Code setup. |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes its name as `door` (`{{secret:turn-...}}`). The plugin hands that name
on as `ROOK_DOOR_TOKEN` in the process's environment and the hub puts the
token there on the way to the worker; the plugin never holds it. The turn's
`rook` MCP server comes from `~/.rook-plugins/agents-claude-code/rook-mcp.json`
on the worker, which names the variable (`Bearer ${ROOK_DOOR_TOKEN}`), never
the token. The journal keeps the name; the hub ends the token when the turn
does.

On the worker, everything lives in `~/.rook-plugins/agents-claude-code/`:
`work/`, where turns run unless the agent names a folder (Claude Code keys
its sessions by folder, so it is the same one every turn), and
`rook-mcp.json`. Install Claude Code on the worker and sign in once
(`claude`) before agents use it.

## A hub that ran the worker version

Before 0.6 this plugin ran on the worker, with its settings in that worker's
`worker.yaml`. After the hub is upgraded the hub's copy answers every turn,
whatever an older worker still announces. Until `worker` is set, the site's
chat says it is getting ready and a turn fails with "set
agents-claude-code.worker to the worker where the program is installed and
signed in". Once, on the hub:

1. Set `agents-claude-code.worker` to the worker that ran it (its name).
2. Copy the other settings from that worker's `worker.yaml`
   (`settings: agents-claude-code:`) to the hub's: `claude` (a full path or
   `~/...`), `door_url` (the hub's MCP door as that worker reaches it), and
   `permission_mode`, `allowed_tools`, `model`, `model_three`, `mcp_config`,
   `isolate` as they were set.
3. To keep an agent's Claude Code session, set that agent's `cwd` to the old
   work folder on that worker: `<the worker's data folder>/plugin-data/agents-claude-code/work`
   (e.g. `~/.rook-castling/worker/data/plugin-data/agents-claude-code/work`).
   Left as is, the agent's next turn starts a new session in
   `~/.rook-plugins/agents-claude-code/work`.
4. Take `agents-claude-code` out of that worker's `plugins:` when the worker
   is next upgraded.

As calls (an owner, or an agent acting for one; each is `worker: rook`, the
cap, the args, and a `why`), for a worker `cachyrig` whose `worker.yaml` had
`claude: ~/.local/bin/claude`, `door_url: https://<hub>/mcp` and
`allowed_tools: [mcp__rook]`:

```
setting.set  {"plugin": "agents-claude-code", "key": "worker", "value": "cachyrig"}
setting.set  {"plugin": "agents-claude-code", "key": "claude", "value": "~/.local/bin/claude"}
setting.set  {"plugin": "agents-claude-code", "key": "door_url", "value": "https://<hub>/mcp"}
setting.set  {"plugin": "agents-claude-code", "key": "allowed_tools", "value": ["mcp__rook"]}
agent.list   {"where": {"name": "house"}}
             -> [{"id": "<id>", "revision": <n>, ...}]
agent.update {"id": "<id>", "revision": <n>,
              "cwd": "~/.rook-castling/worker/data/plugin-data/agents-claude-code/work"}
```

Any other setting the worker had goes the same way (`"key": "permission_mode"`,
...). tests/test_core2_engine_upgrade.py makes these same calls.
