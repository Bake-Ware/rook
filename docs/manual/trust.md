---
id: trust
title: The trust map
order: 10
features:
  - plugin/hub/trust
since: 0.2.0
deprecated: false
preview: true
---
# The trust map

The trust map is your short list of what agents may change on their own.
Each project, machine, repository or resource on it has a level, set in your
own words, and the hub checks every agent call against it. An agent can't
talk its way past it: the hub enforces it, not the agent's instructions.

Open it on **Manage > Trust**.

## The three levels

- **Sacred:** changed only on your direct instruction. Agents may read it,
  propose changes and prepare work, but any change waits for you. Example:
  "kaiju changes are sacred, only ever under my direct direction."
- **Guarded:** agents ask first. Later, a panel of roles will vote, act and
  then tell you; until that exists, a guarded change waits on the
  **Approvals** page like any held call.
- **Ship it:** agents just go. Example: "just ship it, worst case the music
  stops."

What happens to one action depends on two things: what kind of action it is,
and the strictest level of what it touches.

- **Reads** always go, on every level. A device's battery, model, Android
  version, uptime, app version and update progress count as reads on any
  device, even when an older app still calls them something else.
- **Preparing** always goes: writing tasks, knowledge pages, chat and huddle
  messages on the hub.
- **Changes** (running commands, writing files) go on ship-it, ask on
  guarded, and wait for you on sacred.
- **Admin and destructive actions** (updates, deletes, settings) are the
  same, and they still ask on ship-it things whenever the band's own
  approval rules hold them. Ship-it never removes a hold you already had.

Anything not on the map is **Everything else**. It starts as *balanced*:
changes go and admin actions ask, which is how Rook behaved before the map
existed. You can make it guarded or sacred on the Trust page.

The map holds even when approval holds are switched off (Settings > Hub >
Approvals): that switch turns off the blanket holds on risky actions, not
the entries you set here.

## What you see when the map stops an agent

A held call shows on **Approvals** with the entry it came from, for example
*trust map v7, entry "kaiju" (sacred), set by Bake: "kaiju changes are
sacred..."*. The agent gets the same line. Approve it and that one call runs;
your approval is your direct instruction. Every call the map ruled on carries
the same citation in the journal.

## Changing the map

Only you loosen the map, and only on the Trust page. Every change asks for
your words, and the history keeps who changed what, when, and what they said.

- **Levels:** tap a level on an entry to change it.
- **+ Entry:** add a project, machine, repository or resource. "What it
  covers" takes one line per target: `worker:kaiju`, `repo:Bake-Ware/rook#master`,
  `cap:hub.plugin.*` or `project:pianobar-super`.
- **Exceptions:** let agents go on something for a while, then stop on their
  own. An exception can cover one entry, a few, or everything, and can be
  narrowed to certain targets, such as "this experiment may run its own
  containers on kaiju". It never covers the entries listed under "Never
  covers" (your family devices, say). **End now** stops one early.
- **Not confirmed:** an entry someone set from your words on your behalf (an
  agent seeding the map, say). Tap **Confirm** if it's right, or change it.

Agents can't change the map. They can **propose** a change, with a reason;
proposals show at the top of the Trust page with **Accept** and **Reject**.
The house agent may **tighten** the map on its own (raise a level, add a
guarded or sacred entry, end an exception), for example when you tell it
"never without me". You see each of those at the top of the page until you
tap **OK**. Nothing relays a change to the map.

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
Taking a tool away is a tightening, so the house agent may always do that.

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
credentials, so it can't merge either.

## For agents

`rook_trust(action="summary")` gives the short version agents get in their
context; `action="check"` with `{"cap": "shell.exec", "worker": "kaiju"}`
gives the ruling and its citation; `action="propose"` asks you for a change.
The map itself is `trust.json` in the hub's data folder, beside
`policy.json`, with its history in `trust-history.jsonl`.
