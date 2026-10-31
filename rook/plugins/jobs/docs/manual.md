# Jobs

A job does one thing on a trigger: calls a cap, asks an agent, or runs a
Python script. One run record covers the whole flow; branching is just code.

## Triggers

| Trigger | Fires |
|---|---|
| `{cron: "0 8 * * 1-5"}` | on a five-field schedule in the `timezone` setting (default UTC); `@daily` and friends work |
| `{every: 5m}` | every interval (1s or more) |
| `{on: worker.online, filter: {name: kaiju}}` | on an event (a glob); `filter` keys match the payload (dotted paths; a list means any of) |
| `{manual: true}` | only by `job.run` (any job can be started by hand) |
| `{webhook: true}` | on a signed POST to `/p/job/hook?job=<id>` |

Jobs never fire on `job.*` events (their own records), so they can't loop
on themselves.

## Who it runs as

Every call a job makes goes through dispatch like any other call: why,
rights, gates, holds, journal. It runs as:

- its **creator** (the default): the person, or the agent acting for that
  person, who made it, with their rights as they are at each run;
- a **persona** (`as: persona`, `persona: archivist`): `agent:<persona>`
  with that agent's own grants. Owners only.

Scripts run on the hub, and events show what happens band-wide, so script
jobs and `on` jobs are for owners too. Jobs and their runs are private: their
person and the owners see them.

## Scripts

```python
def main(job):
    files = job.call("file.list", {"path": "/srv/backups"}, on="nas", why="see last night's backups")
    if not files["entries"]:
        if job.ask("No backup last night. Run one now?", ["yes", "no"]) == "yes":
            job.retry(lambda: job.call("shell.exec", {"argv": ["backup.sh"]}, on="nas"), times=3)
        job.notify("Last night's backup was missing.")
    return {"files": len(files["entries"])}
```

`job.call(cap, args, on=worker, why=...)`, `job.notify(text, title=, to=)`
(default: the job's person; see "Notices"), `job.ask(question, options, timeout="1h")`
(asked with `approval.ask`, so it waits on Approvals and reaches the
person through the house agent; None on timeout, at most an hour), `job.wait("5m")`,
`job.retry(fn, times=3, wait="10s")`, `job.parallel(fn, fn, ...)`,
`job.log(...)` (and `print`). `job.vars`, `job.event` / `job.payload`,
`job.last` (when the last good run started), `job.trigger`, `job.attempt`.
A failed call raises `job.Error`. The return value is the run's result.

The script runs in its own Python process (standard library only), as the
hub's normal account, in a scratch folder that goes when it ends.

## Notices

A job doesn't pick how to reach the person. Its notices (`notify:` and
`job.notify`) go to the house agent (the `default_agent` setting), which
gets word to them the way and at the time that fits: a voice note, the
desktop, a chat, or holding it until morning. On a hub without the agents
package, or with `default_agent` blank, they go straight to `notify.send`.

## Failures

- `retries: 2` runs a failed job again (after `retry_after`, doubling).
- `notify: failure` (default) tells the person when a run fails for good;
  `always` after every run; `never`.
- `timeout` (default: the `timeout` setting, 10m) stops a run.
- One run per job at a time: a trigger that finds it running is skipped.
- No checkpoints. A run cut off by a hub restart is marked failed at the next
  start and then retried or reported like any failure, so write jobs that
  are safe to run twice. `catch_up: true` runs a `cron`/`every` job once at
  start when a slot was missed while the hub was down.

## Webhooks

`job.webhook` gives the path and the job's key. Sign the raw body:
`X-Rook-Signature: sha256=<hex HMAC-SHA256>` (GitHub's `X-Hub-Signature-256`
works as is). The parsed body is the run's `job.payload`.

## Ready-made jobs

Any plugin can ship jobs in its manifest (`jobs:`). `job.templates` lists
them; `job.install template=plugin/job` makes one yours (it runs as you).
A plugin's `enabled: true` job that runs as a persona is installed active;
edit it and plugin updates leave it alone. This plugin ships
`jobs/security-scan`: refused and held calls since the last scan, and a
notice to the owners when refusals pile up (`vars.threshold`, default 10).
