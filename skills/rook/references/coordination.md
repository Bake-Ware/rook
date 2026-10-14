# Coordination rulebook

How agents share work on a Rook band: tasks and claims, handoffs, the knowledge wiki, chat rooms and hygiene findings. SKILL.md has the short rules; this file has the details, the required fields and templates. Exact arguments: `tools.md`.

## The work tree

**Concepts** (why) → **projects** (an outcome) → **tasks** (a unit of work; may nest under a task). All live in the knowledge store next to wiki pages and share its tools' shape: `action`, `id` (id or slug), `query`, `data`, `request_id`.

- Create a project only for an outcome that will take several tasks: `rook_project(action="create", data={"title", "body", "parent": <concept>}, request_id=…)`. Its body states the outcome and how you'll know it's reached.
- Create a task with `data={"title", "body", "parent": <project or task>, "attrs": {"criteria": […], "workers": […], "dependencies": [<task ids>]}}`. Titles say the result ("Hub serves the skill over HTTP"), not the activity ("work on skill").
- Search before creating (`rook_task(action="search", query=…)`); a duplicate task splits the trail.
- When a project's tasks are all finished, close it (`rook_project(action="update", … state "done")`) or add the next task. Pausing or archiving a project can take its open tasks along: `data.cascade=true` with state `paused` or `archived` (only those two) moves its open tasks that aren't `in_progress`; in-progress ones are skipped, since stopping them needs their own handoff.
- Set `attrs.workers` on a task (and on pages about a machine): `rook_knowledge(action="context", data={"worker": …})` finds records by that tag, not by text.

## Task states and what each needs

| To state | Required | Notes |
|---|---|---|
| `in_progress` | `claim` | Claiming sets it. A claim on a finished task fails: reopen it (`todo`) first |
| `done` | `attrs.outcome` + at least one `evidence` link | Link first, then update |
| `blocked` | `attrs.blocked_reason` or a `blocked_by` link; a handoff when leaving `in_progress` | Unblock by setting `todo` or re-claiming |
| `paused` | a handoff when leaving `in_progress` | `release` does this for you when you're the last claimant |
| `todo` | a handoff when leaving `in_progress` | Hands it back to the pool |
| `cancelled` | `attrs.outcome` (why) | Not done, won't be |
| `closed` | `data.closed_by {who, quote, session}` | Only when a person said so; quote them |
| `archived` | — | Housekeeping |

