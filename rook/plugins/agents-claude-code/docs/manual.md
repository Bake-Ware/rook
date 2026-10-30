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
| `model` | `opus` | System Two's model when the agent names none. Dream cycles ask for `haiku`. |
| `permission_mode` | Claude Code's | `--permission-mode` for turns. |
| `allowed_tools` | none | `--allowedTools`: tools a turn may use without asking. |
| `mcp_config` | none | `--mcp-config`: how the agent reaches Rook (the MCP door). |

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`). Install Claude Code on the worker and sign in once (`claude`)
before agents use it.
