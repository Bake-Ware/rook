# Permissions and signed role grants

Status: **implemented in audit mode** (rook-beta wave 2). The spec below is
unchanged; the next section records what the code does, where it deviates and
what is still open.

## Implementation notes (wave 2)

**Maintainer decisions applied.** Enforcement ships in shadow/audit mode:
decisions are computed and journaled, nothing is denied until the operator
sets `"mode": "enforce"`. The default policy reproduces today's behaviour.
Denying exec/admin to `unverified` callers is implemented but off by default
(one edit: `"unverified": {"exec": "deny", "admin": "deny"}`). Band settings
changes (`settings.set`/`settings.reset`) and `policy.set` are for band owners
and operator-role tokens: default rules deny them to `role:agent` and
`human:member` (in enforce mode), and `policy.set` also hard-gates on the
principal in its handler whatever the mode. The settings framework should
call `rook.hub.authz.require_hub_admin("settings.set")` the same way.

| Area | Code | Notes |
|---|---|---|
| Tier table, tier resolution, signed objects, grants, tickets, deauth v2, revocations, replay cache | `rook/core/authz.py` | Stdlib-only (PyNaCl loaded at runtime; verification fails closed without it). Ships in the worker bundle. |
| Policy document, compiler, evaluator, lint, store | `rook/hub/policy.py` | **JSON** at `policy.json` in the hub data dir (`$ROOK_DATA_DIR`, else next to the MCP stores); `policy.yaml` is read instead when present and PyYAML is installed. The settings framework can take over storage later. Reloaded on change; an invalid file keeps the last good revision. |
| E1: principal resolution, `authorize()`, tickets, journaling | `rook/hub/authz.py`, `BandClient.authz` / `MultiBandClient.authz` | The authorizer sits inside the band client, so every hub path is covered. Principals come from a context variable set by the MCP attribution wrapper (`token:<agent_id>`, `token:static`, `unverified`) and the dashboard principal middleware (`human:<user_id>`, `human:dashboard`); in-process hub code without one is `system:*`. `call(principal=…)` overrides. |
| Hub tools as caps on `rook` | `server._authorize_tool`, `hub_cap_for_tool` | Checked right after attribution. Tools that only forward to worker caps are authorized in the band client. |
| Journal | `calls.principal/decision/rule/policy_rev/tier` | `rook_call` puts the decision on its own row; other paths journal non-allow decisions (coalesced over 10 s). `rook_journal` shows the columns on non-allow rows only. |
| Token roles | `TokenStore.mint_api_token(role=…)`, `/tokens` and account token pages | Existing tokens are `agent`; the static token is `operator`. Only operator accounts may mint operator tokens from the account page. |
| Keys and grants | `rook/hub/keys.py` | `hub-op-key` beside the root key (mode 0600), rotated after 30 days with a 24 h overlap. Grants are issued in memory by each hub process (7 days, renewed daily, scoped to every band that process serves). |
| Signed hub announce, `rook` resolution, impostor quarantine | `BandClient._announce_local`, `_handle_announce`, `core.facts.roles_from_announce` | Replaces the reject-all stub. A remote announce naming `rook` without a held grant is listed as `rook~<id8>`, flagged `quarantined`, and journaled once as `audit.impostor`. |
| E2: worker ticket checks | `rook/worker/authz_guard.py`, `Worker._on_message` | All five modes; default `audit` (`ROOK_AUTHZ_MODE`). Keys learned from signed hub announces or inlined in the ticket; `authz` readiness in the announce; `audit.jsonl` gains `ticket` and `decision`. |
| Dashboard | `/permissions`, `/api/policy`, `/api/policy/explain` (`rook/remote/policy_web.py`); caps `policy.explain/get/set/status` on `rook` | A minimal page: mode, revision, lint, JSON editor, explain, recent non-allow decisions. The full editor (matrix, pickers, dry run, history) waits for the settings framework. |
| `rook band` TUI | `rook/cli/band_tui.py` (`BandHTTP`), `GET /api/band/whoami` | The TUI has no band path of its own: every call is `POST /api/band/call` (and `/api/band/ban`, `/unban`) with the dashboard login, so the dashboard principal middleware, the band client's policy check, ticket signing and journaling all apply. `whoami` reports the principal, the hub policy mode for it and whether the hub signs tickets. On a hub without it (404) the TUI warns that the hub predates permissions, and it adds a `hint` to refusals from hub policy or from ticket-enforcing workers. |

**Current-code gaps (6.3), fixed.**

1. `worker.deauth` accepts only a deauth v2 order (`rook-deauth-v2` domain,
   `worker_id` and `issued_at` required). The hub sends the v2 order nested
   under `v2` inside a legacy v1 body signed the old way, so build-167 workers
   still park and new workers verify only the v2 part.
2. `worker.update(url=…)` requires a signed OTA `manifest` (same verification
   as `worker.apply`, sha256 match, `--selftest`, no downgrades). The optional
   `manifest` argument is new; old workers never receive it.
3. Hub/PSK changes through `worker.reconfigure`, `worker.update` and
   `worker.config_apply` require a **signed order**, implemented as a verified
   call ticket: signed by the hub op key under a root-signed `is_hub` grant,
   bound to this worker, message id, cap and the exact args (so the new hub and
   PSK values are covered). The ticket travels in the envelope, not in the
   args, so build-167 workers are unaffected.

**Compatibility.** Every wire addition is an optional key old peers ignore:
envelope `ticket`, announce `grants`/`asig`/`ts`/`seq`/`authz`, deauth `v2`.
Known breaks, both on new workers only:

- A hub without the root signing key (or an old hub) can no longer repoint a
  new worker's hub or PSK over the band, and a new worker without a trust
  anchor (run from a source checkout, `ROOK_UPDATE_PUBKEY` unset) refuses such
  changes too. Set `ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT=1` on the worker to
  restore the old behaviour locally. Band migrations use the enrollment HTTPS
  path and are not affected.
- `worker.update(url=…)` without a signed manifest and legacy v1 deauth
  orders are refused. Old hubs sending v1 deauth orders to new workers fall
  back to the controller denylist only.

**Not done yet (follow-ups).** Per-argument predicates, on-behalf-of chains
from integrations (the evaluator supports chains; no integration builds one
yet), plugin principals from manifests, the worker local floor
(`policy.yaml` on the worker), hub-signed time / clock offset adoption,
fetching revocation lists (workers accept a signed list inlined in a hub
announce; the hub does not publish one yet), name binding against the device
registry (names bind by the live roster; the "weak binding" badge is not
shown), `{{secret:…}}` refusal for fact-only targets (calls are always
targeted by id today), `/api/band/ticket` (not needed by the TUI, which
routes every call through the hub's call API; only for future direct
scripts), the dry-run replay and history views, `denied: [...]`
annotations in `rook_workers`/`rook_caps`, and stage 6 device-key signed
announces. The hub node keeps its persisted `hub_node_id` as `worker_id`
instead of the op-key `kid`, so rosters and journals keep a stable id across
op-key rotations; the grant does not bind `sub.worker_id`.

This spec covers who may call which capability on which worker, how the
decision is made and recorded, and how a node proves that it holds a role such
as `is_hub`. It is written against the code on `beta` (cut from master
`733fc16`) and has to keep working with build-167 workers.

---

## 0. Terms and how things work today

| Term | Meaning in this spec |
|---|---|
| **Relay** | The telesthete hub process. It forwards frames by 16-byte `band_id` and never holds a PSK. It cannot read or check band traffic. |
| **Hub** | The Rook control plane: the band MCP (`rook/band_mcp`), the dashboard/site (`rook/remote`), their band clients, the vault, the journal and the OTA signing key. When this spec says "the hub enforces", it means this process, before a frame goes out through its band client. |
| **Band** | The set of peers that know one PSK. Every frame is AEAD-sealed with a key derived from that PSK (`BandCrypto(psk)`). |
| **Cap** | A dot-named capability (`shell.exec`) registered on a worker's `CapabilityRegistry` and listed in its announce. |
| **Principal** | Whoever a call is made for (§1). |
| **Tier** | The risk class of a cap: `read`, `write`, `exec`, `admin` (§2). |
| **Grant** | A signed statement that a key holds a role (§4). |
| **Ticket** | A short-lived, signed permission for one call, which the hub attaches so a worker can check the call itself (§3.6). |

