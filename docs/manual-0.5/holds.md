# Holds, hard stops and the why

Most calls just run. A few wait for a person: the hub **holds the exact
call** and asks once. Approving runs that same call (same cap, worker and
arguments); refusing is final. Nobody can sneak a different call through
an approval.

## Every call needs a why

Each call carries `why`: one plain sentence saying why it is made, like
"Check how long kaiju has been up." A call without one is refused
(`denied: why required`). The why is signed into the ticket, journaled
with the call, and shown to you when the call waits for you. It is the
caller's own words: it never grants anything.

## What waits for you

**Hard stops** always wait, and only you can release them, from a place
where you are there yourself: the dashboard's Approvals page, or
`POST /holds` with your own token. Not an agent acting for you, not the
security agent, and not a job or plugin acting as you (the
`approval.decide` cap never answers a hard stop):

- a cap that erases data for good (`destructive`, or an effect the hub
  doesn't know),
- revealing a secret's value,
- sending as you to someone else (`sends_as`),
- spending money (`spends`),
- any change on a machine you marked **sacred** ([Workers](workers.md#sacred-machines)).

**Other holds:**

- A child's first use of a cap waits for an owner ([Presets](people.md#the-four-presets)).
- A security gate may flag a call that looks out of place ([Security plugins](security.md)).

What can't run at all is **denied**, not held: a call outside what the
person, the agent, the session or the worker's ceiling allows comes back
with the layer that refused it, for example `denied: person: risk`.

## Answering

- **Dashboard**: the Approvals page lists what waits, with the cap, the
  machine, who asks and their why. Approve or refuse there.
- **HTTP**: `GET /holds`, then `POST /holds/<hold> {"decision": "approve"}`
  (or `"refuse"`).

A held call's caller gets back `{"held": true, "hold": "h_…"}`. Agents are
told to tell you the hold's name and stop; the call runs once you approve,
and its result is in the journal. Holds don't expire.

## Elevation is not a hold

When a cap needs administrator rights, the worker asks the hub for a short
lease inside the same call ([Workers](workers.md#normal-user-elevation-on-request)).
The hub checks rights and journals it; it does not ask you. Elevation
should be rare, and the security plugins watch it.

## For plugin authors

Choose effects honestly: they decide what waits. A cap that deletes a
torrent's files is `destructive`; one that only removes it from the list
is not. Put the irreversible part in its own cap (`torrent.delete` beside
`torrent.remove`) so the everyday one never has to wait.
