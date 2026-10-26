---
title: Administering and installing Rook
pitch: Config pushes, OTA updates, plugins, hub services, tokens and bands; installing a hub, worker, phone app or MCP client
tools: [rook_config_get, rook_config_apply, rook_install_claude_code]
caps: [worker., updates., plugin.]
refs: [references/admin.md, references/install.md]
notes:
  - rook_config_apply: Config pushes go through here (commit-confirmed, it reverts by itself unless confirmed), never hand edits over shell.exec. Re-banding and key changes need the user's go-ahead first.
---
# Administering and installing Rook

**Use it when** you change how a worker or the hub is configured, update software, manage plugins, or install Rook somewhere new. Read `references/admin.md` or `references/install.md` (or ask this topic with `"detail": true`) before you start.

## Hygiene

- **Config pushes** go through `rook_config_apply` (commit-confirmed, auto-reverts), not hand edits over `shell.exec`.
- **Mutating `worker.*` calls** (update, reconfigure, restart, config apply, deauth, hold) can take a machine off the band: confirm with the user first.
- **Ask first** before re-banding, `worker.deauth`, key changes, and anything that spends money.
- **Agents cannot** mint pairing codes (the dashboard's Tokens page) or sign updates and deauths (the controller key): ask the user.
- **The band key is root on every worker.** Never print it, and never put it in args, chat, knowledge, handoffs or a command line.
