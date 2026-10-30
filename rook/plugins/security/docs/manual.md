# Security

The security plugin checks every call at the hub, before a ticket exists,
and stays out of the way: it blocks only on canary hits, holds only risky
calls that look out of place, and reaches you only when something is
actually wrong. It ships enabled; turning it off or swapping it is an owner
action and is journaled.

## Canaries

A canary is a decoy secret nobody should ever use. One is made on first
start (`backup-admin-token`); make more with `security.canary.create`
(owners in person, from the dashboard; an agent can't see or make them):

- `kind`: `token`, `aws`, `github` or `password`; `name` should look like a
  real secret's.
- The reply has the decoy's text. Plant it where an intruder would look
  (`~/.aws/credentials` for an `aws` one) with `file.write` within 10
  minutes: that one write is allowed. Add `planted: ["<worker>:<path>"]` to
  make naming that file a hit too.

A hit is the canary's value (or its base64, or `{{secret:<name>}}`) in a
call's args or why, in what a call returned, or a call naming a planted
path. The call is refused (`denied: blocked by gate security: canary`), a
result carrying it is withheld, and the session and the agent behind it are
paused: every later call from them is refused. You get one message through
the house agent, in plain words, and the triage agent looks into it.

If it was you testing, say "that was me": lift it with `security.clear`
`{session, agent}` (owners in person only; an agent can't clear itself).
`security.canary.list` shows the canaries and their hits, never the values;
`security.canary.retire` stops watching one.

## Taint and the pinned task

This part watches agents (prompt injection steers agents, not you or the
hub's own plugin code). An agent's session is tainted once it read outside
content (a web page, an email: caps with the `taints` effect). From then on:

- a risky call (exec or admin, or persistence, sensitive or destructive
  effects) waits for you unless it shares words with the session's pinned
  task;
- persistence (SSH keys, cron, services, new users, autostart entries) and
  sending data out or running fetched code (`nc`, `scp`, `curl -d @file`,
  `| sh`, `base64 -d`) always wait.

From a clean session only a high-impact call (admin, or persistence) that
has nothing to do with the pinned task waits. Everything else goes through,
and the gate's reading is journaled with the call.

## The triage agent

The `security` agent (agents package) is told about every call this gate
holds. When it can show a false alarm (the call matches the pinned task, or
the same caller made exactly that call there from a clean session before)
it releases that one call with `security.release`; the plugin checks the
evidence itself and refuses hard stops, persistence, sending data out,
canary hits, paused sessions and calls other gates also held. At most 5 releases an hour. The
rest wait for you on Approvals.

`security.status` shows canaries, paused sessions, holds and recent alerts.

## Settings

| Setting | Default | |
|---|---|---|
| `house_agent` | `house` | who reaches you about canary hits (else `notify.send`) |
| `triage_agent` | `security` | who is woken on holds; the only caller of `security.release` |
| `task_cap` | `task.task.get` | how a session's pinned task is read |
