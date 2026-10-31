# Approvals

Calls the hub held for a person's answer, and the answer. A call is held
when it hits a hard stop (erasing data for good, revealing a secret,
sending as someone, spending money, a change on a sacred machine), when a
child uses a cap for the first time, or when a security gate flags it.

| Cap | What it does |
|---|---|
| `approval.list` | Calls waiting for a person: the cap, the machine, who asks and their why. Owners see all; anyone else the ones made for them. |
| `approval.decide` | `{hold, decision: approve \| refuse}`. `approve` runs exactly that call now, with a fresh ticket; `refuse` is final. A question: `{hold, answer}`. |
| `approval.ask` | `{question, options?, wait?}`: ask the person something; waits up to `wait` seconds for the answer. |
| `approval.get` | `{hold}`: one of your questions, with its answer once there is one. |

Only a person answers. An agent acting for a person answers as the agent,
so it can never release a hard stop. A hard stop is answered only where
the person is there: this page (the site answers it for them, not through
the cap) or `POST /holds/<hold>` with their own token. The
`approval.decide` cap never answers one, so a job or hub plugin acting as
the person can't. Holds don't expire.

The **Approvals** page (in the sidebar) lists them with Approve and Refuse
buttons and updates as holds come and go. The `approval/holds` component
("Waiting for you") puts the same list on any page.

## Questions

Any agent, an external one over MCP too, asks the person through the same
path: `approval.ask {question, options?, wait?}`. The question waits on the
Approvals page beside the held calls (`q_...` ids), and with the agents
package the house agent hears about it (`approval.asked`) and gets it to the
person the way that fits, then gives their answer back. Nothing runs on an
answer: it goes to the asker, at once if it is still waiting, else
`approval.get {hold}`. A question is asked of the person the asker acts
for (an agent acting for them may relay their words, and is recorded as
who answered); with nobody behind the asker, of the owners. An agent never
answers its own question. Questions don't expire; a hub restart drops
them, like holds.

Over HTTP: `GET /holds` and `POST /holds/<hold> {"decision": "approve"}` (a
question: `{"answer": "..."}`).