What the current code does, and the gaps this spec closes:

- **Band membership is the only gate on workers.** `Worker._on_message`
  (`rook/worker/core.py`) runs any cap it owns for any frame it can decrypt.
  The `identity` field in the envelope is a free string. The band client
  stamps it from the token label, but any PSK holder can write anything
  there. The worker audit log (`rook/worker/audit.py`) records it, and
  nothing checks it.
- **MCP tokens give attribution, not authorization.** `TokenStore`
  (`rook/band_mcp/tokens.py`) resolves a bearer to `{kind, agent_id, key_id,
  label}`. `attribution.py` says so directly: "no capability allowlist". Every
  token, including the shared static token and the OAuth shim's client secret
  (which is a token too), can call every cap on every worker, plus
  `rook_secret get`.
- **The dashboard calls the band directly** (`bootstrap.py` `/api/call`,
  `work_web.py`) through its own band client. It stamps
  `_dashboard_identity()` (the signed-in account, or `human:dashboard` for the
  shared password). Its only refusal is the controller denylist of banned
  workers.
- **Direct band peers exist.** Any PSK holder, including any worker through
  `Worker.call`, can call workers directly. (The `rook band` TUI,
  `rook/cli/band_tui.py`, never did: it is an HTTP client of the dashboard's
  `/api/band/*`, so its calls already go through the hub and now carry the
  hub's tickets.)
- **Signed control already exists, for code only.** The hub's ed25519 key
  (`rook/remote/update_keys.py`, created on first start) signs OTA manifests
  and `worker.deauth` orders. Its public half is stamped into every bundle as
  `_update_pubkey.PUBKEY_B64` by `build_band_worker.py`. `_update_verify.py`
  verifies with it and fails closed.
- **Per-device keys exist, but only for HTTPS.** Enrolled workers hold an
  Ed25519 key with a certificate from the hub's device CA
  (`rook/remote/devices.py`, `rook/worker/device_key.py`). The certificate
  gates config retrieval and staged PSK migration, and nothing on the band
  checks it ("issuing a certificate does not enforce peer identity there").
- **Worker identity is self-asserted.** `worker_id` is a random uuid the
  worker persisted itself. `name` is its hostname. Announces are not signed,
  so any peer can announce any `worker_id` or name, including a name that
  collides with the hub's.

Design principles carried over from `docs/DESIGN-band-services.md` and the
attribution work:

1. *Breadcrumbs first, walls second.* Every enforcement feature ships in
   shadow ("would deny") mode first, and the journal decides when to switch it
   on.
2. *A broken checker must not brick the band.* This is the 349e3eb lesson
   recorded in `attribution.py`. Failure modes are chosen per principal
   (§3.8), and a crash never turns into a blanket denial for the operator.
3. *The relay stays dumb.* No policy lives in the relay.

---

## 1. Principals

A principal is written as a string `<kind>:<id>`. It is resolved from an
authenticated credential. It is **never** read from caller-supplied fields
such as the envelope `identity`, `sender` or `X-Rook-Host`. Those stay as
display breadcrumbs.

| Kind | Identified today by | Proposed principal id | Notes |
|---|---|---|---|
| **MCP agent token** | `TokenStore` entry: `agent_id` (stable across rotation), `key_id`, `label` | `token:<agent_id>` | Tokens gain a `role` attribute (`agent`, `operator`, `readonly`, `custom`) chosen at mint time. The role picks the principal's default tier table (§3.2). Labels are not unique, so policy stores the `agent_id` and the UI shows the label. |
| **Shared static token** | `ROOK_MCP_STATIC_TOKEN` → `kind="shared"` | `token:static` | Kept for compatibility as an `operator`. The dashboard flags it as a finding and recommends minting per-agent tokens and then unsetting it. |
| **OAuth shim client** | The client secret *is* a rook token | same as the token | No new principal. The shim returns the token itself. |
| **Unverified caller** | `Attribution(kind="unverified")`: the token passed the transport check but the per-call lookup failed | `unverified` | Has its own defaults table (§3.8). |
| **Dashboard human** | Account session (`users`, `memberships.role` = `owner`/`member` per band, `admin` flag), or the shared dashboard password → `human:dashboard` | `human:<user_id>`. The built-in groups `human:owner` and `human:member` are resolved per band. `human:dashboard` stays for the shared password. | Owners hold `admin` on their band. The shared password maps to an owner for compatibility and should be retired later. |
| **Integration** (Telegram/Discord bot, third-party app) | The hub plugins `telegram` and `discord` call as `integration:<name>` and fail closed on `would_deny` ([../integrations.md](../integrations.md)). No on-behalf-of chain yet. | `integration:<name>`, optionally narrowed to `integration:<name>/user:<external_id>` | An integration acts *for* chat users, so its calls carry an on-behalf-of chain (§1.1). It authenticates with its own credential: a hub plugin identity, or a token minted with `role=integration`. |
| **Plugin** | Not modelled. Hub modules call the band as `system:*` strings. Worker plugins can call through `Worker.call`. | `plugin:<name>` on the hub, `plugin:<name>@<worker>` on a worker | A plugin gets **nothing by default**. Its manifest lists what it needs (`requires: [embed.text, memory.get]`), the operator approves that list at install, and the approved list becomes the plugin's allow rules. |
| **Worker** (as a caller) | Self-chosen `worker_id` and `name`, unauthenticated. `device_id` plus Ed25519 certificate for enrolled workers, over HTTPS only. | `worker:<device_id>` | Until announces and calls are signed with the device key (rollout stage 6), a call arriving over the band has **no** authenticated principal. The hub treats it as `band:unauthenticated`. |
| **System** | Free strings: `system:rook-mcp`, `system:work`, … | `system:<component>` | Internal hub callers such as timeout discovery (`caps.describe`) and work-session RPCs. Each is declared in code with an explicit cap list, and it is not a wildcard. |

### 1.1 On-behalf-of chains

A call can pass through several principals. Examples: a Telegram user →
the Telegram integration → the hub, or an agent token → a hub plugin that
fans out calls for it. The hub records the chain in order:
`via: ["integration:telegram", "human:<uid>"]`.

**Rule: the call is allowed only if every principal in the chain is allowed.**
The effective permission is the intersection. So linking a Telegram user
to an owner account never gives the bot more than its own policy allows,
and a plugin acting for a read-only token cannot exec. A chat user with no
linked account adds no principal, and the integration's own policy applies.

### 1.2 What a principal carries into evaluation

```
principal   = "token:agent_3f…"          # the authenticated id
kind        = "token"
role        = "agent"                     # picks the defaults table
groups      = ["ci-agents"]              # operator-assigned in policy
via         = []                          # on-behalf-of chain, outermost first
verified    = true
```

---

## 2. Risk tiers

Every cap has exactly one tier. A cap's tier is set by the **worst effect a
caller can reach through it**, not by its typical use.

| Tier | Definition | Test |
|---|---|---|
| `read` | Returns information. Side effects are limited to logs and caches. | Is a repeated call harmless apart from disclosure? |
| `write` | Changes application-level state inside Rook or a plugin's own domain, such as messages, notes, chat, torrents or task records. It cannot run arbitrary code or change trust. | Could the worst misuse be cleaned up in the app's own terms? |
| `exec` | Can lead to arbitrary code or arbitrary input on the host as the worker's user. That covers shell and processes, keyboard/mouse injection, writing arbitrary files, starting an agent with tools, and custom `cmd.*` caps. | Could a determined caller get a shell out of it? |
| `admin` | Changes the worker's or band's identity, code, configuration, trust or cap surface: update, restart, reconfigure (hub/PSK), config apply, plugin enable/disable, custom-cap definition, enrollment, deauth, token/policy/secret administration. | Could it re-point, re-code or re-key the node or the band? |

Tiers are ordered `read < write < exec < admin`. A principal default of
`exec: allow` does **not** imply `admin: allow`. Each tier is set on its own.

### 2.1 Tags

Tags are orthogonal to tiers. Policy can select on them (`tag:sensitive`),
and they don't change the tier:

- `sensitive`: the cap discloses secrets or private data: file contents,
  screenshots, camera frames, agent transcripts, environment values, audit
  logs, vault values.
- `destructive`: hard to undo: power off, deleting torrents together with
  their data, killing processes.
- `physical`: acts on hardware outside the worker's own process space (HID,
  CEC, KVM, power).

