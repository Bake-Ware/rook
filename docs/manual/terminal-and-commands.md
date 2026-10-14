---
id: terminal-and-commands
title: The terminal dashboard and commands
order: 23
features:
  - features/rook-band-terminal-control-panel
  - cli/band
  - cli/worker
  - cli/sessions
  - cli/history
  - cli/dashboard
since: 0.1.0
deprecated: false
---
# The terminal dashboard and commands

## The terminal dashboard

Type `rook` in a terminal on any desktop machine with a worker. A full-screen
dashboard opens: machines on the left, the selected machine's details on the
right, and your chats below.

- Use the arrow keys to move into a machine's capabilities and run one.
- Switch plugins, add custom commands, message machines and remove machines.
- Press `J` for the hub's jobs: their runs, each step's output, run one now, or
  switch one on or off.

It asks for your dashboard name and password the first time and remembers them
in `~/.config/rook/band.conf`. Everything it does goes through the hub, under
your login, and is recorded. Closing it leaves the worker running.

To install only the terminal dashboard on a machine without a worker:

```sh
curl -fsSL https://<your-hub>/install | bash -s -- cli
```

## Commands

`rook --help` lists them:

- `rook` or `rook band` (also `rook tui`): the terminal dashboard.
- `rook worker`: run the worker by hand (the installers set it up as a
  service). `rook worker --help` lists its options.
- `rook sessions` (also `rook history`): browse Claude Code history on this
  machine, offline.
- `rook skill`: install Rook's instructions for AI agents
  ([[manual-agents-and-mcp]]).
- `rook hub`: run and manage the hub ([[manual-running-a-hub]]).
- `rook dashboard` and `rook mcp`: the two halves of the hub as separate
  programs, kept for older setups.
