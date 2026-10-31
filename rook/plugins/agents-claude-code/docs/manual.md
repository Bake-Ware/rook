# Claude Code backend

`agents-claude-code` lets agents think with the Claude Code CLI on this
worker, under whatever subscription `claude` is signed in with. It provides
the `agent.backend` contract, so the agents package uses it for every agent
whose backend is `default` (when it is the chosen provider) or
`claude-code`.

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
| `mcp_config` | none | `--mcp-config`: how the agent reaches Rook (the MCP door). |
| `isolate` | on | Leave out the signed-in user's own settings, hooks and MCP servers (`--setting-sources project,local --strict-mcp-config`), so a turn reaches only what `mcp_config` gives it. Off: turns run with the user's whole Claude Code setup. |

For `mcp_config`, point Claude Code at the hub's MCP door (`/mcp`) with a
token for the agent acting for a person
(`python -m rook.core2.hub --config hub.yaml token --person <you> --agent <agent>`).
Until agents are hub identities of their own, every turn on this worker
shares that one token.

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`). Install Claude Code on the worker and sign in once (`claude`)
before agents use it.
