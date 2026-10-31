# Change records

A lasting change (a deploy, a config or database change, a plugin install, a
delete) gets a **change record**: what changes, why, what could break, the
plan, and the caps that do it, test it and undo it. Writing one makes you
think the change through; the records are the trail the next person or agent
reads to learn why things are the way they are. All of it is caps on worker
`rook`; put `changes` in the hub's library.

## How much a record says

It follows the project's trust level (`project.trust`, set by an owner):

| Level | The record needs | Runs |
|---|---|---|
| `ship-it` | `what` and `why` | on its own |
| `normal` (also: no project) | also `risk` and `plan`; with scripts, `tests` and a `rollback` | on its own |
| `sacred` | the same as normal | after an owner's card |

A risky change may have no rollback (some changes have no way back). Scripts
marked `atomic` check and undo themselves (like `core.plugin.install`), so
they need no tests.

## Submitting

```
change.submit what="Deploy pianobar 2.3" why="Fixes the skip bug." project=pianobar
  risk="Playback stops if the new build won't start."
  plan="Pull 2.3, restart the service, check it answers; restart on 2.2 to undo."
  checks=[{worker: nas, cap: shell.exec, args: {argv: [test, -d, /srv/pianobar-2.2]}}]
  scripts=[{worker: nas, cap: shell.exec, args: {cmd: "deploy 2.3"}}]
  tests=[{worker: nas, cap: shell.exec, args: {argv: [pianobar, --version]}, expect: "2.3"}]
  rollback={worker: nas, cap: shell.exec, args: {cmd: "deploy 2.2"}}
  wait=true
```

Each step is a cap call on a worker, made through dispatch **as you** (your
rights, the gates and the journal apply), with the change's why and its id.
A standard change runs in the session you submitted it in, so a session
narrowed to a few caps stays narrow.
A step fails when the call fails or its result says `ok: false`; `expect` is
text its result must contain.

- **standard** (`kind: standard`, the default; routine and reversible): the
  hub runs the checks, the scripts, then the tests. A failed check changes
  nothing (`refused`). A failed script or test runs the rollback
  (`rolled_back`, or `failed` when the rollback fails too or there is none,
  and the owners are told). A first script that never ran (dispatch refused
  it) or is `atomic` changes nothing (`refused`). All tests pass: `kept`.
- **risky** (`kind: risky`: permanent, or could hurt), and anything in a
  sacred project: `asked`. The hub holds `change.apply`, a hard stop, so an
  owner gets one card; approving it runs the change, refusing it ends it
  (`declined`).
- **no scripts**: the record is the change (`noted`): something done by hand.

A machine on the sacred list can't be in a change: every call there waits for
the person on its own. A step that comes back held stops the change where it
is (`failed`, nothing rolled back, since that call may still run when someone
answers it).

## Afterwards

- `change.list`, `change.get`, `change.search`: records are band-wide; each
  names its project, task, machines and caps, and logs every step's result.
  Only the plugin writes them.
- `change.revert id=...`: run a kept change's rollback, as you (its author or
  an owner).
- A hub restart: a change that was running is rolled back if a script had
  started, else `refused`; a change waiting for its card gets a new one.

Plugin pushes (`POST /plugins/push`) are change records too: the install is
the script, `core.plugin.rollback` the way back.

Setting `rejoin` (default 5m): how long a step waits for its worker to be on
the band, since a deploy may restart it.
