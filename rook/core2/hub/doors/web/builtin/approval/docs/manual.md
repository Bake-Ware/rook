# Approvals

Calls the hub held for a person's answer, and the answer. A call is held
when it hits a hard stop (erasing data for good, revealing a secret,
sending as someone, spending money, a change on a sacred machine), when a
child uses a cap for the first time, or when a security gate flags it.

| Cap | What it does |
|---|---|
| `approval.list` | Calls waiting for a person: the cap, the machine, who asks and their why. Owners see all; anyone else the ones made for them. |
| `approval.decide` | `{hold, decision: approve \| refuse}`. `approve` runs exactly that call now, with a fresh ticket; `refuse` is final. |

Only a person answers. An agent acting for a person answers as the agent,
so it can never release a hard stop. Holds don't expire.

The **Approvals** page (in the sidebar) lists them with Approve and Refuse
buttons and updates as holds come and go. The `approval/holds` component
("Waiting for you") puts the same list on any page.

Over HTTP: `GET /holds` and `POST /holds/<hold> {"decision": "approve"}`.
