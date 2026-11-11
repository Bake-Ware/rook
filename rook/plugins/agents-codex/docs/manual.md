# Codex backend

`agents-codex` lets agents think with the Codex CLI on one of the band's
workers, under whatever ChatGPT subscription `codex` is signed in with there. It provides the
`agent.backend` contract, so the agents package uses it for every agent
whose backend is `codex`, and for `default` agents when it is the chosen
provider.

The plugin runs on the hub; the CLI runs on the worker its `worker` setting
names, as a process there (`shell.open`), ended when a turn runs past its
timeout or is cancelled. One call is one turn: `codex exec --json`, the
prompt on stdin, the agent's
system prompt as Codex's developer instructions. The next turn continues the
same Codex session (`codex exec resume <id>`). The reply (Codex's last
message), the session id and token usage come back.

`agents-codex.models` answers the models its `model` setting and an agent's
model take: the ones `codex debug models` on the worker shows in Codex's own
picker, and `default`, the `model` setting (blank: Codex's own). Any other
model id Codex knows works too.

Settings (the hub's: Settings page, or `setting.set`):

| Setting | Default | What |
|---|---|---|
| `worker` | none | The worker where Codex is installed and signed in. Turns fail until it is set. |
| `codex` | `codex` | The program on that worker, on its PATH or a full path (`~` is its home). |
| `model` | Codex's | System Two's model when the agent names none. |
| `model_three` | `gpt-6-luna` | System Three's model, for dream cycles: a light one. Blank: Codex's default. |
| `sandbox` | Codex's (`read-only`) | The sandbox for commands a turn runs: `read-only`, `workspace-write` or `danger-full-access`. |
| `door_url` | `http://127.0.0.1:8741/mcp` | The hub's MCP door as the worker reaches it. |
| `config` | none | Extra Codex config, one `key=value` per line, each passed as `-c` (other MCP servers, say). |
| `isolate` | on | Leave out the signed-in user's own `config.toml` and rules (`--ignore-user-config --ignore-rules`); sign-in still comes from `CODEX_HOME`. Off: turns run with the user's whole Codex setup. |

Each turn reaches Rook with its own token: the agents runtime gets one from
the hub for that turn (the agent, for the person behind the message) and
passes its name as `door` (`{{secret:turn-...}}`). This plugin adds the door
as the turn's `rook` MCP server (`door_url`), its bearer token read from
`ROOK_DOOR_TOKEN` (`bearer_token_env_var`), and hands the name on as that
variable in the process's environment, where the hub puts the token on the
way to the worker: never on the command line, on disk or in the journal. It
lets Codex call Rook's tools without asking: the hub decides what each call
may do. The hub ends the token when the turn does.

Turns run in the folder the agent names, else in
`~/.rook-plugins/agents-codex/work` on the worker (the same one every turn).
Install Codex on the worker and sign in once (`codex login`) before agents
use it. A hub that ran this plugin on a worker (before 0.6): set `worker`
and copy the other settings from that worker's `worker.yaml` to the hub's,
as in the Claude Code backend's manual.
