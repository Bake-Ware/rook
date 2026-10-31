# Codex backend

`agents-codex` lets agents think with the Codex CLI on this worker, under
whatever ChatGPT subscription `codex` is signed in with. It provides the
`agent.backend` contract, so the agents package uses it for every agent
whose backend is `codex`, and for `default` agents when it is the chosen
provider.

One call is one turn: `codex exec --json`, the prompt on stdin, the agent's
system prompt as Codex's developer instructions. The next turn continues the
same Codex session (`codex exec resume <id>`). The reply (Codex's last
message), the session id and token usage come back.

Settings:

| Setting | Default | What |
|---|---|---|
| `codex` | `codex` | The program, on PATH or a full path. |
| `model` | Codex's | System Two's model when the agent names none. |
| `model_three` | `gpt-6-luna` | System Three's model, for dream cycles: a light one. Blank: Codex's default. |
| `sandbox` | Codex's (`read-only`) | The sandbox for commands a turn runs: `read-only`, `workspace-write` or `danger-full-access`. |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as this worker reaches it. |
| `config` | none | Extra Codex config, one `key=value` per line, each passed as `-c` (other MCP servers, say). |
| `isolate` | on | Leave out the signed-in user's own `config.toml` and rules (`--ignore-user-config --ignore-rules`); sign-in still comes from `CODEX_HOME`. Off: turns run with the user's whole Codex setup. |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes it as `door`. This plugin adds the door as the turn's `rook` MCP
server (`door_url`), with the token in the turn's own environment
(`ROOK_DOOR_TOKEN`, `bearer_token_env_var`), never on the command line or on
disk, and lets Codex call its tools without asking: the hub decides what
each call may do. The hub ends the token when the turn does.

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`). Install Codex on the worker and sign in once (`codex login`)
before agents use it.
