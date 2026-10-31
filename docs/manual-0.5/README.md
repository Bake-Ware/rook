# The Rook 0.5 manual

Rook gives an agent more access where it matters: your machines, your
devices and your accounts, reached through one hub that decides what may
run and keeps a journal of everything that did. This is the manual for
Rook 0.5 ("Castling"). The 0.4 manual is in `docs/manual/` and stays as it
is until 0.5 ships.

0.5 is a clean cut from 0.4: no 0.4 machine talks to a 0.5 one, and a band
moves over all at once.

## For people

1. [How Rook fits together](concepts.md): bands, the hub, workers, caps, people and agents.
2. [Install and first run](install.md): a hub, a relay, your first sign-in.
3. [Adding machines](enrollment.md): enrolling a worker, joining another band.
4. [People and presets](people.md): owners, adults, children, guests, agents and tokens.
5. [Workers and appliances](workers.md): what a worker runs, ceilings, sacred machines, devices without a worker.
6. [Plugins](plugins.md): installing, updating and rolling back, and every plugin that ships.
7. [Settings and secrets](settings.md): where a setting lives and how secrets travel.
8. [Caps](caps.md): what a cap says about itself: risk, scope, effects.
9. [Holds, hard stops and the why](holds.md): what waits for you and how to answer.
10. [The journal](journal.md): what was done, by whom, and why.
11. [The hub site](dashboard.md): the dashboard, pages, and the house agent's dock.
12. [Agents and personas](agents.md): the ready-made agents, talking to them, making your own.
13. [The workbench](workbench.md): tasks, projects, knowledge, concepts and whiteboards.
14. [Jobs](jobs.md): things that run on a schedule or an event.
15. [Streams](streams.md): terminals, processes and other live things.
16. [Security plugins](security.md): the gates that watch every call.

## For agents

- [MCP for agents](mcp.md): connecting, the seven tools, and how to work on a band.
- [The workbench](workbench.md): claiming tasks, whiteboards, knowledge with sources.

## For plugin authors

- [Writing a plugin](authoring.md): the manifest, config caps (Deluge), appliances (PiKVM), `run:` scripts, testing.
- [Caps](caps.md) and [Holds](holds.md): choosing a cap's risk, scope and effects.

## Reference

- [Reference](reference.md): `hub.yaml`, `worker.yaml`, the command line, the HTTP door.
- The protocol: `docs/spec/core-v2.md`. The manifest schema: `conformance/manifest/manifest.schema.json`.
- Running a real 0.5 band on localhost for testing: `docs/castling-e2e.md`.

## Writing these pages

This section is for contributors; it is not part of the manual.

- One short, task-oriented page per topic, plain language first, commands
  after. Say only what the `castling` branch does; when something is
  designed but not built, say so in one line rather than describing it.
- Each page starts with a `# Title` and is listed above. Link pages with
  relative Markdown links (`[Caps](caps.md)`), and link repo files the same
  way (`../../rook/plugins/agents/docs/manual.md`).
- What belongs to one plugin goes in that plugin's `docs/manual.md`, named
  in its manifest as `docs: { manual: docs/manual.md }`; these pages link
  to it rather than repeat it.
- A YAML block that starts with `manifest: 1` is a complete manifest and
  must validate.
- `tests/test_manual05.py` (in pytest, so it blocks a merge) checks all of
  the above, plus that every plugin has a manual,
  every plugin is in the [plugin list](plugins.md#every-plugin), and every
  config key, command, HTTP route, MCP tool and preset is in the manual.
  `python tools/manual05_check.py` runs the same checks and lists every
  problem.