### 2.2 Where the tier comes from

1. **Declared by the cap.** The plugin API gains
   `@capability("exec", tier="exec", tags=("physical",))`. The worker
   announces a compact map `"tiers": {"shell.exec": "x", "info.ping": "r", …}`
   (`r|w|x|a`), which build-167 clients ignore. Hub plugins declare tiers the
   same way.
2. **The built-in table** in Appendix A, shipped with the hub. It covers
   workers that announce no tiers (build 167) and acts as a floor: **a
   worker may declare a higher tier than the table, never a lower one.**
   Otherwise a worker running modified code could announce `shell.exec` as
   `read`.
3. **An operator override** in policy (`tiers:` block, §3.4). Raising a tier
   is always allowed. Lowering one below the built-in table needs an explicit
   `lower: true` and is journaled as `audit.policy` with a warning.
4. **Unknown cap** (not declared, not in the table, no override): **`exec`**.
   Custom command caps (`cmd.*`) are always `exec`.

---

## 3. Policy

### 3.1 Shape

There is one policy document per hub, stored by the settings framework (scope
`hub`, with history and revisions). It is YAML on disk and in the editor, and
compiled at load into an in-memory decision structure. Every save creates a
new `rev` (a monotonic integer) that shows up in journal rows and tickets.

```yaml
version: 1
mode: enforce            # off | audit | enforce   (hub-side, §3.5)

# Who gets what when no rule matches. Resolution order for a principal:
# principals[<exact id>] -> principals[<role>] -> principals[<kind>:*] -> defaults
defaults:  { read: allow, write: allow, exec: deny,  admin: deny }

principals:
  "role:operator":      { read: allow, write: allow, exec: allow, admin: allow }
  "role:agent":         { read: allow, write: allow, exec: allow, admin: deny }
  "role:readonly":      { read: allow, write: deny,  exec: deny,  admin: deny }
  "human:owner":        { read: allow, write: allow, exec: allow, admin: allow }
  "human:member":       { read: allow, write: allow, exec: allow, admin: deny }
  "integration:*":      { read: allow, write: allow, exec: deny,  admin: deny }
  "plugin:*":           { read: deny,  write: deny,  exec: deny,  admin: deny }
  "band:unauthenticated": { read: allow, write: deny, exec: deny, admin: deny }
  "unverified":         { read: allow, write: allow, exec: deny,  admin: deny }

groups:                      # target groups
  lab:        [worker-a, worker-b]
  kiosks:     { match: "has(display) && is_embedded" }   # dynamic, placement only
principal_groups:
  ci-agents:  ["token:agent_3f…", "token:agent_91…"]

tiers:                       # operator overrides (raise freely; lower needs lower: true)
  deluge.remove: { tier: write, tags: [destructive] }

rules:                       # ordered; most specific wins (§3.3)
  - id: telegram-exec-lab
    who:   integration:telegram
    allow: tier:exec
    on:    [worker-a, worker-b]
  - id: telegram-no-exec
    who:   integration:telegram
    deny:  tier:exec
    on:    "*"
  - id: telegram-no-sensitive-reads
    who:   integration:telegram
    deny:  tag:sensitive
    on:    "*"
  - id: ci-agents-lab-only
    who:   group:ci-agents
    deny:  tier:exec
    on:    "!group:lab"
  - id: no-camera-for-agents
    who:   role:agent
    deny:  camera.*
    on:    "*"
  - id: hygiene-plugin
    who:   plugin:hygiene
    allow: [memory.get, memory.search, knowledge.*]
    on:    rook
```

The worked example from the task: `telegram-no-exec` denies exec everywhere,
and `telegram-exec-lab` names two hosts, so its target is more specific and it
wins on `worker-a` and `worker-b`. It would win even if it came later in the
file. Its position here is only for readability.

### 3.2 Grammar

```
rule       := { id, who, (allow|deny): capsel, on: targetsel, [note] }
who        := principal | "role:"R | "group:"G | kind":*" | "*"
capsel     := cap | cap-glob ("shell.*", "*.read") | "tier:"T | "tag:"X | [capsel, …]
targetsel  := worker-name | "id:"worker_id | "device:"device_id | "group:"G
            | fact-expr | "rook" | "*" | "!"targetsel | [targetsel, …]
fact-expr  := "has(" fact ["," cond] ")" | "is_embedded" | "is_hub" | "role(" R ")"
            | fact-expr "&&" fact-expr | fact-expr "||" fact-expr
cond       := field op value      # e.g. has(gpu, vram_gb>=8)
```

- **Worker names resolve against the hub's enrolled device registry**, not
  against announces. A name in a rule binds to the `device_id` that holds it
  at policy save time, and the compiled policy stores the id. Renaming a
  worker doesn't move rules onto another machine. A new machine announcing
  an existing name doesn't inherit them. Workers that aren't enrolled (legacy
  PSK-only) bind by `worker_id` and show a "weak binding" badge in the editor.
- **`rook`** is the reserved hub worker (§4.6). It matches only the peer
  holding a valid `is_hub` grant for this band.
- **`is_hub` and `role(R)`** match signed grants only (§4).
- **Everything else in a fact expression is self-reported** (§4.8).

### 3.3 Evaluation

The input is `(principal chain, cap, target worker)`. The output is
`allow | deny`, plus the rule id (or `default:<table>`) and the policy `rev`.

1. **Hard invariants.** Policy cannot override these:
   - unknown or unauthenticated principal on an authenticated surface → deny;
   - target on the controller denylist (bans) → deny;
   - target named `rook` without a valid `is_hub` grant → deny (impostor);
   - untargeted (broadcast) call above `read` → deny;
   - `{{secret:…}}` substitution into a target matched only by self-reported
     facts → deny (§4.8).
2. For **each principal in the chain**, evaluate steps 3–5. The call is
   allowed only if all of them allow (§1.1).
3. Collect the rules whose `who`, cap selector and target selector all match.
4. **Most specific wins.** Compare matching rules by the tuple
   `(target_specificity, cap_specificity, principal_specificity)`,
   lexicographically, highest first:
   - target: `id:`/`device:`/name = 4, `rook` = 4, `group:` = 3, signed-role
     selector = 2, self-reported fact expression = 1, `*` = 0. A negated
     selector (`!x`) scores one below `x`.
   - cap: exact name = 4, glob with a namespace prefix (`shell.*`) = 3,
     `tag:` = 2, `tier:` = 1, `*` = 0.
   - principal: exact id = 4, `group:` = 3, `role:` = 2, `kind:*` = 1, `*` = 0.
   - **Tie on all three:** the earlier rule in the file wins. That is the
     only thing file order decides. The editor and the loader lint every
     tie between an allow and a deny, and the editor asks for confirmation
     before saving one.
5. **No rule matched:** use the principal's tier default. Resolution order:
   exact principal → `role:` → `kind:*` → `defaults`.

Target specificity comes first on purpose. Operators think in host
exceptions ("except on these two machines"). A host exception written
against a tier must beat a broad cap-specific rule on other hosts, and still
lose to a cap-specific rule on the *same* host.

The explain output (`policy.explain`, the dashboard, and the denial reply)
shows the winning rule and the runner-up so operators can see why.

### 3.4 Tier overrides and per-argument risk

v1 authorizes by cap, tier and tag only. Argument-level constraints
(`file.read` only under `/srv/share`, `shell.exec` only for `systemctl
status *`) are out of scope. The supported pattern is a custom command cap
(`customcap.add`, admin tier). Its template is operator-authored and its
arguments are shell-quoted, so a narrow action gets its own cap name that
policy can allow on its own. Argument predicates are a candidate follow-up
(§6.5).

### 3.5 Enforcement points

