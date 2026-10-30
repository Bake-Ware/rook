# Projects

A project is ongoing work with a goal; its tasks point at it (`task.project`).
`concepts` tags it with the kinds of work it is (software, hardware,
creative writing...): a project can carry several, and `concept.pool` gives
the experts, skills and sign-off norms they bring.

Caps: `project.list`, `get`, `search`, `create`, `update`, `archive`,
`history` (records kit; updates need the current `revision`). Each project
also has a whiteboard for the higher-level picture (`whiteboard.read
project=<id>`).

**Trust level** (`project.trust.*`, admin): how much ceremony a change gets.
`sacred` = a full change record and the owner signs off; `normal` = a full
record, then it proceeds; `ship-it` = a one-line note of what and why. It is
its own record so that an agent allowed to edit a project can't loosen it;
no level means `normal`.
