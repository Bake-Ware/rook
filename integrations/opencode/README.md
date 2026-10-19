# Rook for OpenCode

An OpenCode integration for using Rook's band tools and workflows from the OpenCode TUI. It provides slash commands for the worker roster, task deck, and safe deck grooming. Rook's MCP tools are also available directly to the agent.

OpenCode does not provide the persistent custom side pane used by the Claude Code mod, so this integration uses native commands instead. It does not mirror OpenCode session transcripts or move a live conversation into a Rook terminal.

## Connect OpenCode to Rook

Add a remote MCP server to your OpenCode config (`opencode.json` or `~/.config/opencode/opencode.json`). Use the Rook MCP endpoint and bearer token for your deployment:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "rook": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:ROOK_MCP_STATIC_TOKEN}"
      }
    }
  }
}
```

Set `ROOK_MCP_STATIC_TOKEN` in the environment before starting OpenCode. Replace the URL with the MCP endpoint for your hub. For a local Rook hub, see the main README's MCP setup. Do not commit the token to project configuration.

## Install the commands

Copy the `commands/` directory into either the project-level `.opencode/commands/` or the user-level `~/.config/opencode/commands/` directory. The available commands are:

- `/rook-bands`: ask the agent to retrieve and summarize the worker roster and band status.
- `/rook-deck`: retrieve open tasks and recent handoffs.
- `/rook-groom`: validate a deck snapshot against current Rook data and suggest safe task/handoff hygiene updates; ask before any other write or worker action.

The OpenCode agent needs access to the `rook` MCP server. If you configured a different MCP server name, replace `rook` in the command prompts with that name. These command templates do not provide Claude's interactive pane or automatic settings controls.

## Development

Edit the Markdown command files and copy them to an OpenCode command directory to test. The plugin and command formats are documented by OpenCode; this integration intentionally has no runtime dependencies and no installation/version-check MCP tool.
