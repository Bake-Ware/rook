# OpenCode backend

`agents-opencode` lets agents think with the OpenCode CLI on one of the
band's workers. It
is the first-run installer's "none" path: OpenCode's free `opencode/*`
models work with no account, so a new hub's house agent can talk right away.
Any model OpenCode is set up for works too (`opencode models` lists them).
It provides the `agent.backend` contract, so the agents package uses it for
every agent whose backend is `agents-opencode.turn`, and for `default`
agents when it is the chosen provider.

The plugin runs on the hub; the CLI runs on the worker its `worker` setting
names, as a process there (`shell.open`) whose output the plugin reads as it
comes. One call is one turn: `opencode run --format json`, the prompt on
stdin. The
next turn continues the same OpenCode session (`--session <id>`). The reply
(the text of the turn's last step), the session id and token usage come back.

`agents-opencode.models` answers the models its `model` setting and an
agent's model take: `opencode models` on the worker (`provider/model`, one a
line), and `default`, the `model` setting (blank: OpenCode's own).

OpenCode's free models refuse a custom system prompt, so the agent's
instructions go at the head of a new session's first message, inside
`<instructions>`. When the model fails (the free tier's usage limit, say),
OpenCode would retry quietly until the turn times out; this plugin reads its
error log instead and ends the turn at once with that error.

Settings (the hub's: Settings page, or `setting.set`):

| Setting | Default | What |
|---|---|---|
| `worker` | none | The worker where OpenCode is installed. Turns fail until it is set. |
| `opencode` | `opencode` | The program on that worker, on its PATH or a full path (the installer puts it in `~/.opencode/bin`; `~` is its home). |
| `model` | OpenCode's | System Two's model, `provider/model`. |
| `model_three` | `model` | System Three's model, for dream cycles. |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as the worker reaches it. |
| `own_tools` | on | Turns use OpenCode's own shell, file and web tools on the worker as well as Rook's caps. Off: Rook's caps only. |
| `isolate` | on | Leave out the user's own OpenCode config on the worker (MCP servers, agents, plugins, instructions); only its model providers are kept (read from it with `file.read`). |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes its name as `door` (`{{secret:turn-...}}`). This plugin writes a
config with the door as the turn's `rook` MCP server, its bearer token read
from `ROOK_DOOR_TOKEN`, and hands the name on as that variable in the
process's environment, where the hub puts the token on the way to the
worker: never on the command line, on disk or in the journal. Tools run
without prompts: the hub decides what each call may do.

On the worker, everything lives in `~/.rook-plugins/agents-opencode/`:
`work/`, where turns run unless the agent names a folder (the same one every
turn), and `config/opencode/opencode.json`, written each turn (with
`isolate`, `config/` is OpenCode's whole config folder). A hub that ran this
plugin on a worker (before 0.6): set `worker` and copy the other settings
from that worker's `worker.yaml` to the hub's, as in the Claude Code
backend's manual.
