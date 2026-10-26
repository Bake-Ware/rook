---
title: Tasks, claims and the deck
pitch: See what work is on, claim it, finish it with proof, or stop it cleanly
tools: [rook_task, rook_project, rook_concept]
caps: [task.]
refs: [references/coordination.md, references/examples.md]
notes:
  - rook_task: Claim before working; finish with an evidence link and state done with attrs.outcome, or release with data.handoff. Writes need a fresh request_id (the same one when retrying).
  - rook_task.deck: Narrow the deck (data.states, done_days, project_fields, limit) and page until next_offset is null; the unfiltered deck is tens of KB.
  - rook_task.claim: Claimed. If also_claimed_by lists someone active in the last 2 hours, coordinate before touching anything. To finish: link evidence, then update state done with attrs.outcome. Stopping early: release with data.handoff {goal, state, next_steps}.
  - rook_task.create: Search first (action=search) so the trail doesn't split. The title says the result, not the activity; attrs.criteria says what done means.
  - rook_task.update: Updates need the current revision; on "Record changed", get and re-apply. done needs attrs.outcome and an evidence link first.
---
# Tasks, claims and the deck

**Use it when** work will take more than a few calls, or another agent may need to know it is happening.

## How

- **See what's on**: `rook_task(action="deck", data={"states": ["in_progress", "blocked"], "done_days": 1})`, or `id=<project>`. Add `project_fields: ["id","title","state"]` and page with `limit`/`offset` until `next_offset` is null.
- **No task?** `rook_task(action="search", query=…)` first, then `create` under a project with `attrs.criteria`.
- **Claim before working**: `rook_task(action="claim", id=…, request_id=…)`. Your later calls, consoles and handoffs link to it. Work on one claimed task at a time: auto-links go to the claim you started most recently.
- **Finish with proof**: `rook_task(action="link", id=…, data={"kind": "commit", "ref": "repo@sha"}, request_id=…)`, then `rook_task(action="update", id=…, data={"revision": N, "patch": {"state": "done", "attrs": {"outcome": "…"}}}, request_id=…)`. A commit made through `rook_call` whose message contains `rook: <task slug>` is linked for you.
- **Progress without a state change**: `rook_task(action="note", id=…, data={"text": …})`.

## Hygiene

- **One owner.** Claims never block anyone, so this is discipline. A second active claimant is a conflict to resolve (chat, or the project's huddle), not a co-owner.
- **Say it in the huddle** when you start, stall and finish (topic `person`).
- **An outcome says** what changed, where, and how you verified it ("merged as abc123; tests pass; service restarted and answering"). Good evidence is traceable: a commit, a journal id, a console room, a file on a worker, a PR URL.
- **Never just leave.** Stopping unfinished: `release` with `data.handoff {goal, state, next_steps}` (the task goes `paused`), `update` to `blocked` with `attrs.blocked_reason` and the same handoff, or `cancelled` with an outcome saying why. `closed` is only for "a person said so" and needs their quote in `data.closed_by`.
- **Close it when the work lands**: PR merged, deploy verified, done then, not later. If you orchestrate subagents, each claims its own task, or you finish them yourself.
- **Writes**: a fresh `request_id` per distinct write, the same one when retrying it.

## Mistakes

| Symptom | Fix |
|---|---|
| Task `in_progress`, claimant gone, no handoff | Read its links, latest handoff and journal; finish it, or release the stale claim (allowed after 2 h idle) with a handoff saying where it really stands |
| `done` refused | `link` evidence first, then `update` with `attrs.outcome` |
| Two claims, links on the wrong task | Finish or release the current task before switching, or name the task explicitly |
