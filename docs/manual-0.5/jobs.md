# Jobs

A **job** is a time or an event that fires one call. Put the `jobs` plugin
in the hub's library to use them. Logic goes in a hub plugin script cap that
the job calls ([Writing a plugin](authoring.md)); a job holds no steps.

## Triggers

| Trigger | Fires |
|---|---|
| `{cron: "0 8 * * 1-5"}` | on a schedule, in the `timezone` setting (default UTC) |
| `{every: 5m}` | every interval |
| `{on: worker.online, filter: {name: kaiju}}` | on an event; `filter` matches the event's payload |
| `{manual: true}` | only when started by hand (`job.run`); any job can be |

## Making one

```
call worker=rook cap=job.create args={
  "name": "kaiju-disk",
  "trigger": {"cron": "0 7 * * *"},
  "call": "shell.exec", "worker": "kaiju", "args": {"argv": ["df", "-h", "/"]}
} why="Check kaiju's disk every morning."
```

A job fired by an event passes it on: if the cap takes an `event` arg, the
call gets `event: {name, payload}`. Options: `timeout` (default 10
minutes), `catch_up` (run once at start if a scheduled slot was missed while
the hub was down).

`job.list`, `job.get`, `job.update` (with the current revision), `job.run`
and `job.run.list` manage them.

## Who a job runs as

A job's call goes through dispatch like any other (why, rights, gates,
holds, journal). By default it runs as **its creator**, with their rights as
they are at each run. Owners can run a job as a **persona** (`as: persona,
persona: archivist`): the agent's own, narrower grants. `on` jobs and
persona jobs are for owners. Jobs and their runs are private to their
person and the owners.

## Ready-made jobs

Plugins ship jobs in their manifest; `job.templates` lists them and
`job.install template=<plugin>/<job>` makes one yours:

```yaml
manifest: 1
plugin: wiki-care
version: 0.1.0
runs_on: hub
jobs:
  weekly-sweep:
    doc: The archivist sweeps the wiki every Sunday night.
    trigger: { cron: "0 4 * * 0" }
    call: agent.chat.send
    args: { agent: archivist, text: Sweep the wiki for stale and duplicate pages. }
```

More: [the jobs plugin's manual](../../rook/plugins/jobs/docs/manual.md).

## Timers inside a plugin

A plugin that only needs its own timer doesn't need a job: its manifest's
`hooks` run code `every: [{interval: 30m, run: ...}]` or `on: [{event: ...,
call: ...}]` ([Writing a plugin](authoring.md#hooks-and-events)).
