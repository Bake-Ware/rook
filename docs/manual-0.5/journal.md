# The journal

The hub writes one journal entry for every call that reaches dispatch,
whatever happened to it: allowed, denied or held, and a second entry when a
held call is approved or refused. It also records elevations, results that
arrive after a call timed out, and its own pruning. Only the hub writes the
journal; agents and workers can't.

## What an entry says

- when, which worker (or device), which cap;
- who: the person, the agent, or both;
- the session, and the project it was working for;
- the **why**;
- the cap's risk, scope and effects;
- the decision (`allow`, `deny` and which layer refused, `held`,
  `released`, `refused`), each security gate's reading;
- the outcome: ok or the error, and how long it took.

Arguments are kept with secret values replaced; a hash of the exact
arguments ties the entry to the ticket that ran.

## Reading it

- **Dashboard**: Journal (under More), searchable by cap, worker, person
  and project.
- **Agents**: the MCP `journal` tool, or the cap `journal.search` on `rook`.
- **HTTP**: `GET /journal?cap=shell.exec&worker=kaiju&limit=20` (filters:
  `call`, `cap`, `worker`, `person`, `agent`, `project`, `kind`, `session`,
  `chain`, `since`, `until`, `limit`).

Owners read everything. A person reads their own calls. An agent reads its
own calls and the history of the projects its sessions work for. A session
names only a project its caller may see: an owner names any; anyone else
needs rights to read projects and the project on the workbench (by id or
slug), else the session is refused (`denied: project: ...`).

`journal call=<id>` is how you find out what happened to a call that timed
out or was held: a late result is journaled when it arrives.

## Tamper-evident

Entries are chained: each one carries the hash of the one before it, one
chain per project plus a `general` chain for work outside any project. Rows
can't be updated or deleted in the database, and the hub re-checks every
chain hourly; a break is logged as `journal chain broken`. This protects
against an agent quietly covering its tracks, not against someone with root
on the hub's machine.

## How long it is kept

The `general` chain is pruned a day at a time once the day is 90 days
old, whatever was in it. Project chains are meant to go whole, some time after the project
closes; that step isn't wired to the workbench yet, so project chains are
kept for now.

A 0.4 journal brought over by the migration is kept read-only beside the
0.5 chains ([Moving from 0.4](migrating.md)).
