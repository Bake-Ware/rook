# Projects

A project is ongoing work with a goal; its tasks point at it (`task.project`).
`concepts` tags it with the kinds of work it is (software, hardware,
creative writing...): a project can carry several, and `concept.pool` gives
the experts, skills and sign-off norms they bring.

Caps: `project.list`, `get`, `search`, `create`, `update`, `archive`,
`history` (records kit; updates need the current `revision`). Each project
also has a whiteboard for the higher-level picture (`whiteboard.read
project=<id>`).

A project's **trust level** (how much ceremony a change gets) is kept by the
trust plugin in the agents package (`trust.level.*`); without it every
project is `normal`.
