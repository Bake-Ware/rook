# People and presets

Everyone who uses the hub is a **person** with an account, and every
person has a **preset**: a canned set of rights. Agents have identities of
their own. An agent working for a person can do only what **both** may.

## The four presets

| Preset | Can use | Highest risk | Effects allowed | Also |
|---|---|---|---|---|
| `owner` | everything | no limit | all | owns every band of the hub; manages people, plugins, bands and ceilings; the only one who pushes plugins |
| `adult` | everything | `admin` | all | |
| `child` | talking to agents (`agent.chat.*`) and viewing pages (`page.view`) | `write` | none | a child gets more only when an owner adds a grant |
| `guest` | talking to agents | `write` | none | the account stops working 7 days after it was made |

"Effects allowed: none" means a child or guest can never use a cap that
touches the physical world, personal data, money or secrets, even if a
plugin or an owner grants the cap itself ([Caps](caps.md#effects)).
Plugins add kid-safe caps to a preset in their manifest
(`presets: {child: {grants: ["homework.item.*"]}}`, only inside their own
namespace); an owner can grant a person more caps. Neither can lift the
preset's risk and effects limits.

## Adding a person

People live in the hub's config, `hub.yaml`:

```yaml
users:
  bake:   { preset: owner }
  autumn: { preset: adult }
  milo:   { preset: child, grants: ["lights.set@milo-room"] }
  sam:    { preset: guest, created: 1793400000 }   # the 7 days count from created (Unix time)
presets:
  child: { max_risk: read }                        # change a preset for this hub
```

A grant is a pattern: `deluge.*` (every cap of a plugin), `light.set@downstairs-*`
(a cap on matching machines or devices). `*` is the only wildcard. Give a
guest a `created` time: without one, the 7 days have long passed.

Restart the hub after editing people, then mint the person a sign-in
token:

```sh
python -m rook.core2.hub --config hub/hub.yaml token --person autumn --label "autumn's laptop"
```

The token is printed once; the hub keeps only its hash, in
`<data>/tokens.json`. Tokens minted while the hub runs work at once. To
revoke one, delete its entry (find it by `label`) from `tokens.json` and
restart the hub. Taking a person out of `users:` ends all their tokens.

## Agents

Each agent the hub knows is listed with its own rights:

```yaml
agents:
  claude-code: { grants: ["*"], max_risk: exec }
  codex:       { grants: ["shell.*", "file.*", "task.*"] }
```

```sh
python -m rook.core2.hub --config hub/hub.yaml token --person bake --agent claude-code
```

That token is **claude-code acting for bake**: each call gets only what
both bake and claude-code may. A token with `--agent` and no `--person` is
an agent acting alone, on its own rights (an unattended agent; the journal
names the agent, not a person). Agent tokens can't sign in to the dashboard.
Connecting an agent: [MCP for agents](mcp.md).

The hub's own agents (the house agent, the archivist and the rest) are
personas with their own grants ([Agents and personas](agents.md)).

## Sessions

Every call belongs to a **session**, which is one more layer that can only
narrow. An agent may start one that declares the caps it will use and the
project or task the work is for; calls in it can't reach other caps, and
their journal entries carry that project ([MCP for agents](mcp.md#sessions)).
Sessions end after 12 hours. A session that has read outside content (a web
page, an email) is marked tainted for good, and the security gate treats
its risky calls with more care ([Security plugins](security.md)).
