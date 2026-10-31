# Journal

Search the hub's journal: what was called, by whom, why, and what came of
it.

`journal.search` takes any of `cap`, `worker`, `person`, `agent`, `project`,
`kind`, `session`, `since` and `until` (Unix time) and `limit` (default 50,
at most 500), and returns entries newest first. Owners read everything; an
agent reads its own calls and its projects' history; a person reads their
own calls.

The **Journal** page (under More) searches it. The `journal/recent`
component shows the latest entries on any page, optionally for one `cap`
or `worker`.

What an entry holds, the hash chains and how long entries are kept: the
manual page "The journal" (`docs/manual-0.5/journal.md`).
