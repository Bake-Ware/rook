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
   `task.comment` for anything worth saying (`task.comment.list
   where={task: <id>}` reads them).
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
sign-off before it is done. With no reviewers named it goes to the expert
roles of the project's concepts (`concept.pool`), as `role:<name>`; on a
sacred project (`project.trust`) the owners are added, since the trust level
decides whether the person signs. With the agents package installed, the
agents asked are messaged with the request (as the one asking). Reviewers
answer with `task.signoff approve=true|false` (`reviewer=` when they answer
a request made to a role), or for a pull request with `github.review`;
`task.signoff.list where={task: <id>}` shows where it stands. Nobody signs
for someone else: a person's sign-off comes from that person's own login,
never from an agent acting for them. Comments and sign-offs are written
only by these caps, so who said or signed what is always the caller.
Nothing stops a task being marked done without sign-off: it is a working
habit, like on a real team.

A stale claim (someone stopped and never released) can be taken off by
anyone with `task.release actor=<them>`; the journal records who did.
