---
title: Hard stops, holds and change requests
pitch: What asks the person first, what to do with a held or denied call, and how risky changes go through a change request
tools: [rook_trust]
caps: [change., trust.]
refs: [references/admin.md, references/troubleshooting.md]
notes:
  - held: Held: nothing ran. Tell the user the hold's name; repeat the same call (same cap, worker and args) once they answer on their phone card, by voice or on the dashboard. A denial is final. Never route around a hold with another tool, worker, job, console or ssh.
---
# Hard stops, holds and change requests

**Use it when** a call might change a sacred machine, erase data for good, reveal a secret's value or send as the person; when a reply says `held` or `denied`; when a change could hurt.

## The four hard stops

The hub asks the person before an agent:

1. changes anything on a **sacred machine** (`rook_trust(action="summary")` lists them; reading there is fine). Sacred means changed only at the explicit request of its owner, or of someone with write permission on that worker; never on your own initiative;
2. **erases data for good**: wiping disks, dropping databases, deleting a home, top-level or data directory, `memory.forget`, deleting secrets;
3. **reveals a secret's value** (`rook_secret` get, a `{{secret:…}}` in a message);
4. **sends a message as the person** to someone else.

Everything else just goes and is journaled.

## Held and denied

- **Say why.** Pass `why="<one plain sentence>"` on `rook_call` and claim the task first; the person sees *Title / For (your task) / Who / I want to / Why / Risk*.
- A held call waits about 45 s; then the reply is `{"ok": false, "held": true, "hold": "<id>", …}` and nothing ran. Tell the user the hold's name and repeat the **same** call once they answer. Neither the ask nor their yes expires; a yes covers that one call.
- A `denied:` reply is final: don't retry it, ask.
- **Never work around a hold**: no other tool, worker, job, console or ssh for the same action.

## Change requests

A change that could hurt (a deploy, a config or database change) goes as a change request: `rook_call(worker="rook", cap="change.submit", …)`; `caps.describe` with prefix `change.` has the args. Standard ones (routine deploys, reversible config or database changes) the hub checks, runs, tests and rolls back by itself; risky or permanent ones get one huddle round, then one card to the person. Reads and routine runs need none.

## Hygiene

- **Keep a way back** before you change anything: snapshot, backup, the old release.
- **Never add or tighten** guardrails, holds or trust entries, and never edit policy, approval or security settings: only the person does, and the hub refuses agents. Reviewing someone's change, propose a safer way; don't block it.
- Changes to the trust map happen only when the person asks for them in conversation.
