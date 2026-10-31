# Security plugins

Rook's protection comes in layers. The core ones are always on: every call
needs a why and a signed ticket, rights only narrow, workers keep their own
ceilings, and hard stops wait for you ([Holds](holds.md)). On top of that,
**gates** look at each call at the hub before its ticket exists. Gates come
from plugins; install the ones you want in the hub's library.

## The kill switch

Close a door and it refuses everything that comes through it: calls,
session starts, reads, and work already started through it (a plugin's
sub-calls). Closing `mcp` also refuses every agent's token on the other
doors, so an agent can't walk around it. Owners close and open doors with
`POST /doors/mcp {"open": false}` (or the `hub.door` cap); a hub component
such as the security plugin may close one but never open it. A closed door
stays closed across restarts; `hub.status` lists them.

## How gates work

- A gate answers `allow`, `flag` or `block` for the calls it watches.
  `block` refuses the call (`denied: blocked by gate <name>: <reason>`);
  `flag` holds it for you. Gates can only restrict, never grant.
- A gate in **shadow** mode is journaled and changes nothing. Gates ship in
  shadow mode until you trust them; to switch one, set its manifest's
  `gate: {mode: enforce}` in the library and restart the hub. Changing or
  removing a gate is an owner's act.
- A gate that errors or is too slow counts as a flag, so a broken gate
  can't wave calls through. The hub never waits more than 200 ms for one.
- Every gate's reading is in the call's journal entry (`gates`).

## `security`: canaries, taint and the pinned task

Ships enabled. Canary hits block from the start; the taint checks start in
shadow (they journal what they would have held and let the call go) until
an owner sets the plugin's `heuristics` setting to `enforce`. It reaches you
only when something is actually wrong.

- **Canaries**: decoy secrets nobody should ever use. Plant one where an
  intruder would look; any call that carries it, returns it or names its
  file is refused, and the session and agent behind it are paused until an
  owner clears them in person.
- **Taint**: once an agent's session has read outside content (a web page,
  an email), its risky calls are flagged unless they match the session's
  pinned task, and persistence or sending data out always is. In shadow
  (the default) a flag is only journaled and counted in `security.status`
  (`would_hold`); with `heuristics: enforce` the call waits for you.
- **Elevation**: a call that asks for admin rights mid-run goes through the
  gates again as an admin call, and is refused unless they pass it.
- **Triage**: the `security` agent looks at every call this gate holds and
  may release one call when it can show a false alarm. It can never
  release a hard stop, and it can release at most 5 calls an hour.

Details and caps: [the security plugin's manual](../../rook/plugins/security/docs/manual.md).

## `security-ml`: the classifier

A small model that scores every call against what usually happens on your
band, in well under a millisecond on the hub's CPU. Ships in **shadow**
mode. It retrains nightly from the journal, never from flagged calls, and
replaces its model only after three passing nights. `security-ml.explain`
shows what it would say about a call without running it.

Details: [the security-ml plugin's manual](../../rook/plugins/security-ml/docs/manual.md).

## Writing a gate

A plugin's manifest declares it:

```yaml
gate:
  run: scripts/gate.py:check        # gets the call, the caller, the why, the session
  match: { risk: [write, exec, admin] }
  budget: 20ms
  mode: shadow
```

The function returns `{verdict, confidence, reason}`; the reason is one of
`intent_mismatch`, `anomaly`, `persistence`, `exfiltration`, `injection`,
`canary`, `taint`, `policy`, `other`. A `result:` function may also look at
what a call returned and withhold it. The contract is in
`docs/spec/core-v2.md` section 10.2.
