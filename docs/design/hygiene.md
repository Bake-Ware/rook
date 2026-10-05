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

| Kind | Fires when | Says | Resolves when |
|---|---|---|---|
| `work_signal` | A commit or PR is linked to an open task: by hand (`link` kind `commit`, or a `url` with `/pull/` or `/commit/`), or found in a `rook_call` reply (below); or a console room linked to the task is closed | "If the work is finished: update state done with attrs.outcome" | The task leaves `in_progress`, or the signal expires |
| `handoff_saved` | A handoff is linked to a task still `in_progress` | "Stopping? release it (the handoff counts) or set paused/blocked/done" | Same |
| `idle_claim` | Claimed, `in_progress`, idle past `hygiene_idle_minutes`, with work since the last handoff. Past `hygiene_dirty_hours` the claim is also marked dirty | "Stopped? save a handoff, link evidence, record knowledge, set the state" | The claimant is active again, releases, or the task leaves `in_progress` |
| `release_proposed` | The same claim idle past `hygiene_release_hours` (to the claimant and, unaddressed, to the deck) | "Release it with a handoff: `rook_task release data {actor, handoff}`" | Same |
| `session_ended` | An MCP session closes (HTTP DELETE) while its actor holds an `in_progress` claim with work since the last handoff, and no other session of that actor is open. The claim is marked dirty | "Save a handoff, then set the state or release" (delivered when that actor is back) | The task leaves `in_progress`, a re-claim, or expiry |
| `done_without_knowledge` | A task goes `done` (on the update reply itself; the scan catches tasks finished elsewhere within `hygiene_done_window_days`) and no knowledge page links it, is linked from it, mentions `[[slug]]`, or was written by one of its claimants after their claim started | "Update a page (suggestions from search) or create one, and link it" | Such a page appears. Raised once |
| `project_complete` | An `active` project whose tasks are all finished, the last one more than `hygiene_project_idle_hours` ago (to the project's creator and the last actor on its tasks) | "rook_project update state done, or add the next task" | A task reopens or is added, or the project changes state |
| `stale_knowledge` | An active page mentions or record-links a superseded page, or links a cancelled task (to its last editor) | "Check it still holds; update or supersede it" | The page no longer relies on them |

### Commits and PRs from band calls

When a `rook_call` succeeds and its args mention `git` or `gh`, the reply's
stdout is scanned for `git commit` output (`[branch 1a2b3c4] subject`) and, for
`gh`, GitHub pull request URLs. Each is auto-linked:

- to the task the commit message names, as **evidence**: `rook: <task id or
  slug>` anywhere in the args (or a bare `t_<id>`), the convention for commit
  messages;
- otherwise to the caller's live claim, as **produced** (the existing
  auto-link rule: claims idle past `STALE_CLAIM_SECS` collect nothing).

Then the `work_signal` above fires. A commit made through a console
(`rook_console_write`) isn't seen; link it by hand or name the task.

There is no GitHub webhook on the hub, so merges done on github.com are not
seen. Adding one later only needs to call `HygieneEngine.on_link`.

## Delivery

- `_hygiene` is added by the MCP bridge after any tool runs
  (`server.py` `_with_hygiene`, `envelope.add_notice`): a JSON-object reply gets
  the key, a plain-text reply (`text=true`) a last `[rook] {...}` line. Each
  item is `{kind, id (slug), say, suggest?}`.
- The deck marks rows and projects with open findings (`hygiene: [kinds]`).
  `needs_hygiene` still means a dirty claim, now also set by the scan and by
  ended sessions.
- `rook_task(action="hygiene", id?, data {mine, limit})` lists open findings.
  `rook_task(action="get")` and `rook_knowledge(action="get")` include the
  record's open findings as `hygiene`.
- People: with `hygiene_notify_people` on, release proposals and finished
  projects are also posted through `notify.send` (Telegram/Discord), once per
  finding.
- The §5 in-session nudge (`band_mcp/hygiene.py`) now takes its idle threshold
  from `hygiene_idle_minutes` and stops when `hygiene_enabled` is off.

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

## Where it lives

- `rook/hub/plugins/knowledge/hygiene.py`: `HygieneEngine` (findings table,
  event hooks, scan, delivery). Store-only, no network.
- `rook/hub/plugins/knowledge/migrations/002_hygiene.sql`: the `hygiene` table.
  A new table only, so `user_version` stays 2 and an older release still opens
  the file.
- `rook/hub/plugins/knowledge/service.py`: hooks after `link`, `update`,
  `claim`, `release` and knowledge writes; `auto_link`; the `hygiene` read
  action; deck and get flags.
- `rook/hub/plugins/knowledge/__init__.py`: the settings and the scan loop
  (`hygiene_tick`, started with the plugin).
- `rook/band_mcp/server.py`: `_hygiene` piggyback, the session-to-actor map and
  the session-end hook, `on_call` after each `rook_call`, `on_console_closed`.
- `rook/band_mcp/http_sessions.py`: `on_session_end`, called on DELETE only (an
  evicted session belongs to a client that never said it was done).

## First run on a hub with history

The scan only looks at recent done tasks (`hygiene_done_window_days`), but idle
claims, finished projects and stale pages are raised for whatever exists. At one
item per reply that is a trickle per actor; set `hygiene_hints_per_reply` to 0
first to review the deck flags without agents seeing them.

## Not done

- A GitHub webhook (merges on github.com), and commits made in consoles.
- Asking a freshly woken agent to do the write-up for `done_without_knowledge`
  (the §5 wake path); today it is a finding.
- Per-worker rate limits; limits are per (record, kind, actor).
