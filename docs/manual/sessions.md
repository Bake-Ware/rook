---
id: sessions
title: Sessions: agents and terminals
order: 9
features:
  - features/sessions
  - plugin/worker/sessions
  - plugin/worker/session_mirror
  - plugin/worker/session_shim
  - plugin/worker/terminals
  - plugin/worker/work
  - plugin/worker/claude_history
  - plugin/worker/codex_history
  - plugin/worker/hermes
  - plugin/worker/wake
  - settings/agent
since: 0.1.0
deprecated: false
---
# Sessions: agents and terminals

**Sessions** lists every AI agent and terminal session on every machine: Claude
Code, Codex, Hermes and ordinary shells, whether Rook started them or you
started them yourself in your own terminal.

## What you can do

- **Open** a session to see its live terminal, a live view (Claude Code with
  the Rook mod) or its transcript.
- **Send** it a message, as if you typed it.
- **Stop** it.
- **Resume** a session that ended, in a new Rook terminal on the same machine.
- **Start** a new session on any machine: pick the machine, the program and the
  folder.

A session started for a task claims that task and links to it. When the
session ends, the task gets a note asking for a handoff
([[manual-work]]).

## Rook terminals

A Rook terminal is a terminal that lives inside the worker, so it keeps running
when you close the browser and you can watch it from anywhere. Agents open them
with their console tools to run long jobs.

Run `sessions.shim.install` on a machine to make `claude` and `codex` typed in
your own terminal run inside a Rook terminal too, so they show up here.

## Agent history

On machines where Claude Code or Codex has been used, Rook can search and read
past conversations (`claude-history.*`, `codex-history.*`), and `rook sessions`
browses them offline in a terminal. The `hermes.*` capabilities talk to a
Hermes agent running on a machine: chat with it, run one task, look at its
skills and memory.

## Waking an agent

A machine can be set up to start an agent when someone mentions it in a chat
room. Set the **Wake (agent.wake)** command on that machine's Settings page,
for example `claude -p {prompt_file}`. The woken agent gets the room's
conversation and answers there.
