# Concepts

A concept is a kind of work and its base context: software, hardware
engineering, game development, admin, a wedding playlist. Many projects over
time share one, and a project can carry several (embedded firmware is
software and hardware), so concepts are tags, not parents.

A concept sets the pool of experts a project draws on: `roles` (agent
personas), `skills` (skill pages in knowledge), `signoff` (`none`,
`experts`, or `user` when the person signs off too) and the `trust` level new
projects start with. A playlist needs no security advisor; a comic book
needs no finance.

Caps: `concept.list`, `get`, `search`, `create`, `update`, `archive`,
`history`, and `concept.pool concepts=[...]`: every role and skill of those
concepts, the strictest sign-off and trust among them, and each concept's
`doc`. Unknown slugs come back under `missing`.

When a new kind of activity becomes real work, the agent that notices asks
how the person likes to work, finds out how practitioners do it, and writes
the concept. Plugins can ship concepts too (`from` says which).
