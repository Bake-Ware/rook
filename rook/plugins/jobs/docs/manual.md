# Jobs

A job is a trigger and one call: when the trigger fires, the job calls one
cap and keeps a run record. Logic (steps, branching, loops, asking a person)
goes in a hub plugin script cap that the job calls.

## Triggers

| Trigger | Fires |
|---|---|
| `{cron: "0 8 * * 1-5"}` | on a five-field schedule in the `timezone` setting (default UTC); `@daily` and friends work |
| `{every: 5m}` | every interval (1s or more) |
| `{on: worker.online, filter: {name: kaiju}}` | on an event (a glob); `filter` keys match the payload (dotted paths; a list means any of) |
| `{manual: true}` | only by `job.run` (any job can be started by hand) |

Jobs never fire on `job.*` events (their own records), so they can't loop
on themselves.

## The call

`call` (the cap), `worker` (blank: the worker that serves it) and `args`.
When an event fires the job and the cap declares an `event` arg, the call
also gets `event: {name, payload}`, so a cap can act on what happened (the
text that arrived, the worker that came online).

## Who it runs as

The call goes through dispatch like any other call: why, rights,
holds, journal. It runs as:

- its **creator** (the default): the person, or the agent acting for that
  person, who made it, with their rights as they are at each run;
- a **persona** (`as: persona`, `persona: archivist`): `agent:<persona>`
  with that agent's own grants. Owners only.

Events show what happens band-wide, so `on` jobs are for owners too. Jobs
and their runs are private: their person and the owners see them.

## Runs

- `timeout` (default: the `timeout` setting, 10m) stops a run.
- One run per job at a time: a trigger that finds it running is skipped.
- No checkpoints. A run cut off by a hub restart is marked failed at the
  next start, so write calls that are safe to run twice. `catch_up: true`
  runs a `cron`/`every` job once at start when a slot was missed while the
  hub was down.

## Ready-made jobs

Any plugin can ship jobs in its manifest (`jobs:`, each a trigger and a
`call`). `job.templates` lists them; `job.install template=plugin/job` makes
one yours (it runs as you). A plugin's `enabled: true` job that runs as a
persona is installed active; edit it and plugin updates leave it alone.

## The Jobs page

Hub > Jobs lists every job you can see: what triggers it, the call it makes
and where, who it runs as, its last run (ok, or the error), and an on/off
switch. Run now starts it; click a job for its recent runs with their result
or error. Ready-made jobs from plugins are listed under it with Install. The
page updates as jobs and runs change. Jobs are made by agents (`job.create`),
not on the page.
