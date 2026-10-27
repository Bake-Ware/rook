---
title: Handoffs
pitch: Leave or pick up the state of unfinished work so the next agent can continue without asking
tools: [rook_handoff_save, rook_handoff_list, rook_handoff_get]
refs: [references/coordination.md, references/examples.md]
notes:
  - rook_handoff_save: State, not a transcript: goal, state (done and verified / assumed / broken), decisions (who decided), next_steps (the first runnable as written), artifacts. Pass thread_id to update a thread; status=closed when finished.
  - rook_handoff_get: Heed freshness: STALE (over 3 days) or SUPERSEDED means verify before acting on it.
---
# Handoffs

**Use it when** you pass work to another agent or person with context your whiteboard can't show. A claimed task's whiteboard (topic `tasks`) is saved as its handoff on its own when you release it, go idle or the session ends, so routine stops need no handoff. Resuming: `rook_handoff_list(limit=5)`, then `rook_handoff_get(thread_id)`.

## What goes in

- `goal`: the outcome in one sentence.
- `state`: what is done and verified, what is assumed, what is broken right now.
- `decisions`: who decided ("user chose X", "proposed, not confirmed: Y").
- `next_steps`: ordered and concrete; the first one runnable as written (worker, path, command).
- `artifacts`: branches and commits, `worker:path`, console room ids, journal ids, PR URLs, knowledge slugs. Secrets by vault name only.

## Hygiene

- **One thread per piece of work.** Pass `thread_id` to update it rather than starting a new thread; never save the same handoff to several new threads.
- **Close finished or shelved threads** (`status="closed"` with `thread_id`) so they leave everyone's list.
- **It links to your claimed task** by itself, or pass `task=<id>`.
- **Heed freshness** when you pick one up: `STALE` or `SUPERSEDED` means check the real state first.