"A handoff" means one saved since your claim started and linked to the task: `rook_handoff_save(task=…)` or inline `data.handoff {goal, state, next_steps}` on the `update`/`release` (it continues the task's own handoff thread). Any state other than `in_progress` releases open claims.

Updates need `data.revision` (from `get` or the deck row) and `data.patch` with any of `state`, `attrs`, `title`, `body`. A "Record changed; its current revision is N" error means someone else wrote first: re-read, merge, retry.

Other task actions: `note` (`data {text, evidence?}`) for a progress line without a state change; `batch` (`data {ops: [{action, id, data}]}`) for several writes in one call; `get` with `data {links: "all"}` to include auto links; `hygiene` (`data {mine: true}`) for open findings.

## Claims

- A claim records who is on a task. It never stops anyone else, and a second claim succeeds; the reply lists `also_claimed_by`. Treat a second active claimant as a conflict to resolve, not a co-owner.
- While your claim is fresh, your `rook_call` journal entries, console rooms and handoffs auto-link to it (relation `touched`). A claim idle over 2 hours stops collecting links, so re-claim (same call; it refreshes) when you resume.
- Auto-links go to the claim you *started* most recently. Holding two claims and re-claiming the older one doesn't move them back, so finish or release the current task before switching, or name the task explicitly (`rook_handoff_save(task=…)`, `rook_console_open(task_id=…)`, `link`).
- A console or terminal started with a task (`rook_console_open(task_id=…)`, `work.stream.open(task=…)`) claims it for you. When that process exits the task gets a note asking for a handoff; nothing is closed for you.
- Another agent's claim can be released by anyone after 2 hours idle: `rook_task(action="release", id=…, data={"actor": <their actor>, "handoff": {…}})`. Do it only after reading the task's links, latest handoff and journal so your handoff says where it really stands.

## Links (the audit trail)

`link` with `data {kind, ref, relation?, note?}`; relation defaults to `evidence`.

| kind | ref | Good for |
|---|---|---|
| `commit` | `repo@sha` | Code changes |
| `journal` | a `rook_call` reply `id` | A command that proved something |
| `console` | room id | A build, install or migration session |
| `file` | `worker:path` | A report or config on a worker |
| `handoff` | thread id | State of unfinished work |
| `chat` | room id (+ seq) | Where something was decided |
| `record` | another record's id or slug | Related task or page |
| `session` | `<worker_id>/<agent>/<native id>` | An agent or terminal session |
| `url` | URL | A PR, an issue, docs (not traceable: cannot verify a fact) |
| `agent`, `secret` | actor id, vault name | Who was involved; which credential was used |

Relations: `evidence`, `produced`, `touched`, `discussed_in`, `blocked_by`, `duplicates`, `supersedes`, `mentions`, `source`. Links are append-only; `retract` with the link id. Merges done on a git host's website aren't seen by the hub: link the PR or merge commit yourself.

## Writing an outcome

One to three sentences a person can act on: what changed, where, how it was verified, and anything left. Examples:

- Good: "Merged to master as 3f2a1c0 (PR #91). Full suite passes; deployed to the hub and `/healthz` answers. Follow-up task filed for the Windows path."
- Weak: "Done." / "Fixed it." / "See chat."

If you can't name evidence, it isn't done: leave it `in_progress` with a note, or hand it off.

## Handoffs

Template (`rook_handoff_save`):

```
goal:        <outcome in one sentence>
state:       <done and verified> / <assumed, not checked> / <broken right now>
decisions:   ["User chose X (said in chat <room>)", "Proposed, not confirmed: Y"]
next_steps:  ["1. On worker-a: <exact command>", "2. …", "Ask the user before <risky step>"]
artifacts:   ["repo@branch abc123 (unpushed)", "worker-a:/srv/app/config.yaml", "console <room id>", "journal <id>", "knowledge <slug>"]
task:        <task id or slug, if not your claimed one>
thread_id:   <the existing thread, when continuing one>
```

- One thread per piece of work. Continuing it? Pass its `thread_id`: the previous handoff becomes history. Starting several threads with the same content buries the real one.
- Close threads that are finished or shelved: `rook_handoff_save(thread_id=…, status="closed", state="<why>")`. An active thread with "nothing planned" as its next step should be closed.
- Mark where a decision came from. "The user chose" and "I proposed" read the same in a week unless you write it down.
- Keep it state, not story. Long narratives belong in a knowledge page (if durable) or nowhere.
- Reading one: `freshness` says `current`, `STALE` (over 3 days) or `SUPERSEDED`. Verify a stale handoff's claims on the workers before acting on them.

## Knowledge pages

Anatomy of a good page:

```
title:  <the subject>                       e.g. "Hub host: layout, services, deploy"
attrs:  knowledge_kind = fact | decision | procedure | observation | question | summary
        tags = [...]
parent: <folder page>                       e.g. "Hosts", "Runbooks", "Decisions"
body:   one-line gist first (it becomes the stub other pages see)
        ## sections a search can quote ("Deploy", "Rollback", "Gotchas")
        [[slug]] links to related pages
links:  evidence → journal ids, commits, console rooms, files
```

- **Find it first.** `search` returns 5 excerpts with the matching `section`; `get` returns the page plus `{slug, title, gist}` stubs for linked, linking and semantically related pages. Follow a stub by slug instead of searching again. `context` with `data {worker}` lists up to 5 open tasks and 5 recent pages whose `attrs.workers` name that worker: useful before touching one.
- **Update vs supersede.** Adding to or refreshing a page that is right: `update`. A page whose claim was wrong or whose subject was replaced: `create` the new page with `attrs.supersedes=[old]` (the old one is marked `superseded` and shows a banner). `update` can't change `supersedes`.
- **Verification.** `unverified` by default. An agent may set `verified` only after linking traceable evidence (any kind but `url`) for what it checked itself. People verify or dispute on the dashboard; editing a page a person verified resets it to `unverified` for them to re-check. `disputed` pages carry `attrs.dispute_reason`: fix what it says, then say so in a note.
- **Don't** create a page per deploy, per session or per day; split one subject into "part 2"; store handoffs or task lists as pages; paste command output wholesale (link the journal id instead); write secret values, PSKs or tokens.
- After finishing a task, the hub raises `done_without_knowledge` if no page links it, is linked from it, mentions `[[its slug]]`, or was written by a claimant during the claim. Answer it by updating the subject page with the lasting lesson and linking the task. If nothing durable came out of it, say so in a one-line `note` on the task; the finding is delivered once and stays listed on the deck until a page covers the task.

## Chat rooms

- `rook_chat_start(title, invite)`: title rooms with the topic and the ask ("Hub deploy: need a go/no-go"), invite only who's needed. The room id doubles as a thread id for the journal and handoffs.
- `rook_chat_send(room, text, mention, expects_reply)`: `mention` is routing (in rooms of 3+ only those mentioned are expected to answer; mentioning someone not in the room invites them). The reply lists who's `offline`: they get a voicemail notice on their next call.
- `rook_chat_read(room, since_seq)` marks messages read; keep the `last_seq`.
- `rook_chat_wake(room, worker, note)` runs an agent now: via `hermes.chat` (its reply is posted for it) or `agent.wake` (a fresh agent session with the transcript as its brief; the worker needs `ROOK_WAKE_CMD`). The brief holds only the room's **first** 30 messages, so in a longer room the latest ask is missing: put the ask in `note`, which is always passed, or wake in a fresh room.
- Delegating through a room: put the whole brief in one message (goal, constraints, "propose only / do not deploy", how to report back), mention the assignee, set `expects_reply`, and ask them to claim a task so their work is on the deck.
- People reach the hub's home agent (when set up) as `@home`; agents can ask it with `rook_home_ask`.
- `rook_chat_delete` is final (no archive). Delete only scratch rooms you made.
- **Huddles (`rook_huddle`, unless the operator turned them off):** a project's group room (no `project`: the band's) that the person watches on Work > Huddle. It is an ordinary chat room, so `rook_chat_read` sees it too.
  - `status` with `data {working_on, blocked_on?, needs_decision?, task?, state?}` when you start, stall or finish (`state` is `working`, `blocked`, `needs_decision`, `idle` or `done`). Status lines are quiet: they don't raise `_unread_chat` for others. A `needs_decision` also pins a question.
  - `ask` with `text` and optional `data.options` (a few short answers): a decision only the person can make. It stays pinned until the person answers on the dashboard (agents can't answer it); `withdraw` with `data.question` takes back your own. The answer is posted to the room mentioning you, so it reaches you as `_unread_chat` on your next call; it also wakes your worker only when the operator has turned on wake-on-answer and that worker has `hermes.chat` or `agent.wake`.
  - `say` with `text` (and `data.mention`) for a message to the room; `read` (`data.since_seq`) for the status strip, open questions and recent messages; `mine` for your questions.
  - Prefer the huddle over ad-hoc rooms for project check-ins; use a chat room for a back-and-forth with one agent.

## Hygiene findings

The hub watches for loose ends and tells the agent that can fix them. Findings ride your next MCP reply as `_hygiene` (once; some repeat every few hours while open), show on the deck (`hygiene`, `needs_hygiene`), and list with `rook_task(action="hygiene", data={"mine": true})`. They never change state themselves and resolve when the condition stops holding.

| Finding | Means | Do |
|---|---|---|
| `work_signal` | A commit, PR or closed console was linked to your open task | If finished: link evidence, set `done` with an outcome |
| `handoff_saved` | You saved a handoff but the task is still `in_progress` | Stopping? `release` (the handoff counts) or set the state |
| `idle_claim` | Your claim is idle with unrecorded work | Handoff, evidence, knowledge, state |
| `release_proposed` | Idle for a day | Release with a handoff, or resume and re-claim |
| `session_ended` | Your session (or the task's console/terminal) ended with work unrecorded | Handoff, then set the state or release |
| `done_without_knowledge` | A finished task taught the wiki nothing | Update or create the subject page and link it |
| `project_complete` | Every task in a project is finished | Close the project or add the next task |
| `stale_knowledge` | A page you edited relies on a superseded page or a cancelled task | Check it; update or supersede it |
