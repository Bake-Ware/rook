---
id: trust
title: The trust map
order: 10
features:
  - plugin/hub/trust
  - plugin/hub/changes
  - settings/change
since: 0.2.0
deprecated: false
preview: true
---
# The trust map

Agents just get on with their work, and the journal records every call, so
you can always see what they did. Only a few things stop and ask you. The
hub enforces them, not the agent's instructions, so an agent can't talk its
way past them.

Open the map on **Manage > Trust**.

## What always asks you

The hub asks you before an agent:

- **Changes anything on a sacred machine** (kaiju, say). Reading there is
  fine. A command sent to a sacred machine over ssh, scp or rsync counts as
  a change there.
- **Erases data for good**: wiping a disk or file system, dropping a
  database or table, deleting a database file, deleting a top-level, home
  or service-data folder with everything in it, forgetting a memory,
  deleting a secret, deleting a chat room it doesn't own, or deleting a job
  or role.
- **Reveals a secret**: reading a secret's value, putting a secret (or a
  `{{secret:...}}` placeholder) in a message, notification or speech, or
  touching the hub's own vault and guardrail files (`vault.key`,
  `vault.db`, `trust.json`, `policy.json`, `approvals.db`, `settings.db`).
- **Sends a message as you** to other people: a text, or a reply to a
  notification on your phone.

Everything else just goes. People are never held. These stops hold even
with **Hold risky agent calls** switched off (Settings > Approvals).

The checks on commands look for known patterns. They catch mistakes; they
are not a sandbox.

## The levels

- **Sacred:** changed only on your direct instruction. Agents may read it,
  propose changes and prepare work. Example: "kaiju changes are sacred,
  only ever under my direct direction."
- **Guarded** and **Ship it:** agents just go. Guarded entries ask first
  only with the advanced security plugin on ([[manual-security]]).

Anything not on the map is **Everything else**, and it goes too. (With the
advanced security plugin on, it is *balanced*: admin actions ask.)

Your map is not changed by this. A trust proposal may offer to remove
entries that no longer do anything.

## When the map stops an agent

The ask shows on your phone and on **Approvals** with the entry it came
from, for example *trust map v7, entry "kaiju" (sacred), set by Bake:
"kaiju changes are sacred..."*. The agent gets the same line. Approve it
and that one call runs. The journal keeps the same citation.

## Only you add guardrails

Agents, huddle roles and the house agent can never add or tighten
guardrails, holds or trust entries. They can't change the policy, the
approval or security settings, or the advanced security switch either: the
hub refuses. Only you do, on the dashboard, or by voice when the voice is
sure it is you.

An agent may **propose** a trust change that loosens or rearranges the map,
never one that only makes it stricter. Reviewers suggest a safer way; they
don't block.

## Changing the map

Every change asks for your words, and the history keeps who changed what,
when, and what they said.

- **Levels:** tap a level on an entry to change it.
- **+ Entry:** add a project, machine, repository or resource. "What it
  covers" takes one line per target: `worker:kaiju`, `repo:Bake-Ware/rook#master`,
  `cap:hub.plugin.*` or `project:pianobar-super`.
- **Exceptions:** let agents go on something for a while, then stop on their
  own. An exception can cover one entry, a few, or everything, and can be
  narrowed to certain targets, such as "this experiment may run its own
  containers on kaiju". **End now** stops one early.
- **Not confirmed:** an entry someone set from your words on your behalf (an
  agent seeding the map, say). Tap **Confirm** if it's right, or change it.
- **Proposals** show at the top of the page with **Accept** and **Reject**.
  One proposal can hold several changes; you accept or reject them together.

## Borrowed state

When an exception lets an experiment change something sacred (stop a
service on a GPU, say), the map keeps a ledger row: what changed, why, who
allowed it, and exactly how to restore it. The Trust page lists open rows
under **Borrowed state**, with your words and until when; **Normal state**
lists each machine's recorded normal state. The house agent fills these in
when you start a project by talking to it and say it may borrow something:
see [[manual-onboarding]].

## Tool grants

When a role runs as an always-on session ([[manual-persistent-agents]]), the
MCP servers it was given are listed on the map as **tool grants** ("rea for
re-tech"), with who asked and their words. The house agent adds one only when
you ask for it yourself; another agent's request waits for you as a proposal.

## Coding sessions can't push the main branch

A role's coding session pushes only its own branch and opens a pull request.
Three things stop it pushing `master`:

- The session's own rules refuse it.
- A push guard the hub installs for each session refuses any branch but its
  own.
- GitHub's branch protection on `master` refuses direct pushes from anyone,
  administrators included: every change goes through a pull request.

When a GitHub App is set up (Settings > Roles: session GitHub App id,
installation and private key), the hub mints each session a short-lived
token for its one repository and runs the session without your own GitHub
credentials, so it can't merge either. Merges happen at the conference
table ([[manual-conference-table]]).

## Change requests

A change that could hurt goes in as one **change request**. It says what
changes and why, on which machines, the plan, the commands to run, the
tests (commands and the result they should give) and how to roll it back:
the command, a dry run of it, and a check that the snapshot or rollback
point exists. Reads, audits, collecting information and routine job runs
need no change request.

There are three kinds:

- **Standard:** routine deploys, restarts, updates, rollbacks, failovers,
  and config or database changes you can undo. The hub checks that the
  request is complete, that the rollback point exists and that the rollback
  dry run works, and runs the tests first. Then it makes the change, tests
  it again, and keeps it or rolls it back on its own. Nobody has to say yes.
- **Emergency:** a service is down. A standard fix (roll back the last
  change, restart, fail over) runs at once and you get one note. Any other
  fix comes to you as one urgent card. The agent writes up the rest
  afterwards.
- **Normal:** risky or permanent, such as retiring a server or deleting a
  database. A huddle gets one round (an hour) to turn it into standard
  changes: move it aside now and delete it later, narrow it, try it on a
  test hub or one machine first, make it undo itself after a while, or take
  a snapshot first. The huddle lowers the risk; it doesn't approve or
  reject. What is left comes to you as one card. **Yes** runs it with its
  tests and rollback; **No** drops it.

A "standard" request that names a sacred machine, hits one of the stops
above, or isn't one of the standard kinds is treated as normal.

Hub updates are standard changes. The hub takes a snapshot first, checks
that the previous release is still there to go back to, deploys, then
checks its health and version. If those fail, it rolls back on its own.

### The digest

Each morning a quiet phone notification (with a copy to your chat bridges)
lists the change requests that ran, rolled back, failed or wait for you. On
Mondays it is a weekly review. Reply to the house agent:

- "revert <name>" rolls a kept change back.
- "<kind> is fine" makes that kind a standard change from now on. Only your
  words add a kind.

A change that failed and couldn't roll back rings your phone at once.

Choose how often in Settings, group **Changes**: **Change digest**
(`change.digest`): daily (the default, since agents work overnight),
weekly, or off. Every result is also one line in the 30-day log
([[manual-phone-attention]]).

## For agents

`rook_trust(action="summary")` gives the short version agents get in their
context; `action="check"` with `{"cap": "shell.exec", "worker": "kaiju"}`
gives the ruling and its citation; `action="propose"` asks you for a change.
Change requests are caps on worker `rook`: `change.submit`, `change.update`
(an emergency's write-up afterwards), `change.list`, `change.get`,
`change.revert` and `change.digest`. The map
itself is `trust.json` in the hub's data folder, beside `policy.json`, with
its history in `trust-history.jsonl`.
