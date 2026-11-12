# Security

Security is switched off on the hub and stays off until the hardening
pass. What is there today:

## The switch

One hub setting, `hub.security`, default `off`. An owner flips it on
**Settings** or with `setting.set {plugin: hub, key: security, value: on}`.

- `off`: nothing waits on a person. Each journal entry says
  `security: off`, and `would_hold` lists a hard stop the call would have
  waited on.
- `on`: the hard stops hold the call for you ([Holds](holds.md)).

## The hard stops

The only calls that ever wait: erasing data for good, revealing a
secret's value, sending as you to someone else, and any change on a
machine you marked sacred. See [Holds](holds.md#what-waits-for-you).

## The kill switch

Close a door and it refuses everything that comes through it: calls,
session starts, reads, and work already started through it (a plugin's
sub-calls). Closing `mcp` also refuses every agent's token on the other
doors, so an agent can't walk around it. Owners close and open doors with
`POST /doors/mcp {"open": false}` (or the `hub.door` cap). A closed door
stays closed across restarts; `hub.status` lists them.

## The journal and the vault

Every call is journaled with who made it and why ([The journal](journal.md)).
Secrets live in the hub's vault and are used by name; their values are
masked in the journal and never come back in a result
([Settings and secrets](settings.md)).
