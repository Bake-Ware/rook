# Change records

A lasting change (a deploy, a config or database change, a plugin install, a
trust or grant edit, a delete) gets a **change record**: what changes, why,
what could break, how it is tested, how to undo it, and the steps someone
else could follow. Writing one makes you think the change through; the
records are the trail the next person or agent reads to learn why things are
the way they are. What counts as a lasting change is guidance, not a hard
rule.

A record gates nothing. Write it, then make the change with ordinary calls
(they are journaled and held like any other: a sacred machine, a hard stop,
the security gate), then write how it went with `change.update`. All of it is
caps on worker `rook`; put `changes` in the hub's library.

## How much a record says

It follows the project's trust level (`project.trust`, set by an owner):

| Level | The record needs |
|---|---|
| `ship-it` | `what` and `why` |
| `normal` (also: no project, or no workbench) | also `risk`, `testing`, `rollback` and `steps`, a sentence each at least |
| `sacred` | the same as normal |

```
change.create what="Deploy pianobar 2.3" why="Fixes the skip bug." project=pianobar
  risk="Playback stops if the new build won't start."
  testing="pianobar --version says 2.3 and a song plays."
  rollback="Restart on 2.2 (kept in /srv/pianobar-2.2)."
  steps="Pull 2.3, restart the service, check it answers."
  workers=[nas] caps=[shell.exec]
```

Then `change.update id=... revision=... outcome="Deployed, plays."`.

## Automating it

When applying, testing and undoing a change are automated, they are a job
(the jobs plugin: a script with its steps and branching) and the record links
it: `job` (applies and tests) and `rollback_job` (undoes). Rolling back is
`job.run` on the rollback job.

## Afterwards

- `change.list`, `change.get`, `change.search`, `change.history`: records are
  band-wide and name their project, task, workers and caps, so an agent
  starting work can pull recent changes and their reasons first.
- `change.update` writes the outcome or corrects the words; `change.history`
  keeps every revision.

Core writes a record here for plugin pushes (`category: plugin`) and core
rollouts (`category: update`) when this plugin is installed. Without it they
work the same and the journal is their trail.
