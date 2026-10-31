# How Rook fits together

## The pieces

- **Band**: your machines, joined by one shared key over a relay. Everything
  on a band is encrypted with that key. A band has one hub.
- **Hub**: the band's control plane. People and agents come in through its
  doors (the dashboard, MCP for agents, the HTTP door, jobs, chat bridges),
  and every door ends at one step, **dispatch**, which decides whether a
  call may run. The hub appears on its band as a worker named `rook`.
- **Worker**: Rook running on a machine (a laptop, a server, a phone). It
  announces what it can do and **enforces**: it runs a call only when the
  call carries a ticket the hub signed for exactly that call.
- **Appliance**: a device that runs no worker (a TV, a PiKVM, a phone over
  adb). A worker that can reach it carries its calls.
- **Cap** (capability): one thing a worker can do, like `shell.exec` or
  `torrent.pause`. Every cap says up front what it is: its risk, its reach
  and its effects ([Caps](caps.md)).
- **Plugin**: a folder with a `plugin.yaml` that declares caps and how to
  run them. Almost everything is a plugin, shell included ([Plugins](plugins.md)).
- **People and agents**: a person has an account on the hub; an agent (Claude
  Code over MCP, a persona on the hub) has its own identity. An agent working
  for a person can do only what both may ([People and presets](people.md)).
- **Journal**: the hub's record of every call, kept as tamper-evident chains
  ([The journal](journal.md)).

## What happens on a call

```
 agent or person ──door──▶ hub: who is it? why? rights? gates? holds?
                             │   signs one ticket for this exact call
                             ▼
                  relay (sees only encrypted frames)
                             ▼
                  worker: checks the ticket and its own ceiling, runs the cap
```

1. The caller names a worker, a cap, its arguments and a **why**: one plain
   sentence saying why. No why, no call.
2. The hub checks what every layer allows (the person, the agent, the
   session, the worker's ceiling), asks the security gates, and **holds**
   the call for a person when it must ([Holds](holds.md)).
3. It signs a ticket bound to that call: the cap, the exact arguments, the
   worker, who it is for, and the why. A ticket lives about a minute and
   works once.
4. The worker checks the ticket, runs the cap as a normal user (never as
   administrator unless the hub grants an elevation for that call) and
   replies.
5. The hub writes one journal entry, whatever happened.

## Why it is built this way

- **The hub decides, the worker enforces.** Someone holding the band key can
  read the band's traffic, but can't make a worker run anything without the
  hub's signature.
- **Protection stays out of your way.** Most calls just run. You are asked
  only for the few that need a person: erasing data for good, revealing a
  secret, sending as you, spending money, or changing a machine you marked
  sacred.
- **Agents talk to caps, not shells.** A plugin turns a ten-step shell
  struggle into one clear call that says what it does, so both the hub and
  you can judge it.
