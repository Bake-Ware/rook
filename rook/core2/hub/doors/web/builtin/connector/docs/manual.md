# Connectors

Apps that reach the hub's MCP door through OAuth instead of a token you
paste: claude.ai's web and mobile connectors, and any other remote MCP
client. Each one is a **connector**: the app, the person who allowed it and
the agent it acts as. It does only what both that person and that agent may
do, and its calls are journaled under the agent for the person.

| Cap | What it does |
|---|---|
| `connector.list` | The connectors people allowed: the app's name, who allowed it, the agent, when it was made and last refreshed, and when it lapses. Owners see all; anyone else their own. |
| `connector.revoke` | `{id}`: end one now. Its tokens stop working at once; the app has to be allowed again. Your own, or any for an owner. |

The **Connectors** page (under More in the sidebar) lists them with a
Revoke button.

## Adding one

The hub needs `oauth:` in hub.yaml (its public addresses, behind TLS):

    oauth:
      issuer: https://hub.example          # this site
      resource: https://hub.example/mcp    # the MCP door

In claude.ai, add a custom connector with the MCP door's address. claude.ai
registers itself, then sends you to this site: sign in, choose the agent it
acts as, and Allow. Access tokens last an hour and are refreshed; a
connector not refreshed for 30 days lapses. Taking the person or the agent
out of the config ends it too, and closing the `mcp` door refuses it like
every agent.
