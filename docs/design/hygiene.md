# Hygiene triggers: tasks and knowledge that keep up with the work

Status: implemented on branch `hygiene-triggers`. Extends
[DESIGN-agent-work-system.md](../DESIGN-agent-work-system.md) §5 (the idle-claim
nudge into the agent's own session), which stays as it was.

## Problem

The work system records what agents do (claims, auto links, handoffs), but
closing the loop is left to the agent: marking the task done when the commit
lands, writing the knowledge page, saving a handoff before the session ends,
closing a finished project. Agents forget. The deck grooming session of
2026-10-02 spent most of its time finding exactly these: tasks still
`in_progress` after their PR merged, finished work with nothing written down,
claims left open by sessions that ended.

The hub sees the moments when this goes wrong. It should say something at that
moment, to the agent that can fix it, and leave a mark for whoever comes next.

## Rules

- **Findings, not actions.** A trigger opens a *finding*: a short row that says
  what looks wrong and proposes the fix. The hub never changes a record's state,
  title, body or attrs, and never releases a claim. The only writes are: the
  finding itself, an event on the record (`hygiene`), automatic links (a commit
  or PR to its task), and a claim's existing `dirty` mark (the deck's
  `needs_hygiene`).
- **Bookkeeping never blocks.** Every hook runs after the write or call that
  triggered it and swallows its own errors.
- **Deduplicated and rate-limited.** One open finding per (record, kind, actor).
  The same finding is not raised again within `hygiene_renotify_hours`. Some
  kinds are raised once ever per (record, actor).
- **Delivered where the agent looks.** A finding addressed to an actor rides
  that actor's next MCP reply as `_hygiene`, at most `hygiene_hints_per_reply`
  per reply, once (idle claims, release proposals and finished projects repeat
  each renotify period while still open). Every open finding is also on the deck
  (`hygiene: [kinds]` on the task row or project) and in
  `rook_task(action="hygiene")`, so it isn't lost if the addressee never returns.
- **Resolved by the condition, not by the agent.** Nobody has to dismiss a
  finding. It resolves when its condition stops holding (state changed, page
  linked, claimant active again) or, for the event kinds, after
  `hygiene_signal_hours`.

## Triggers