```
 MCP tool call ─┐
 dashboard  ────┼─► authorize() ─► [allow] ─► sign ticket ─► band client ─► relay ─► worker
 hub plugins ───┤        │                                                           │
 integrations ──┘        └─► [deny] ─► denial reply + journal         verify ticket ◄┘
                                                                     (defense in depth)
```

**E1: hub, before routing (primary).** There is one
`authorize(chain, cap, target)` function, and every hub path that emits a
band call must go through it. To make that hard to bypass, it lives *inside*
the hub's band client wrapper. `BandClient.call` / `MultiBandClient.call`
take a `principal` argument instead of a free `identity` string and refuse to
send without one. That covers `rook_call`, the tools that call caps
internally (`rook_console_open` → `proc.start`, `rook_chat_wake` →
`agent.wake`/`hermes.chat`, `rook_config_apply` → `worker.config_*`), the
dashboard `/api/call`, `work_web`, the migration controller, OTA pushes, and
hub plugins.

**Hub tools are caps too.** The MCP tools that act on the hub itself
(`rook_secret`, `rook_knowledge`, `rook_task`, chat, consoles, journal) are
authorized as caps on the reserved worker `rook` (Appendix A.2). The check
runs in the existing `_attributed_call_tool` wrapper, right after
attribution, so it covers every tool in one place.

Hub-side mode (`mode:` in policy):
- `off`: no evaluation (the build-167 world).
- `audit`: evaluate, allow everything, journal `decision=would_deny` with the
  rule. This is the default when the feature first ships.
- `enforce`: denials are returned.

`audit` can also be set per principal (`principals.<p>.mode: audit`) so an
operator can turn on enforcement for integrations first and leave agents in
shadow mode.

**E2: worker, defense in depth.** A worker that understands tickets checks
each incoming call against its local *enforcement mode*:

| Worker mode | Calls that need a valid hub ticket |
|---|---|
| `off` | none (build 167, and firmware workers) |
| `audit` | none, but unticketed exec/admin calls are logged loudly in `audit.jsonl` and shown on the dashboard |
| `enforce-admin` | admin |
| `enforce-exec` | exec + admin |
| `enforce-all` | everything except `caps.describe` and `info.ping` |

The worker does not hold the policy. A ticket *is* the hub's decision. The
worker checks that the ticket is authentic, fresh, unreplayed and bound to
this call (§3.6), and uses **its own** tier table (declared tier, floored by
the built-in table it ships with) to decide whether a ticket is needed.

A worker may also carry a **local floor** at `~/.rook-band-worker/policy.yaml`,
set by whoever administers that machine. The floor can only *deny*, for
example `deny tier:exec for integration:*`. It is applied to the ticket's
principal chain after the ticket verifies. The hub cannot override it.

