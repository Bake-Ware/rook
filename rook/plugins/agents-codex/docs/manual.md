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
| `model_three` | Codex's | System Three's model, for dream cycles; a fast one (e.g. `gpt-6-luna`) is cheaper. |
| `sandbox` | Codex's (`read-only`) | The sandbox for commands a turn runs: `read-only`, `workspace-write` or `danger-full-access`. |
| `config` | none | Extra Codex config, one `key=value` per line, each passed as `-c`. |
| `isolate` | on | Leave out the signed-in user's own `config.toml` and rules (`--ignore-user-config --ignore-rules`); sign-in still comes from `CODEX_HOME`. Off: turns run with the user's whole Codex setup. |

To let the agent reach the band, point Codex at the hub's MCP door in
`config`:

```
mcp_servers.rook.url="http://<hub>:8751/mcp"
mcp_servers.rook.bearer_token_env_var="ROOK_AGENT_TOKEN"
mcp_servers.rook.default_tools_approval_mode="approve"
```

with `ROOK_AGENT_TOKEN` in the worker's environment holding a token for the
agent acting for a person
(`python -m rook.core2.hub --config hub.yaml token --person <you> --agent <agent>`).
The last line lets Codex make the calls without asking: the hub decides what
each call may do.

Turns run in the folder the agent names, else in this plugin's data folder
(`work/`). Install Codex on the worker and sign in once (`codex login`)
before agents use it.