**The whiteboard stands in for the handoff** (`rook/hub/plugins/knowledge/whiteboard.py`,
agent sessions design item 13). Each claim has a whiteboard the hub fills from the
session's calls (each call's `why`, worker and result), consoles, handoffs and commits,
plus what the agent jots (goal, decisions, open items). Wherever a handoff would be asked
for, a claim whose whiteboard has work since the task's last handoff gets the board saved as
that handoff instead: on `release` or a stop by `update` (paused/blocked/todo) without
`data.handoff`, when the client closes its MCP session (the session's own claim, or all the
actor's claims when it was its last session), and in the idle-claim loop
(`band_mcp/hygiene.py`, before any nudge). So `idle_claim` and `session_ended` below fire
only for claims with nothing new on their board. An MCP session's idle timeout (5 min) is not
a stop: it saves nothing.

| Kind | Fires when | Says | Resolves when |
|---|---|---|---|
| `work_signal` | A commit or PR is linked to an open task: by hand (`link` kind `commit`, or a `url` with `/pull/` or `/commit/`), or found in a `rook_call` reply (below); or a console room linked to the task is closed | "If the work is finished: update state done with attrs.outcome" (a PR: "keep it in_progress until the PR merges", see `pr_merged`) | The task leaves `in_progress`, or the signal expires |
| `handoff_saved` | A handoff is linked to a task still `in_progress` | "Stopping? release it (the handoff counts) or set paused/blocked/done" | Same |
| `idle_claim` | Claimed, `in_progress`, idle past `hygiene_idle_minutes`, with work since the last handoff. Past `hygiene_dirty_hours` the claim is also marked dirty | "Stopped? save a handoff, link evidence, record knowledge, set the state" | The claimant is active again, releases, or the task leaves `in_progress` |
| `release_proposed` | The same claim idle past `hygiene_release_hours` (to the claimant and, unaddressed, to the deck) | "Release it with a handoff: `rook_task release data {actor, handoff}`" | Same |
| `session_ended` | An MCP session closes (HTTP DELETE) while its actor holds an `in_progress` claim with work since the last handoff, and no other session of that actor is open. The claim is marked dirty. Also: a session linked to the task ends on its own (a console room's process exits, a Rook terminal started for the task closes, or the Sessions page posts a `note` with `data.session_end`); the task gets a note and its claimants the finding | "Save a handoff, then set the state or release" (delivered when that actor is back) | The task leaves `in_progress`, a re-claim, or expiry |
| `done_without_knowledge` | A task goes `done` (on the update reply itself; the scan catches tasks finished elsewhere within `hygiene_done_window_days`) and no knowledge page links it, is linked from it, mentions `[[slug]]`, or was written by one of its claimants after their claim started | "Update a page (suggestions from search) or create one, and link it" | Such a page appears. Raised once |
| `project_complete` | An `active` project whose tasks are all finished, the last one more than `hygiene_project_idle_hours` ago (to the project's creator and the last actor on its tasks) | "rook_project update state done, or add the next task" | A task reopens or is added, or the project changes state |
| `stale_knowledge` | An active page mentions or record-links a superseded page, or links a cancelled task (to its last editor) | "Check it still holds; update or supersede it" | The page no longer relies on them |
| `pr_merged` | An `in_progress` task's linked pull requests are all merged or closed (one merged): seen by the GitHub sweep, or at once when the hub merges it (huddle merge vote). The merge commit is linked as **evidence** (automatic link) | "Finished? update state done with attrs.outcome. More to do? Link the next PR" | The task leaves `in_progress`, or the link is retracted |
| `pr_ci_failed` | An open linked pull request's checks failed at its head (sweep, `in_progress` tasks). A new failing head rewrites the finding and delivers it again | "Fix it before asking for a merge, marking done or reporting it green: gh pr checks N" | CI is no longer failing (running or green), the PR closes, or the task leaves `in_progress` |
| `pr_open_done` | A `done` task (within `hygiene_done_window_days`) links a pull request that is still open (to the claimants and whoever set done) | "Reopen it until the PR merges, or retract the link" | The PR merges or closes, the link is retracted, or the task leaves `done` |

### Pull requests behind tasks (`knowledge/prs.py`)

Aimed at three failures seen on 2026-10-27: tasks marked done before their PR
merged, claims left `in_progress` after it merged, and agents asking for merges,
marking done or reporting green while CI was red or still running. Deterministic:
the task's own links plus the PR's state on GitHub, no model.

- **Which links.** A link whose ref is a pull request URL
  (`github.com/<owner>/<repo>/pull/N`) with relation `evidence`, `produced` or
  `touched` (the `gh pr create` auto-link is `produced`). `mentions`, `source`,
  `blocked_by` and the rest are about other work. Retracted links do not count.
- **Done check.** `update` to `done` reads the linked PRs (at most 5, cached a
  minute). One still open (a draft too) → an **agent** is refused with what to
  do: get CI green, have the chair pin it and call the merge vote (the hub
  merges and links the merge commit), then mark it done; or retract the link if
  that PR isn't this task's (the message names the link ids). A **person**
  gets the same text as `pr_check` on the reply and the update goes through:
  people decide. A PR closed without merging is a `pr_check` note. GitHub
  unreachable → the update goes through with a note: never a refusal. This is
  a saving rule on the task's state, like "done needs evidence"; it adds no
  hold, guardrail or trust entry.
- **Sweep.** Every `hygiene_github_minutes` (from the hygiene loop): in-progress
  tasks and recently done ones (`hygiene_done_window_days`) with PR links.
  Merged PRs are cached for good; open ones are read again when stale; a head's
  checks are read only for in-progress work and only again while not green. At
  most 40 reads a sweep; a 403/429 or a network error stops reading for that
  sweep, and a task whose PRs could not be read keeps its findings as they are.
- **The hub's merge.** `Table.execute` calls `knowledge.pr_merged(repo, n,
  merge_sha, head)` after merging: the open tasks linking that PR get the merge
  commit as evidence, and the in-progress ones (whose other PRs are settled) a
  `pr_merged` finding, without waiting for the sweep.
- **Credential.** The same GitHub client the huddle merge gates use
  (`huddle.table.GitHub`), with the hub's credential (`huddle.vote_github_secret`)
  read from the vault once per sweep or done check (`system:hygiene`), else
  anonymous reads (60 an hour per IP: enough for done checks, thin for sweeps).
- **CI at pin time.** Pinning a PR on the conference table reads its head's
  checks and says where CI stands in the pin reply (`ci`) and the room line
  ("CI still running: ... (wait for green before the vote)"). The ci gate is
  unchanged. A vote's `artifact` may also be the PR's URL, `owner/repo#N` or
  `#N` (its newest live pin in that huddle).

### Commits and PRs from band calls

When a `rook_call` succeeds, the reply's stdout is scanned for `git commit`
output (`[branch 1a2b3c4] subject`, only when the args run `git commit`) and
for the pull request URL `gh pr create` prints (only for `gh pr create`:
`gh pr view`/`list` print PRs that already exist). Each is auto-linked:

- to the task the commit message names, as **evidence**, only if the caller
  holds a live claim on that task in the band of the worker the call ran on.
  The name is `rook: <task id or slug>` (or a bare `t_<id>`), read from the
  commit message only: the `-m`/`--message` values, the here-document fed to
  `-F -`, and the subject line in the output. Text elsewhere in the args
  (other commands, other fields) is ignored;
- otherwise to the caller's own live claim (in that band), as **produced**
  (the existing auto-link rule: claims idle past `STALE_CLAIM_SECS` collect
  nothing). Never as evidence: a message can name any task, and an evidence
  link is what lets a task go `done`.

Then the `work_signal` above fires. A commit made through a console
(`rook_console_write`) isn't seen; link it by hand or name the task.

There is no GitHub webhook on the hub: merges done on github.com are seen by
the PR sweep (below), within `hygiene_github_minutes`.

## Delivery

- `_hygiene` rides only replies Rook builds itself. `rook_call` adds it to its
  own notices: a key on the compact JSON envelope, or the same single
  `[rook] {...}` line as `_task`/`_tips` with `text=true`; the worker's output
  is never edited. The task/project/concept/knowledge tools (and a few other
  Rook-built JSON objects, `server._HYGIENE_TOOLS`) get the key merged in
  after the tool runs (`envelope.add_notice`). Any other reply (a list,
  plain text, someone else's data) is left alone and the finding stays queued
  for the next reply that can carry it: a finding is marked delivered only
  once it is on a reply. Each item is `{kind, id (slug), say, suggest?}`.
- The deck marks rows and projects with open findings (`hygiene: [kinds]`).
  `needs_hygiene` still means a dirty claim, now also set by the scan and by
  ended sessions.
- `rook_task(action="hygiene", band?, id?, data {mine, limit})` lists open
  findings in the caller's bands (`band` narrows to one).
  `rook_task(action="get")` and `rook_knowledge(action="get")` include the
  record's open findings as `hygiene`.
- People: with `hygiene_notify_people` on, release proposals and finished
  projects are also posted through `notify.send` (Telegram/Discord), once per
  finding.
- The §5 in-session nudge (`band_mcp/hygiene.py`) now takes its idle threshold
  from `hygiene_idle_minutes` and stops when `hygiene_enabled` is off. An idle
  period is announced once: the scan raises no `idle_claim` for a claim that
  loop already nudged (`claims.nudged` with a `hygiene_nudge` event since the
  claim went idle), and the loop skips a claim whose `idle_claim` finding was
  already delivered.

## Settings

On the `knowledge` plugin, group "Hygiene", all applied live. Env overrides use
the canonical names (`ROOK_KNOWLEDGE_HYGIENE_IDLE_MINUTES` and so on).

| Setting | Default | Meaning |
|---|---|---|
| `hygiene_enabled` | `true` | All triggers and the in-session nudge |
| `hygiene_idle_minutes` | 30 | Idle claim nudge |
| `hygiene_dirty_hours` | 4 | Mark the claim dirty (deck `needs_hygiene`) |
| `hygiene_release_hours` | 24 | Propose releasing the claim |
| `hygiene_renotify_hours` | 6 | Repeat open repeating findings; also the re-raise rate limit |
| `hygiene_signal_hours` | 24 | Event findings expire |
| `hygiene_done_window_days` | 3 | How far back the scan checks done tasks for knowledge |
| `hygiene_project_idle_hours` | 24 | Quiet time before proposing to close a finished project |
| `hygiene_hints_per_reply` | 1 | `_hygiene` items per MCP reply (0 = deck and `action="hygiene"` only) |
| `hygiene_scan_seconds` | 300 | Scan interval |
| `hygiene_notify_people` | `false` | Also post selected findings through `notify.send` |
| `hygiene_github_minutes` | 10 | How often tasks' linked pull requests are read on GitHub (the PR findings above); 0 = off (the done check still runs) |

## Where it lives

- `rook/hub/plugins/knowledge/hygiene.py`: `HygieneEngine` (findings table,
  event hooks, scan, delivery). Store-only, no network.
- `rook/hub/plugins/knowledge/prs.py`: `PullRequests` (the done check, the
  GitHub sweep, the merge hook). The only part that talks to GitHub.
- `rook/hub/plugins/knowledge/migrations/002_hygiene.sql`: the `hygiene` table
  and an `events(actor, ts)` index. New objects only, so `user_version` stays 2
  and an older release still opens the file.
- `rook/hub/plugins/knowledge/service.py`: hooks after `link`, `update`,
  `claim`, `release` (each op of a `batch` too, which goes through the same
  path), and the knowledge re-check after knowledge-page writes and links;
  `auto_link`; the `hygiene` read action; deck and get flags.
- The scan evaluates in a read transaction and writes (inserts, resolves,
  dirty marks) in one short write transaction, only when there is something
  to write.
- `rook/hub/plugins/knowledge/__init__.py`: the settings and the scan loop
  (`hygiene_tick`, started with the plugin).
- `rook/band_mcp/server.py`: `_hygiene` piggyback, the session-to-actor map and
  the session-end hook, `on_call` after each `rook_call`, `on_console_closed`.
- `rook/band_mcp/http_sessions.py`: `on_session_end`, called on DELETE only (an
  evicted session belongs to a client that never said it was done), and
  `on_session_gone`, called when a session ends any way, which drops it from
  the session-to-actor map (so a timed-out session doesn't count as "another
  session of that actor is still open"). The map is also capped (LRU).

## First run on a hub with history

The scan only looks at recent done tasks (`hygiene_done_window_days`), but idle
claims, finished projects and stale pages are raised for whatever exists. At one
item per reply that is a trickle per actor; set `hygiene_hints_per_reply` to 0
first to review the deck flags without agents seeing them.

## Not done

- A GitHub webhook (the PR sweep polls instead), and commits made in consoles.
- Commit links are not checked against the default branch (only PR links are).
- Asking a freshly woken agent to do the write-up for `done_without_knowledge`
  (the §5 wake path); today it is a finding.
- Per-worker rate limits; limits are per (record, kind, actor).
