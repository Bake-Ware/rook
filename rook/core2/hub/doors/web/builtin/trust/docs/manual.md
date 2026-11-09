# Trust

What agents may do to what, in one place: the 0.4 **Trust map** page on
0.5's model (Bake, Nov 9: "Let's bring over the trust map ... pages too").

| Cap | What it does |
|---|---|
| `trust.map` | Everything in one read: `security` (`off`/`on`), `hard_stops`, `machines` (each with `level` sacred or normal), `projects` (each with its `project.trust` level; normal when it has none), `agents` (each with its `grants`), `waiting` (calls held for a person), `summary` (what agents are told, in plain words) and `history` (recent trust changes from the journal). A plugin that isn't installed (projects, agents) is listed in `missing`. |
| `trust.sacred` | `{worker, sacred}`: make a machine sacred (every call there that isn't a read is a hard stop: it waits on a person while security is on) or normal again (`sacred: false`). Kept in hub.yaml's `sacred:` list and applied at once. |

The **Trust map** page (sidebar, Trust) shows the map and changes it:

| 0.4 | 0.5 |
|---|---|
| Entries (host, project, ...) at sacred / guarded / ship-it | **Machines** (sacred or normal: `trust.sacred`) and **Projects** (sacred, normal or ship-it: `project.trust.*`) |
| Everything else | Everything not marked: normal |
| Exceptions | **What agents may do**: each agent's grants (`agent.update`) |
| Notices and proposals | Calls waiting on a person, with a link to Approvals |
| The person's words with every change | An optional "Why" line, sent as the change's why (journaled) |
| History | Trust changes from the journal (`trust.sacred`, `project.trust.*`, `hub.security`, agents' grants) |

The security switch (`hub.security`) is on the page too. 0.4's borrowed
state ledger and normal-state baseline have no 0.5 counterpart and aren't
shown.
