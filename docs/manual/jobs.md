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
- send you a phone notification, a voice message or a Telegram message.

Each step has a time limit and can retry. After a step, the job can go one way
on **success**, another on **failure**, and another if it **hangs**. Branches can
run side by side and meet again.

## When jobs run

- On a **schedule** written in cron style, such as `0 3 * * *` (every day at
  3 am) or `@hourly`. The editor's schedule helper writes these for you and
  shows the next five run times in plain English.
- **At** a set time, once.
- **After** the previous run finished, with a gap.
- Only when you press **Run now**.

Times use the hub's time zone unless the job sets its own. Daylight saving
changes are handled so a job runs once, not twice or never. If the hub was off
when a job was due, it runs once when the hub is back, if that is within the
job's grace period.

## The Jobs page

- **Overview**: what is running, what is next, recent failures and blocked jobs.
- **Jobs**: every job; switch one on or off, run it now, edit or delete it.
- **Editor**: the job itself. **Validate** checks it, **Save** (or Ctrl+S)
  stores it.
- **Runs**: the history of every run, with each step's output. Runs are kept
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
