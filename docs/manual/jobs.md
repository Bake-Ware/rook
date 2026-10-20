---
id: jobs
title: Jobs: scheduled work
order: 11
features:
  - features/jobs
  - plugin/hub/jobs
  - settings/job
since: 0.1.0
deprecated: false
---
# Jobs: scheduled work

A **job** is work the hub runs for you on a schedule or when you ask: back up a
machine every night, check a website every 15 minutes and tell you when it
breaks, run something again an hour after it last worked.

## What a job is made of

A job is a short list of **steps**. A step can:

- run a capability on a machine (or on the hub);
- run the same capability on every machine that matches a filter;
- wait;
- send you a phone notification, a voice message or a Telegram message;
- hand a task to an agent: the home agent, a huddle role (`agent: "role:qa"`,
  which answers with its own sources, tools and memory) or a coding session;
- hold a **huddle** round: the step posts a question into a huddle with roles,
  waits for the home agent's summary and sends it to you (Telegram, voice or
  notification), with the action cards left in the huddle for you to approve.
  A weekday 08:30 standup is a scheduled job with one such step; see
  [[manual-huddles]].

Each step has a time limit and can retry. After a step, the job can go one way
on **success**, another on **failure**, and another if it **hangs**. Branches can
run side by side and meet again.

## When jobs run

- On a **schedule** written in cron style, such as `0 3 * * *` (every day at
  3 am) or `@hourly`. The editor's schedule helper writes these for you and
  shows the next five run times in plain English.
- **At** a set time, once.
- **After** the previous run finished, with a gap.
- Only when you press **Run now** (▶ on the Jobs tab, or in the editor's ⋯ menu).

Times use the hub's time zone unless the job sets its own. Daylight saving
changes are handled so a job runs once, not twice or never. If the hub was off
when a job was due, it runs once when the hub is back, if that is within the
job's grace period.

## Jobs that end themselves

A job that waits for something ("tell me when the deploy finishes") should
stop once it has told you. Give it an end:

- **Complete on a step.** Mark the step that sends the news with
  `"complete": "success"`. When that step works, the job switches itself off
  for good after the run. An agent step can do the same: the agent ends with
  *complete*, and the hub switches the job off.
- **Limits.** Under `limits`, `until` stops the job at a set time,
  `max_runs` stops it after that many runs, and `complete_on: "success"` stops
  it after its first run that works.
- **Too many failures.** A job that fails 10 runs in a row switches itself
  off (`max_failures`, or the hub setting *Switch off after failures*).

A job that switched itself off shows *paused: completed*, *max_runs*,
*expired* or *max_failures*, and its history says which run did it. Turning it
back on starts the counts again.

**No repeats.** A job sends the same message to the same phone or chat at
most once every 6 hours (`limits.notify_window`, or the hub setting *Repeat
message window*; `0` turns this off). Numbers and punctuation don't make a
message new, so "failed 68 times" and "failed 69 times" count as the same
message.

## Locked jobs

Lock a job (🔒, in its ⋯ menu or the editor's ⋯ menu) to keep it exactly as it
is. While it is locked, no agent or API token can change it, switch it on or
off, delete it or cancel its runs; they get a *locked* error and have to ask
you. Only a person signed in to the dashboard (or in the `rook band` panel,
key J) can change it, lock it or unlock it. Anyone who may run it still can.

A locked job still ends itself by the limits and *complete* steps it had when
you locked it. The hub's *Switch off after failures* setting doesn't apply to
it, and an agent step can end it only if that step says `"complete":
"agent"`. Every lock and unlock is in the job's history.

## The Jobs page

- **Overview**: what is running, what is next, recent failures and blocked jobs.
- **Jobs**: every job; switch one on or off, run it now (▶), and edit, see runs,
  lock or unlock (🔒) or delete it from its ⋯ menu. **+ New** starts a new job; **All / On / Off**
  filters the list.
- **Editor**: the job itself. **Validate** checks it, **Save** (or Ctrl+S)
  stores it; the ⋯ menu has **Format**, **Run now** and the job **Schema**.
- **Runs**: the history of every run, with each step's output (tap a run to open
  it; tap its id to copy it; ↻ refreshes). Runs are kept
  for 30 days by default.
- **Guardrails**: what jobs may never do (see below).
- **Settings**: the jobs time zone, history length and other defaults (operator
  only).

## Who a job runs as

A job runs as whoever created it, or as an agent token you choose. If that
identity is revoked, the job pauses until you give it a new one (or uses the
fallback identity you set). Someone who edits another person's job takes it
over and it runs as them from then on.

## Guardrails

Guardrails stop a job from doing something dangerous even if its identity
could: changing Rook's own settings or permissions, removing machines,
updating or restarting machines or the hub, deleting things for good. Running
ordinary commands is allowed. A step that hits a guardrail stops as **blocked**.
Before you save a guardrail change, the page shows which jobs it would block.

## Passwords in jobs

Never type a password into a job. Store it under Secrets and write
`{{secret:name}}` instead; see [[manual-secrets]]. Run logs show the placeholder,
never the password.
