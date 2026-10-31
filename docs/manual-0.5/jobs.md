# Jobs

A **job** does one thing when something happens: calls a cap, asks an
agent, or runs a short Python script. Put the `jobs` plugin in the hub's
library to use them.

## Triggers

| Trigger | Fires |
|---|---|
| `{cron: "0 8 * * 1-5"}` | on a schedule, in the `timezone` setting (default UTC) |
| `{every: 5m}` | every interval |
| `{on: worker.online, filter: {name: kaiju}}` | on an event; `filter` matches the event's payload |
| `{manual: true}` | only when started by hand (`job.run`); any job can be |
| `{webhook: true}` | on a signed POST to the hub (`job.webhook` gives the address and key) |

## Making one

```
call worker=rook cap=job.create args={
  "name": "kaiju-disk",
  "trigger": {"cron": "0 7 * * *"},
  "call": "shell.exec", "worker": "kaiju", "args": {"argv": ["df", "-h", "/"]}
} why="Check kaiju's disk every morning."
```

Exactly one of `call` (with `worker` and `args`), `agent` (`{persona?,
prompt}`: ask an agent) or `script` (Python, owners only). Options:
`retries` (a failed run runs again, waiting longer each time), `notify`
(`failure`, the default, tells you when a run fails for good; `always`;
`never`), `timeout` (default 10 minutes), `catch_up` (run once at start if
a scheduled slot was missed while the hub was down).

`job.list`, `job.get`, `job.update` (with the current revision),
`job.run`, `job.cancel` and `job.run.list` manage them.

## Who a job runs as

Every call a job makes goes through dispatch like any other (why, rights,
gates, holds, journal). By default it runs as **its creator**, with their
rights as they are at each run, so taking a right away from someone also
stops their jobs from using it. Owners can run a job as a **persona**
(`as: persona, persona: archivist`): the agent's own, narrower grants.
Script jobs, `on` jobs and persona jobs are for owners. Jobs and their runs
are private to their person and the owners.

A job that hits a hold waits for the person like any call.

## Scripts

```python
def main(job):
    files = job.call("file.list", {"path": "/srv/backups"}, on="nas", why="See last night's backups.")
    if not files["entries"]:
        job.notify("Last night's backup is missing.")
    return {"files": len(files["entries"])}
```

`job.call`, `job.notify`, `job.ask` (a question for the person),
`job.wait`, `job.retry`, `job.parallel`, `job.log`. A script runs in its own
Python process on the hub, as the hub's normal account, with the standard
library only. Write jobs that are safe to run twice: a run cut off by a hub
restart is marked failed and retried.

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
    agent: { persona: archivist, prompt: Sweep the wiki for stale and duplicate pages. }
```

The jobs plugin ships `security-scan`, which tells the owners when refused
and held calls pile up.

Everything else (webhook signing, `job.*` in full, run records): [the jobs
plugin's manual](../../rook/plugins/jobs/docs/manual.md).

## Timers inside a plugin

A plugin that only needs its own timer doesn't need a job: its manifest's
`hooks` run code `every: [{interval: 30m, run: ...}]` or `on: [{event: ...,
call: ...}]` ([Writing a plugin](authoring.md#hooks-and-events)).
