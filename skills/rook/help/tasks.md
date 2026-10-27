---
title: Tasks, claims and the deck
pitch: See what work is on, claim it, finish it with proof, or stop it cleanly
tools: [rook_task, rook_project, rook_concept]
caps: [task.]
refs: [references/coordination.md, references/examples.md]
notes:
  - rook_task: Claim before working; finish with an evidence link and state done with attrs.outcome, or release with data.handoff. Writes need a fresh request_id (the same one when retrying).
  - rook_task.deck: Narrow the deck (data.states, done_days, project_fields, limit) and page until next_offset is null; the unfiltered deck is tens of KB.
  - rook_task.claim: Claimed. If also_claimed_by lists someone active in the last 2 hours, coordinate before touching anything. Your calls now fill this claim's whiteboard; jot decisions and open items on it. To finish: link evidence, then update state done with attrs.outcome. Stopping early: release (the whiteboard becomes the handoff, or pass data.handoff).
  - rook_task.jot: On your whiteboard. Jot what calls can't show: a decision and who made it, an open item, done (an open item's seq or text). The why on each call already records the work.
  - rook_task.create: Search first (action=search) so the trail doesn't split. The title says the result, not the activity; attrs.criteria says what done means.
  - rook_task.update: Updates need the current revision; on "Record changed", get and re-apply. done needs attrs.outcome and an evidence link first.
---
# Tasks, claims and the deck

**Use it when** work will take more than a few calls, or another agent may need to know it is happening.

## How

- **See what's on**: `rook_task(action="deck", data={"states": ["in_progress", "blocked"], "done_days": 1})`, or `id=<project>`. Add `project_fields: ["id","title","state"]` and page with `limit`/`offset` until `next_offset` is null.
- **No task?** `rook_task(action="search", query=…)` first, then `create` under a project with `attrs.criteria`.
- **Claim before working**: `rook_task(action="claim", id=…, request_id=…)`. Your later calls, consoles and handoffs link to it: the claim this MCP session made, else your newest. Work on one claimed task per session.
- **The whiteboard** is the claim's living page, kept by the hub: each call's `why`, worker and result (repeats collapse), consoles, handoffs, secrets used by name, commits. `rook_task(action="whiteboard")` reads yours (`id=` another task's). Add what calls can't show: `rook_task(action="jot", data={"goal": …, "decision": …, "open": …, "done": <seq or text>, "note": …}, request_id=…)`. It rides the brief when your context was lost, and is saved as the task's handoff when you release, go idle or end the session with work since the last one.
- **Finish with proof**: `rook_task(action="link", id=…, data={"kind": "commit", "ref": "repo@sha"}, request_id=…)`, then `rook_task(action="update", id=…, data={"revision": N, "patch": {"state": "done", "attrs": {"outcome": "…"}}}, request_id=…)`. A commit made through `rook_call` whose message contains `rook: <task slug>` is linked for you.
- **Progress without a state change**: `rook_task(action="note", id=…, data={"text": …})`.

## Hygiene

- **One owner.** Claims never block anyone, so this is discipline. A second active claimant is a conflict to resolve (chat, or the project's huddle), not a co-owner.
- **Say it in the huddle** when you start, stall and finish (topic `person`).
- **An outcome says** what changed, where, and how you verified it ("merged as abc123; tests pass; service restarted and answering"). Good evidence is traceable: a commit, a journal id, a console room, a file on a worker, a PR URL.
- **Never just leave.** Stopping unfinished: `release` (the task goes `paused`; your whiteboard is saved as the handoff, or pass `data.handoff {goal, state, next_steps}` to write your own), `update` to `blocked` with `attrs.blocked_reason` and the same handoff, or `cancelled` with an outcome saying why. `closed` is only for "a person said so" and needs their quote in `data.closed_by`.
- **Close it when the work lands**: PR merged, deploy verified, done then, not later. If you orchestrate subagents, each claims its own task, or you finish them yourself.
- **Writes**: a fresh `request_id` per distinct write, the same one when retrying it.

## Mistakes

| Symptom | Fix |
|---|---|
| Task `in_progress`, claimant gone, no handoff | Read its links, latest handoff and journal; finish it, or release the stale claim (allowed after 2 h idle) with a handoff saying where it really stands |
| `done` refused | `link` evidence first, then `update` with `attrs.outcome` |
| Two claims, links on the wrong task | One claimed task per session: finish or release the current one before switching, or name the task explicitly |
