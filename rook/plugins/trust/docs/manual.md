# Trust

What agents may do to what, in one place: the 0.4 **Trust map** page on
0.5's model (Bake, Nov 9: "Let's bring over the trust map ... pages too"),
in the agents package: put `trust` in the hub's library.

| Cap | What it does |
|---|---|
| `trust.map` | Everything in one read: `security` (`off`/`on`), `hard_stops`, `machines` (each with `level` sacred or normal), `projects` (each with its `trust.level`; normal when it has none), `agents` (each with its `grants`), `waiting` (calls held for a person), `summary` (what agents are told, in plain words) and `history` (recent trust changes from the journal). A plugin that isn't installed (projects, agents) is listed in `missing`. |
| `trust.level.list`, `get`, `create`, `update` | A project's trust level, one record per project (`project` is its id): it decides who signs off before a task's work lands: `sacred` = a reviewer and the owner; `normal` = one reviewer other than the author; `ship-it` = nobody. No record means `normal`. Creating and changing one is admin, so an agent allowed to edit a project can't loosen it. Tasks and github read it; without this plugin every project is `normal`. |

The **Trust map** page (sidebar, Trust) shows the map and changes it:

| 0.4 | 0.5 |
|---|---|
| Entries (host, project, ...) at sacred / guarded / ship-it | **Machines** (sacred or normal: core's `hub.sacred`) and **Projects** (sacred, normal or ship-it: `trust.level.*`) |
| Everything else | Everything not marked: normal |
| Exceptions | **What agents may do**: each agent's grants (`agent.update`) |
| Notices and proposals | How many calls wait on a person |
| The person's words with every change | An optional "Why" line, sent as the change's why (journaled) |
| History | Trust changes from the journal (`hub.sacred`, `trust.level.*`, `hub.security`, agents' grants) |

The security switch (`hub.security`) is on the page too. 0.4's borrowed
state ledger and normal-state baseline have no 0.5 counterpart and aren't
shown.
