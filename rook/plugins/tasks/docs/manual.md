# Tasks

A task is one piece of work, usually in a project (`project`), sometimes
part of a bigger task (`parent`). Its title says the result, not the
activity; `criteria` say what done means.

Working a task:

1. `task.claim` before you start. The task goes `in_progress` (from `todo`
   or `paused`); the reply is the task and who else holds a claim. Several
   may work one task; talk to the others.
2. Read the task's whiteboard (`whiteboard.read task=<id>`) and keep it
   current while you work: it is how the next person picks up where you are.
3. `task.link` what the work produced or rests on (a PR, a commit, a journal
   entry, a page, a task that blocks it with `relation: blocked_by`).
   `task.comment` for anything worth saying.
4. Finish with `task.update state=done outcome=...` and the evidence linked,
   or `task.release` with the state it is in (`blocked`, `paused`, `todo`).

The record caps (`task.list`, `get`, `search`, `create`, `update`,
`archive`, `history`) are the records kit's; updates need the current
`revision`. `task.list where={assignee: ...}` is what is waiting on someone.

**Handing off** (`task.handoff to=<who> note=<what they need>`): sets
`assignee`, drops your claim, notes it, and sends the event
`task.handed_off` so an agent or a person can be told. Hand off directly
(engineer to QA to whoever deploys); go through the project manager when the
task depends on other work.

**Sign-off** (`task.request_signoff reviewers=[...]`): any task can ask for
sign-off before it is done. Reviewers come from the project's concepts
(`concept.pool`); the project's trust level says whether the person must be
one of them. Reviewers answer with `task.signoff approve=true|false`
(`reviewer=` when they answer a request made to a role). Nothing enforces
it: sign-off is a working habit, like on a real team.

A stale claim (someone stopped and never released) can be taken off by
anyone with `task.release actor=<them>`; the journal records who did.
