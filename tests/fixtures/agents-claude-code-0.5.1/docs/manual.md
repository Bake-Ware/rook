# Claude Code backend

`agents-claude-code` lets agents think with the Claude Code CLI on this
worker, under whatever subscription `claude` is signed in with. It provides
the `agent.backend` contract, so the agents package uses it for every agent
whose backend is `default` (when it is the chosen provider) or
`agents-claude-code.turn`.

One call is one turn: `claude -p --output-format json`, the prompt on stdin,
`--resume` with the session id from the last turn, the agent's system prompt
appended with `--append-system-prompt`. The reply, the session id and token
usage come back.

Settings:

| Setting | Default | What |
|---|---|---|
| `claude` | `claude` | The program, on PATH or a full path. |
| `model` | `opus` | System Two's model when the agent names none. |
| `model_three` | `haiku` | System Three's model, for dream cycles. |
| `permission_mode` | Claude Code's | `--permission-mode` for turns. |
| `allowed_tools` | none | `--allowedTools`: tools a turn may use without asking. |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as this worker reaches it. |
| `mcp_config` | none | `--mcp-config`: other MCP servers for turns. |
| `isolate` | on | Leave out the signed-in user's own settings, hooks and MCP servers (`--setting-sources project,local --strict-mcp-config`), so a turn reaches only Rook and what `mcp_config` gives it. Off: turns run with the user's whole Claude Code setup. |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes it as `door`. This plugin writes it into a private MCP config (`rook`
server, `door_url`) in the call's temp folder, never on the command line,
and the hub ends the token when the turn does. No token to set up.

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`). Install Claude Code on the worker and sign in once (`claude`)
before agents use it.
