# OpenCode backend

`agents-opencode` lets agents think with the OpenCode CLI on this worker. It
is the first-run installer's "none" path: OpenCode's free `opencode/*`
models work with no account, so a new hub's house agent can talk right away.
Any model OpenCode is set up for works too (`opencode models` lists them).
It provides the `agent.backend` contract, so the agents package uses it for
every agent whose backend is `agents-opencode.turn`, and for `default`
agents when it is the chosen provider.

One call is one turn: `opencode run --format json`, the prompt on stdin. The
next turn continues the same OpenCode session (`--session <id>`). The reply
(the text of the turn's last step), the session id and token usage come back.

OpenCode's free models refuse a custom system prompt, so the agent's
instructions go at the head of a new session's first message, inside
`<instructions>`. When the model fails (the free tier's usage limit, say),
OpenCode would retry quietly until the turn times out; this plugin reads its
error log instead and ends the turn at once with that error.

Settings:

| Setting | Default | What |
|---|---|---|
| `opencode` | `opencode` | The program, on PATH or a full path (the installer puts it in `~/.opencode/bin`). |
| `model` | OpenCode's | System Two's model, `provider/model`. |
| `model_three` | `model` | System Three's model, for dream cycles. |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as this worker reaches it. |
| `own_tools` | off | Let turns use OpenCode's own shell, file and web tools on this worker. Off: Rook's caps only, so every action goes through the hub and its journal. |
| `isolate` | on | Leave out the user's own OpenCode config (MCP servers, agents, plugins, instructions); only its model providers are kept. |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes it as `door`. This plugin writes a config with the door as the turn's
`rook` MCP server, its bearer token read from the turn's own environment
(`ROOK_DOOR_TOKEN`), never on the command line or on disk. Tools run without
prompts: the hub decides what each call may do.

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`).
