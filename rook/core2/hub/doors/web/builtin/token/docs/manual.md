# Tokens

Door tokens let an agent reach the hub (the MCP door, or HTTP `/call`).
A token always acts for a person: each call gets only what both that
person and the agent may.

| Cap | What it does |
|---|---|
| `token.create` | `{agent, label?}`: a token for `agent` (yourself or another agent) acting for the person you call for. Returned once; only its hash is kept in `<data>/tokens.json`. A new agent name gets the minting agent's rights (every grant when a person mints it), which the person's rights narrow. The reply has `mcp_url` (when the MCP door has its public `oauth:` address), a ready `claude mcp add` command, and `connector` `{url, client_id, client_secret}` for a claude.ai custom connector that needs no password. |
| `token.list` | The tokens (never the tokens themselves): id, agent, person, label, created. Owners see all, others their own. |
| `token.revoke` | `{id}`: the token stops working at once. The first owner's own token can't be revoked here, so an owner can't lock themselves out. |

An agent setting itself up calls `token.create` with its own name and puts
the token in its MCP client. The **Tokens** page (sidebar, Hub) does the
same for a person and lists and revokes them.