Mode changes: raising the mode is a normal ticketed `worker.config_apply`.
Lowering it needs a ticketed admin call *or* local access
(`ROOK_AUTHZ_MODE=off` in the worker's environment). Local root on a worker
is trusted by definition, and the env override is the recovery hatch.

**E3: relay.** None. The relay has no key and sees only ciphertext. Per-device
transport keys (§6.5) would let it admit or refuse peers, which is a
different layer.

### 3.6 Call tickets

The hub signs a ticket for every call it sends. Tickets are attached even to
workers that ignore them: build-167 workers read only `id/cap/target/args/
identity` from the envelope, so an extra key is harmless. That lets the audit
data show which workers are ready.

```json
{"id": "<msg id>", "cap": "shell.exec", "target": "<worker_id>", "args": {…},
 "identity": "agent:ci-runner",
 "ticket": {
   "v": 1, "kid": "<hub op-key id>",
   "p": "token:agent_3f…", "via": [],
   "cap": "shell.exec", "t": "<worker_id>", "id": "<msg id>",
   "ah": "<b64url sha256 of canonical args as sent>",
   "tier": "x", "rev": 42,
   "iat": 1790800000, "exp": 1790800030,
   "sig": "<b64 ed25519>"}}
```

- **Signed bytes:** `b"rook-ticket-v1\n" + canonical_json(ticket minus sig)`.
  `canonical_json` means UTF-8, sorted keys, `separators=(",",":")`,
  `ensure_ascii=True`, and no floats in signed bodies. It is the same
  encoding `_update_verify.canonical_payload` uses, plus a domain prefix
  (§4.2).
- **Signed by** the hub's *operational* key (`kid`), which the worker trusts
  only through a valid `is_hub` grant for its band (§4). The OTA root key is
  never used for tickets.
- **The worker checks:** a valid signature from a key with a valid `is_hub`
  grant for this band; `t` == own `worker_id`; `id` == envelope `id`; `cap` ==
  envelope `cap`; `ah` == sha256 of the canonical args received; `iat - skew
  ≤ now ≤ exp + skew` (skew 300 s); and `id` not in the replay cache (LRU of
  msg ids seen within `exp + skew`, bounded at 10k entries).
- **Lifetime:** 30 s. A ticket only has to survive transit, not execution.
- **Args binding** means a PSK holder who captures a ticket can't reuse it
  with different arguments, on another worker, or twice. The hash covers the
  args *after* `{{secret:…}}` substitution, because those are the bytes the
  worker receives. The hash reveals nothing that the PSK-encrypted args don't
  already reveal to a band member (§6).
- **Direct band peers** (scripts) route through the hub's call API
  (`POST /api/band/call`), as the `rook band` TUI does; a ticket endpoint
  (`POST /api/band/ticket`) is left for a peer that must keep a direct path.
  A peer with only the PSK can't get a ticket.
- **Worker-to-worker and worker-to-hub calls** carry no hub ticket. Until
  stage 6 they are `band:unauthenticated`: read-only by default at the hub,
  and refused above the worker's mode threshold at enforcing workers. At
  stage 6 the calling worker signs the same ticket body with its device key
  (`kid = device:<id>`), and the principal becomes `worker:<device_id>`.

### 3.7 Denials: replies and journaling

The denial reply has the same shape as every other `rook_call` failure, so
agents need no new handling:

```json
{"ok": false,
 "error": "denied: integration:telegram may not call shell.exec (exec) on worker-c",
 "denied": {"tier": "exec", "rule": "telegram-no-exec", "rev": 42,
            "principal": "integration:telegram", "via": []}}
```

- The reply names the rule id and the policy revision. It never includes the
  full policy or other principals' rules.
- A worker-side refusal comes back as
  `{"ok": false, "error": "denied by worker: exec requires a hub ticket (mode enforce-exec)"}`.
- **Journal (hub):** the `calls` table gains `principal`, `decision`
  (`allow|deny|would_deny`), `rule`, `policy_rev` and `tier`. Every call,
  allowed or not, gets one row, as allowed calls already do today. Denials
  are rare by design, and they are exactly the rows an operator wants to
  find. A denied call's args are journaled with secrets unexpanded
  (placeholders only), as now.
- **Worker audit:** `audit.record` gains
  `ticket: {kid, p, verified, reason}` and `decision`. That makes "calls
  without a ticket" a query in `log.audit`, and the dashboard's readiness
  view (§5.2) uses it.
- **Policy changes** are journaled as `audit.policy` rows carrying the actor,
  the old and new rev, and a diff summary.
- Repeated identical denials (same principal, cap and target within 10 s) are
  coalesced into one row with a `count`, so a looping agent can't flood the
  20k-row ring.

### 3.8 Failure modes

| Failure | Behaviour |
|---|---|
| Policy file fails to parse or validate on load | Keep the last-known-good compiled policy and alert. At first start with no valid policy, use the built-in compatibility policy (§5). |
| `authorize()` raises for a principal with role `operator`, `human:owner` or `token:static` | **Allow, alert, journal `decision=error_allow`.** The owner is never locked out by a bug. |
| `authorize()` raises for any other principal | **Deny that call**, alert, journal `decision=error_deny`. A crash must not hand an integration a shell. |
| Attribution unverified (`kind="unverified"`) | Evaluate as `unverified`: read and write allowed, exec and admin denied, with a loud alert. This is a deliberate partial outage. The transport already accepted *a* token, but not knowing whose it is must not grant the broadest defaults. Operators can widen it in policy. |
| Ticket signing unavailable (op key missing or unreadable) | The hub sends calls without a ticket and alerts. Workers in `audit` still work. Workers in `enforce-*` refuse exec/admin, and the dashboard shows why. |
| Worker can't verify (no grant cached, clock badly wrong) | See §4.5 (hub-signed time) and §4.4 (grant fetch). The worker refuses only above its mode threshold and logs the reason. |

### 3.9 Performance budget

- **Hub `authorize()`**: no I/O on the call path. The policy is compiled into
  per-principal tables. Target selectors are pre-resolved against the roster
  and recomputed when the roster or `rev` changes, not on every call. The
  result is memoized by `(chain, cap, target_id, rev, roster_rev)`. Budget:
  **p99 ≤ 50 µs**.
- **Ticket signing**: one ed25519 signature (libsodium, about 20–50 µs) plus a
  sha256 of the args. Budget: **p99 ≤ 150 µs** including canonical JSON of
  typical args, and ≤ 2 ms for multi-MB args such as `file.write`.
- **Worker verification**: one ed25519 verify (about 60–120 µs on x86, around
  1 ms on a small SBC) plus the args hash. Grant signatures are verified once
  per grant and cached by `kid`. Budget: **≤ 1 ms on SBC-class hardware**.
- **Announces**: the grant adds about 400 bytes every 30 s per role holder.
  Stage-6 announce signatures cost one verify per announce per listener.
  Both are negligible next to the existing heartbeat traffic.
- **Tokens**: `rook_caps` and `rook_workers` annotate (or, as an option,
  hide) caps the caller is denied, so agents don't spend turns on calls that
  will fail. The annotation is one compact `denied: [...]` list per worker,
  in line with the token-envelope work.

The acceptance test for implementation: the existing `pytest` suite plus a
microbenchmark of 10k `authorize()` + sign + verify rounds, which must stay
inside these numbers on the CI runner.

### 3.10 Editing policy in the dashboard

A **Permissions** page, available to owners and operator accounts only:

- **Defaults matrix:** principals and roles (rows) × tiers (columns), each
  cell `allow|deny|audit`.
- **Rules list:** drag to reorder, which only matters for ties. Each rule has
  pickers for who, caps (with tier and tag chips) and targets (workers,
  groups, fact expressions). Self-reported facts get a "placement only" badge
  and a lint when used in an allow rule above `write`.
- **Explain:** pick a principal, a cap and a worker, and see the decision,
  the winning rule and the runner-up.
- **Dry run:** before saving, replay the last 7 days of journal rows through
  the draft policy and list the calls that would change outcome ("this edit
  would have denied 14 calls by `token:ci-runner`"). This is the main
  protection against a policy edit bricking the band.
- **YAML tab:** the same document as text, schema-validated, with lint shown
  inline.
- **History:** revisions through the settings framework, with diff and
  one-click revert, which creates a new revision.
- **Guards:** saving needs a fresh session (re-auth within 10 min) plus CSRF.
  It is itself an admin-tier cap on `rook` (`policy.set`), so an MCP agent can
  change policy only if policy lets it, and by default no agent can. The
  editor refuses a save that would leave no principal holding `admin` on
  `rook`.
- **Token mint:** the Tokens page gets a role picker (agent, operator,
  readonly, integration, custom), so new tokens start least-privileged.

---

## 4. Signed role grants

### 4.1 Key hierarchy

| Key | Where | Signs | Trusted because |
|---|---|---|---|
| **Root key**: the existing OTA signing key (`update-signing-key`) | hub, mode 0600 (later: offline, §6.5) | OTA manifests, deauth orders, **grants**, revocation lists, root rotation | its public half is stamped into every worker bundle (`_update_pubkey.PUBKEY_B64`) |
| **Hub operational key** (new, `hub-op-key`, ed25519) | hub, mode 0600, rotated every 30 days | call tickets, hub announces (and signed hub replies later) | an `is_hub` grant signed by the root |
| **Device keys** (existing Ed25519 enrollment keys) | each enrolled worker | stage 6: worker announces and worker-originated tickets | device certificate from the device CA, *and* for roles, a root-signed grant naming the device key |

The root key signs rarely: grants, rotations and releases. The operational key
signs every call. Keeping them separate means rotating the busy key never
touches the anchor baked into bundles, and a future offline root costs only
one grant renewal a month.

### 4.2 Domain separation

Today a deauth order is checked with `verify_manifest`, the same function
and the same canonical bytes as an OTA manifest. So any validly signed body
can stand in for any other (§6.3). Grants must not repeat that mistake.
Every signed object type gets its own prefix and `typ`:

| Object | Signed bytes |
|---|---|
| grant | `b"rook-grant-v1\n" + canonical(body)` with `typ: "rook-grant"` |
| ticket | `b"rook-ticket-v1\n" + canonical(body)` |
| revocation list | `b"rook-revocations-v1\n" + canonical(body)` |
| root rotation | `b"rook-root-rotate-v1\n" + canonical(body)` |
| signed announce (stage 6) | `b"rook-announce-v1\n" + canonical(body)` |
| manifest v2 / deauth v2 (wave-2 fix) | `b"rook-manifest-v2\n"…` / `b"rook-deauth-v2\n"…` |

Verifiers accept an object only under its own prefix. Legacy unprefixed v1
manifests stay valid **only** on the manifest path, and never as a grant,
deauth or ticket.

### 4.3 Grant format

```json
{"typ": "rook-grant", "v": 1,
 "serial": "<128-bit random, hex>",
 "iss": "<root key id = first 16 hex of sha256(root pubkey)>",
 "sub": {"key": "ed25519:<b64 pubkey>", "kid": "<16 hex>",
         "device_id": "<optional>", "worker_id": "<optional>"},
 "role": "is_hub",
 "name": "rook",
 "scope": {"bands": ["<band_id hex>", "…"]},
 "constraints": {"max_tier": "admin"},
 "iat": 1790800000, "nbf": 1790800000, "exp": 1791404800,
 "sig": "<b64 ed25519 by root>"}
```

- `role` comes from a registry. v1 defines only `is_hub`. Candidates for
  later: `is_relay_admin`, `peer_hub` (§4.7), `is_gateway`.
- `sub.key` is what the grant is really about: whoever can sign with this key
  holds the role. `worker_id` and `device_id` are informational bindings, and
  they are checked when present.
- `scope.bands` limits the grant to the listed bands. A grant is invalid on a
  band it doesn't list, so a grant leaked from one band can't be replayed on
  another.
- `constraints` attenuate the role (`max_tier`, `caps`), and this is what
  federation needs (§4.7). A ticket signed under a grant with
  `max_tier: read` is invalid for an exec cap.
- `name` is set only for `is_hub`, and only to the reserved name (§4.6).

### 4.4 Carrying and verifying grants

- **In announces.** A role holder adds `"grants": [<grant>, …]` to its
  announce. Build-167 listeners ignore unknown announce keys:
  `BandClient._handle_announce` reads fields with `.get`, and workers drop
  anything without `cap`. So no version negotiation is needed for the field
  itself. Readiness is advertised separately (§5).
- **Holding the key.** A grant in an announce proves nothing by itself,
  because anyone on the band can copy it into their own announce. Peers
  treat a grant as *held* only when the announcer shows it holds `sub.key`.
  Until stage 6 the hub proves this with a signature over the announce
  (`rook-announce-v1`, covering `worker_id`, `name`, `caps`, `ts` and `seq`),
  and tickets prove it on every call. A replayed hub announce only repeats
  a statement that was true within the freshness window (`ts` within 90 s,
  `seq` must increase). It can't be used to sign tickets or to change the
  announced caps.
- **Who verifies:**
  - *Workers* verify the `is_hub` grant behind every ticket's `kid`. They
    cache grants from the hub's announces. On a miss they accept a grant
    inlined in the ticket (`ticket.grant`), which the hub includes for a
    worker's first call after a key change.
  - *Hub band clients* (MCP, dashboard) verify grants on any announce that
    claims the name `rook` or a role, including other hubs later.
  - *Clients such as the TUI and other apps* verify before routing anything
    to `rook` or trusting a role.
- **Verification steps:** signature by a trusted root under the `rook-grant-v1`
  prefix; `iss` matches that root; `nbf ≤ now ≤ exp` (1 h grace for skew);
  the current band is in `scope.bands`; `serial` not revoked; `sub` bindings
  match the announcer; the role is known.

### 4.5 Rotation, revocation, expiry, time

- **Operational key rotation.** The hub creates a new op key, the root signs
  an `is_hub` grant for it, and the hub announces **both** grants for an
  overlap window (default 24 h), signing new tickets with the new key.
  Workers accept tickets from any currently valid grant. The old key is
  deleted after the window.
- **Expiry.** `is_hub` grants live 7 days and are renewed automatically every
  24 h. Short grant lifetimes are the main revocation mechanism.
- **Revocation list.** For an emergency before expiry, the root signs
  `{typ: "rook-revocations", seq, iat, serials: [...]}`. The hub carries its
  hash in its announce and serves it as `rook` cap `grants.revocations`
  (read tier). Workers keep the highest `seq` they have seen and never go
  back to a lower one. A worker whose cached list is older than the hash in a
  valid hub announce fetches the new list before accepting further tickets.
- **Root rotation.** The root pubkey is baked into bundles, so rotation is a
  signed statement from the *old* root: `{typ: "rook-root-rotate", new_root,
  effective, iat}`. Workers add `new_root` to their persisted anchor list
  (`~/.rook-band-worker/anchors.json`) and drop the old one after
  `effective` + 30 days. Bundles built after the rotation carry the new key.
  A compromised root can't be fixed this way, because the attacker could
  rotate too. Recovery from root compromise is independent re-enrollment,
  the same stance the enrollment plugin already takes for a compromised mesh.
- **Time.** Tickets and grants need rough clock agreement, and some workers
  (embedded, sleeping phones) have bad clocks. A verified hub announce
  carries a signed `ts`. A worker may adopt a clock offset from verified hub
  announces only: the offset only moves forward, and it is bounded to one
  grant lifetime. Unsigned announces never affect the worker's time.

### 4.6 The reserved hub worker name `rook`

- The hub appears on each of its bands as a worker named **`rook`**. Its
  `worker_id` is its op-key `kid`. Its caps are the hub caps (Appendix A.2),
  its `tiers` map is included, and so is its `is_hub` grant with
  `name: "rook"`.
- **Resolution:** clients resolve `worker="rook"` to the announcer whose
  valid `is_hub` grant has `name == "rook"` and lists this band. An announce
  that claims the name `rook` without such a grant is **quarantined**: it is
  not routable by name, it is shown in the dashboard as an impostor, and it
  is journaled as `audit.impostor`.
- **Calls from the hub's own MCP or dashboard to `rook`** are handled in
  process. They never go over the band, but they pass the same `authorize()`.
- **Name hygiene:** enrollment and `worker.description_set`/rename refuse
  `rook` (case-insensitive, and also `rook-*` reserved for federated hubs).
  An existing worker whose hostname is `rook` is shown as `rook~<id8>` and
  can be addressed by id.
- Hub caps are tiered like any others. `rook` gets no implicit trust:
  `secret.get` on `rook` is `admin` whoever calls it.

### 4.7 Multiple hubs (later)

Federation (telesthete `HUB_FED_*`, shipped but dormant) links hubs so that
pooled workers are reachable through their home hub. Grants extend to it
without new concepts:

- **Each hub keeps its own root.** A worker trusts the anchors listed in
  `anchors.json`, and each anchor can be limited to bands (`{"root": "…",
  "bands": ["…"]}`). A worker serving two hubs lists both.
- **Cross-hub authority is attenuated.** Hub A's root issues hub B's op key a
  `peer_hub` grant scoped to the shared bands with `constraints: {max_tier:
  read}` (or a cap list). Tickets from hub B are valid on A's workers only
  within those constraints. A's policy still decides what B's principals may
  do, and B forwards the principal chain with `via: ["hub:<B kid>", …]`.
- **Naming:** only the home hub is `rook` on a band. A peer hub is addressed
  as `rook-<label>` and holds `peer_hub`, not `is_hub`. "One hop, never
  re-forward hub-sourced frames" (DESIGN-band-services §federation) applies
  to tickets too: a ticket is valid only on the band its grant scopes.

### 4.8 Self-reported facts versus signed roles

- **Hardware and platform facts** (`camera`, `gpu`/`vram_gb`, `pty`,
  `display`, `embedded`, `os`, `arch`, battery) are announced by the worker
  under `facts`. The worker is the only source for them, and a worker running
  modified code can lie.
- **They gate placement only**: which worker a plugin runs on, which workers
  a picker offers, which targets a `has(...)` selector expands to. They never
  grant a role, and they never loosen a hard invariant.
- **Roles are grants only.** A fact key that collides with a role name
  (`is_hub`, `role`, `grants`) is dropped from `facts` at parse time.
- **Policy lint:** an `allow` rule above `write` whose target selector
  contains only self-reported facts gets a warning ("a worker can claim this
  fact to receive these calls"). A lying worker gains nothing it couldn't do
  to itself, but it can *attract* calls, and with them arguments and
  workloads.
- **Secrets never go to a fact-only target.** `{{secret:…}}` substitution is
  refused when the target was matched only by self-reported facts. It needs
  a name, id, group or signed role.

---

## 5. Compatibility and rollout

### 5.1 Build-167 workers

| Aspect | Build-167 behaviour | Effect |
|---|---|---|
| Envelope `ticket` key | ignored (the worker reads `id/cap/target/args/identity` only) | tickets can ship from day one |
| Announce `grants`, `tiers`, `facts`, `authz` keys | not sent | the hub uses the built-in tier table; unknown caps count as `exec` |
| Hub announce `grants` | ignored (workers drop frames without `cap`) | no effect |
| Enforcement | none | **hub enforcement (E1) is the only protection.** Direct PSK peers can still command these workers. The dashboard badges them "hub-enforced only". |
| Root pubkey | already baked in (same OTA key) | after an OTA to the enforcing build they can verify grants with the anchor they already trust. No new key distribution. |

Readiness is advertised in announces as
`"authz": {"v": 1, "mode": "audit", "anchors": ["<kid>"]}`. The hub gates
features on it. It never assumes a worker enforces because of its build
number alone.

No wire change needs version negotiation: every addition is an optional key
that old peers ignore. The one behaviour change old workers could notice,
refusals from enforcing workers, happens only on workers new enough to
enforce.

### 5.2 Staged rollout

Each stage ships behind a flag, can be rolled back on its own, and is gated
on journal evidence, not on a date.

| Stage | Ships | Behaviour change | Gate to the next stage |
|---|---|---|---|
| **1. Tiers + shadow** | Built-in tier table, `@capability(tier=…)`, policy engine, journal columns, Permissions page (explain + dry run), hub `mode: audit` | none: `would_deny` rows only | a week of journal with no unexpected `would_deny` for owner/operator principals |
| **2. Hub enforcement for new principal kinds** | `mode: enforce` for `integration:*`, `plugin:*`, `band:unauthenticated`, `unverified`. Token roles at mint. Existing tokens keep compatibility defaults (`role:operator` for the static token, `role:agent` plus `admin: audit` for minted tokens). | integrations and plugins confined. Agents unchanged except admin moves to shadow. | integrations running clean. Owner reviews the `would_deny` admin rows for agents. |
| **3. Grants + tickets** | Op key, `is_hub` grant, hub announces as `rook`, `rook` impostor quarantine, tickets on every hub call, `/api/band/ticket`, TUI routed through the hub | none for workers (they ignore tickets). Clients refuse `rook` impostors. | tickets present on 100% of hub-originated calls in the journal |
| **4. Worker build N (audit)** | Ticket verification in `audit` mode, local floor, tier floor table, domain-separated deauth v2, `worker.update` requires a signed manifest (§6.3), hub-signed time | unticketed exec/admin calls logged on the worker | the readiness view shows zero unticketed exec/admin calls for 7 days on the workers being moved |
| **5. Worker enforcement** | Per-worker ticketed `worker.config_apply` of `authz_mode`: `enforce-admin` first, then `enforce-exec`. Band-wide default switch when all workers are ≥ N. | direct PSK peers lose exec/admin on enforcing workers | – |
| **6. Worker identity on the band** | Device-key-signed announces and worker-originated tickets (`worker:<device_id>`), name binding checked against the device registry | worker callers become real principals; impersonating a worker stops working on verifying clients | – |

**Rollback:** hub `mode` goes back to `audit` (one policy save). Worker mode
goes down through a ticketed config call, or locally through
`ROOK_AUTHZ_MODE=off`. The config OTA is commit-confirmed (it auto-reverts
unless confirmed), so a bad mode push that cuts the hub off rolls itself
back.

**Default policy at first start (compatibility policy):** hub `mode: audit`;
`role:operator`, `human:owner`, `token:static` and `human:dashboard` get
everything; minted tokens get `role:agent` with `admin: audit`; integrations
and plugins get the restrictive defaults in §3.1. Behaviour matches today's
until the operator turns enforcement on.

---

## 6. Threat model

### 6.1 Attackers considered

| # | Attacker | Example |
|---|---|---|
| A1 | **PSK holder**: a compromised worker, a leaked PSK, a former member | anything that can join the band |
| A2 | **Stolen MCP token** | a token pasted into the wrong place |
| A3 | **Integration user or injected prompt** | a Telegram chat member, or text in a web page an agent reads |
| A4 | **Malicious or buggy plugin** | a third-party hub plugin |
| A5 | **Network attacker or relay operator without the PSK** | on-path observer |
| A6 | **Hub compromise** | root on the hub host |

### 6.2 What changes

| Attacker | Today | After stage 5 |
|---|---|---|
| A2 stolen token | every cap on every worker, `rook_secret get`, config and update | limited to the token's role and rules. Admin is denied for `agent` tokens. Every call is journaled with rule and revision. |
| A3 integration | would inherit fleet-wide `shell.exec` | exec denied except on named hosts, sensitive reads deniable, on-behalf-of intersection. Prompt injection is still bounded only by what the principal is allowed. |
| A4 plugin | whatever hub code can reach | only the caps approved from its manifest (the in-process code boundary is the plugin-API task's concern; this spec governs its band and hub-cap calls) |
| A1 PSK holder → worker | runs any cap on any worker | **on enforcing workers:** no exec/admin (no ticket), and can't replay or retarget captured tickets. On build-167, `off` or `audit` workers: unchanged. |
| A1 → spoofing the hub | can announce as the hub name and answer hub-cap calls from other peers | `rook` impostors quarantined. Can't mint grants or tickets. |
| A1 → role spoofing | roles don't exist; facts are self-reported | roles need a root-signed grant plus key possession. Facts can still be faked, but they only affect placement. |

### 6.3 What a PSK holder can still do (known, accepted until follow-ups)

1. **Read all band traffic.** The band key is shared, so a PSK holder can
   decrypt every call and reply: arguments, file contents, screenshots,
   transcripts, and **secrets substituted with `{{secret:…}}`**, because
   substitution happens before the args are PSK-encrypted. The journal masks
   secrets, the wire does not. Follow-up: seal secret-bearing args to the
   target's device key (§6.5).
2. **Forge the display `identity`, `sender` and replies.** Replies aren't
   authenticated, so a PSK holder can answer a call meant for another worker
   first, or send a fake reply to a pending id, and give the hub a false
   result. Follow-up: signed replies from ticket-verifying workers.
3. **Impersonate a worker** (its `worker_id` or name) to clients until
   stage 6, and attract calls to itself, including their arguments.
4. **Command workers that don't enforce** (build 167, firmware, `off` or
   `audit` mode), and call read/write caps on workers whose mode doesn't
   cover those tiers.
5. **Deny service:** flood the band, or spoof announces to churn the roster.
6. **Fake hardware facts** to win placement.

Gaps found in current code while writing this spec. None of them adds
exposure *today*, because a PSK holder can already `shell.exec` anywhere.
They matter as soon as exec is gated, so they are listed as stage-4
requirements:

- **`worker.deauth` accepts any validly signed manifest.** It verifies with
  `verify_manifest`, and when `worker_id`/`issued_at` are missing it skips
  the target and age checks. OTA manifests are public, so any one of them,
  replayed as a deauth payload, parks every cooperative worker. Fix:
  deauth v2 with its own domain prefix and required `worker_id` and
  `issued_at` (§4.2).
- **`worker.update(url=…)` installs a bundle from any URL without a
  signature check** (`_download` → swap). That bypasses OTA signing. Fix:
  require a signed manifest (the same path as `worker.apply`), or remove the
  `url` parameter.
- **`worker.reconfigure` and `worker.config_apply` can repoint a worker to
  another hub or PSK**, and a signed order isn't required. They are `admin`
  tier and must need a ticket in `enforce-admin`.
- **A signed deauth order for worker X can be replayed against X for 24 h**
  (the `issued_at` window). That is acceptable for deauth, and tickets use a
  30 s window plus a replay cache.

### 6.4 What this does not fix

- **Confidentiality between band members** (item 1 above).
- **Hub compromise (A6).** The hub holds the root key, the op key, the vault
  and the policy, so whoever controls it controls the band. Mitigations are
  follow-ups: an offline root, and a hardware-backed op key.
- **Misuse of allowed permissions.** An agent allowed exec on `worker-a` can
  be talked into running anything there. Policy limits how far damage
  spreads. It doesn't detect intent.
- **Local root on a worker.** It can change the worker, its floor and its
  mode. That is by design, since the local operator is trusted.
- **Argument-level risk** (`file.read` of a key file is `read`/`sensitive`,
  not `admin`). Deny `tag:sensitive` for principals that shouldn't read
  secrets.
- **Availability.** Nothing here resists flooding.

### 6.5 Recommended follow-ups (in priority order)

1. **Per-device transport keys.** Replace the shared PSK with keys per device
   (or per session), terminated at the hub and derived from the existing
   device certificates, so compromising one member no longer exposes
   everyone's traffic. The relay could then admit peers by certificate. This
   is the only real fix for §6.3 items 1–3.
2. **Sealed secret arguments.** Encrypt `{{secret:…}}` values to the target
   worker's device public key (sealed box), so only that worker can read
   them. Requires stage 6 key binding.
3. **Signed replies** from ticket-verifying workers (device key), echoing
   the ticket id.
4. **Offline root.** Keep the root key off the hub. The hub holds only its
   op key and a month of pre-signed grant renewals, and releases are signed
   on a separate machine.
5. **Argument predicates** in rules (`where: {path: {prefix: "/srv/share"}}`)
   for the few caps that need them.
6. **Domain-separated manifests** (`rook-manifest-v2`) together with deauth
   v2, retiring unprefixed signatures once the fleet has moved.

---

## Appendix A: cap tier classification

Letters: **R** read, **W** write, **X** exec, **A** admin. Tags: *s*
sensitive, *d* destructive, *p* physical. This is the built-in table (§2.2).
Workers may declare higher tiers, never lower.

### A.1 Worker caps

**Core (registered by `Worker` / `WorkerAdmin`)**

| Cap | Tier | Tags | Why |
|---|---|---|---|
| `caps.describe` | R | | introspection |
| `worker.description_get` | R | | |
| `worker.description_set` | W | | descriptive inventory text |
| `worker.plugin.list` | R | | |
| `worker.plugin.enable` | A | | changes the cap surface |
| `worker.plugin.disable` | A | | changes the cap surface |
| `customcap.list` | R | | |
| `customcap.add` | A | | defines new exec caps |
| `customcap.remove` | A | | changes the cap surface |
| `cmd.*` (custom command caps) | X | | parameterised shell |

**`worker.*` self-update (`selfupdate.py`)**

| Cap | Tier | Tags | Why |
|---|---|---|---|
| `worker.status` | R | | argv already redacts PSK/pair code |
| `worker.check` | A | | forces an update check, including held pins |
| `worker.apply` | A | | installs a (signed) build |
| `worker.ota_begin` | A | | arms an in-band install |
| `worker.hold` | A | | pins the build |
| `worker.deauth` | A | d | parks the node off-band |
| `worker.restart` | A | d | |
| `worker.reconfigure` | A | d | repoints hub/PSK |
| `worker.update` | A | d | installs from a URL (see §6.3) |

**`worker.*` config (`config.py`)**

| Cap | Tier | Tags |
|---|---|---|
| `worker.config_get` | R | |
| `worker.config_apply` | A | d |
| `worker.config_confirm` | A | |
| `worker.config_revert` | A | d |

**`worker.*` enrollment (`enrollment.py`)**

| Cap | Tier | Tags | Why |
|---|---|---|---|
| `worker.enrollment_status` | R | | |
| `worker.enrollment_prepare` | A | | creates the device key and CSR |
| `worker.enrollment_move_prepare` | A | | |
| `worker.enrollment_finish` | A | | binds the device identity |
| `worker.enrollment_prove` | A | | signs with the device key |

**Plugins**

| Cap | Tier | Tags | Notes |
|---|---|---|---|
| `battery.status` | R | | |
| `camera.list` | R | | |
| `camera.snap` | R | s,p | privacy |
| `cec.ping` | R | p | |
| `cec.send` | W | p | TV/AV control |
| `cec.raw` | W | p | raw CEC frames |
| `chat.open` | W | | pops a window for the local human |
| `chat.send` | W | | |
| `chat.rooms` | R | | |
| `chat.poll` | R | s | human replies |
| `claude-history.pull` | R | s | |
| `claude-history.read` | R | s | |
| `claude-history.read_page` | R | s | |
| `claude-history.read_snapshot` | R | s | |
| `claude-history.follow` | R | s | |
| `claude-history.search` | R | s | |
| `claude-history.analyze` | R | s | |
| `claude-history.export` | R | s | |
| `claude-history.resumed` | R | | |
| `claude-history.send` | X | | drives an agent session with tools |
| `claude-history.resume` | X | | relaunches an agent |
| `codex-history.resume` | X | | relaunches an agent |
| `deluge.status` | R | | |
| `deluge.list` | R | | |
| `deluge.files` | R | | |
| `deluge.add` | W | | |
| `deluge.pause` | W | | |
| `deluge.resume` | W | | |
| `deluge.remove` | W | d | can delete data |
| `dongle.status` | R | p | |
| `dongle.display` | R | s,p | |
| `dongle.display_probe` | R | p | |
| `dongle.keyboard` | X | p | keystroke injection = code execution |
| `dongle.mouse` | X | p | |
| `dongle.consumer` | W | p | media keys only |
| `dongle.release` | W | p | |
| `file.read` | R | s | |
| `file.list` | R | | |
| `file.search` | R | s | |
| `file.exists` | R | | |
| `file.write` | X | | arbitrary file write → code execution |
| `hermes.status` | R | | |
| `hermes.memory.status` | R | | |
| `hermes.memory.read` | R | s | |
| `hermes.skills.list` | R | | |
| `hermes.skills.search` | R | | |
| `hermes.sessions.list` | R | | |
| `hermes.sessions.read` | R | s | |
| `hermes.mcp.list` | R | | |
| `hermes.chat` | X | | agent with tools |
| `hermes.run` | X | | agent with tools |
| `hid.backend` | R | | |
| `hid.type` | X | p | |
| `hid.key_combo` | X | p | |
| `hid.mouse.move` | X | p | |
| `hid.mouse.click` | X | p | |
| `hid.mouse.drag` | X | p | |
| `info.host` | R | | |
| `info.uptime` | R | | |
| `info.ping` | R | | |
| `log.audit` | R | s | who called what |
| `log.tail` | R | s | |
| `memory.get` | R | | |
| `memory.search` | R | | |
| `memory.entities` | R | | |
| `memory.put` | W | | caller-owned namespace |
| `memory.note` | W | | |
| `msg.send` | W | | |
| `msg.read` | R | s | |
| `msg.clear` | W | d | |
| `pikvm.snap` | R | s,p | |
| `pikvm.power.status` | R | p | |
| `pikvm.api.get` | R | s,p | |
| `pikvm.type` | X | p | |
| `pikvm.key` | X | p | |
| `pikvm.mouse.move` | X | p | |
| `pikvm.mouse.click` | X | p | |
| `pikvm.power` | X | d,p | ATX power/reset of another machine |
| `pikvm.api.post` | X | d,p | arbitrary KVM API, including power and media |
| `proc.list` | R | | |
| `proc.read` | R | s | |
| `proc.start` | X | | |
| `proc.write` | X | | |
| `proc.signal` | X | d | |
| `proc.close` | X | d | |
| `screenshot.capture` | R | s | |
| `screenshot.capture_region` | R | s | |
| `screenshot.capture_preview` | R | s | |
| `shell.which` | R | | |
| `shell.env.list` | R | s | |
| `shell.env.get` | R | s | |
| `shell.exec` | X | | |
| `agent.wake_info` | R | | |
| `agent.wake` | X | | starts an agent |
| `work.status` | R | | |
| `work.view_page` | R | s | |
| `work.adopt_page` | W | | |
| `work.create` | X | | launches an agent work session |
| `work.command` | X | | drives it |

### A.2 Hub caps (the reserved worker `rook`)

MCP tools map onto hub caps. A tool that forwards to a worker cap is
authorized as *that* cap on *that* worker, plus nothing extra on `rook`.
Proposed hub cap names follow `<area>.<action>`, and the plugin-API task may
rename them. The tiers stand either way.

| MCP tool / surface | Hub cap | Tier | Tags |
|---|---|---|---|
| `rook_whoami` | `identity.whoami` | R | |
| `rook_workers` | `band.workers` | R | |
| `rook_caps` | `band.caps` | R | |
| `rook_call` | *(the target cap on the target worker)* | per target cap | |
| `rook_secret list` | `secret.list` | R | |
| `rook_secret log` | `secret.log` | R | s |
| `rook_secret get` | `secret.get` | **A** | s |
| `rook_secret set` / `delete` | `secret.set` / `secret.delete` | A | d |
| `{{secret:name}}` in args | `secret.use` (target = secret name, checked in addition to the call) | W | s |
| `rook_journal` | `journal.read` | R | s |
| `rook_handoff_get` / `_list` | `handoff.read` | R | |
| `rook_handoff_save` | `handoff.write` | W | |
| `rook_chat_rooms` / `_read` | `chat.read` | R | |
| `rook_chat_start` / `_send` | `chat.write` | W | |
| `rook_chat_delete` | `chat.delete` | W | d |
| `rook_presence` | `chat.presence` | R | |
| `rook_chat_wake` | → `agent.wake` / `hermes.chat` on the target worker | X | |
| `rook_console_list` / `_search` | `console.read` | R | |
| `rook_console_read` | `console.read` | R | s |
| `rook_console_open` | → `proc.start` on the target worker | X | |
| `rook_console_write` / `_signal` / `_close` | → `proc.write` / `proc.signal` / `proc.close` | X | |
| `rook_config_get` | → `worker.config_get` | R | |
| `rook_config_apply` | → `worker.config_apply` / `_confirm` | A | d |
| `rook_knowledge` search/get/list/context/status | `knowledge.read` | R | |
| `rook_knowledge` create/update/link/retract | `knowledge.write` | W | |
| `rook_task` / `rook_project` / `rook_concept` deck/get/list/search | `task.read` | R | |
| `rook_task` / `rook_project` / `rook_concept` create/update/claim/release/link/retract/review | `task.write` | W | |
| `grants.revocations` (new) | `grants.revocations` | R | |
| `policy.explain` (new) | `policy.explain` | R | |
| `policy.get` (new) | `policy.get` | R | s |
| `policy.set` (new; dashboard Permissions page) | `policy.set` | A | |
| Dashboard `/api/call` | *(the target cap)* | per target cap | |
| Dashboard: mint/rotate/revoke tokens | `token.admin` | A | |
| Dashboard: band PSK rotate/revoke, pairing, migrations | `band.admin` | A | d |
| Dashboard: deauth/unban | `band.deauth` | A | d |
| Dashboard: members and invitations | `member.admin` | A | |
| Dashboard: OTA push (`push_update`) | → `worker.ota_begin` | A | |
| Dashboard: guidance / agent instructions edit | `guidance.write` | A | shapes every agent's instructions |
