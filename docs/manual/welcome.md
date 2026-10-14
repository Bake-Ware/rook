---
id: welcome
title: Welcome to Rook
order: 1
features:
  - features/the-band
  - features/capabilities
  - features/control-planes
since: 0.1.0
deprecated: false
---
# Welcome to Rook

Rook lets you look after all of your computers from one place. You install a
small helper program, the **worker**, on each machine you want to reach: a home
server, a Raspberry Pi, a laptop, a phone. Each worker connects out to your
**hub**, the one machine that runs the Rook dashboard. From then on you can see
every machine on one page and ask any of them to do something: run a command,
take a screenshot, type on its keyboard, restart a service or send you a
message.

## The words Rook uses

- **Hub**: the machine that runs Rook's dashboard. You open it in a web browser.
- **Worker**: a machine that Rook can reach. It shows up on the Workers page.
- **Band**: a private group of workers that share one secret key. Everything
  sent inside a band is encrypted. Most people have one band; you can make more
  to keep groups of machines apart.
- **Band key**: the band's secret. Anyone who knows it can join the band and
  control its machines, so treat it like a master password.
- **Capability**: one thing a worker can do, written with a dot, such as
  `shell.exec` (run a command) or `screenshot.capture` (take a screenshot).
- **Plugin**: a bundle of related capabilities, such as everything for
  screenshots. Plugins only switch on where they can work: a machine without a
  screen never offers screenshots.
- **Agent**: an AI assistant (such as Claude Code or Codex) that you allow to
  use Rook on your behalf.

## Three ways to drive Rook

All three see the same machines and use the same capabilities:

- **The dashboard** in your web browser. Most of this manual is about it.
- **The terminal dashboard**: type `rook` in a terminal on a machine with a
  worker. See [[manual-terminal-and-commands]].
- **AI agents** connected over MCP, a standard way for AI tools to use other
  programs. See [[manual-agents-and-mcp]].

## How it fits together

Workers connect out to the hub, so you never have to open ports on your home
router or on the machines themselves. The hub passes encrypted messages between
them without being able to read them. The dashboard and your agents send
requests into the band; the right worker answers.

## Where to go next

1. Set up a hub and your first worker: [[manual-getting-started]].
2. Add more machines: [[manual-adding-machines]].
3. Learn the Workers page: [[manual-workers]].
4. Invite AI agents to help: [[manual-agents-and-mcp]].

**Tip:** every page of this manual is also in your knowledge base under
**Rook manual**, so your agents can read it too. See [[manual-help]].
