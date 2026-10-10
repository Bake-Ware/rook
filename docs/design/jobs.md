# Jobs: scheduled, branching work owned by the hub

Status: design agreed with Bake 2026-10-10. Built in four workstreams on
branch `beta-jobs` (section 9).

## 1. What Bake asked for

A hub plugin, `jobs`, that runs scheduled work. Any Rook client can create,
read and run jobs through MCP. The hub owns the schedule, the run state
and the history. Everything is configurable; defaults are listed here and
each one can be changed on the dashboard.

- A job can fire anything Rook can fire: a cap on a worker, a cap on the
  hub, a fan-out across workers, an MCP-tool action, an agent prompt, a
  notification, a wait.
- A job is a graph of steps. Each step has its own timeout and branches on
  success, failure and hang (timeout). Branches can join with conditions.
- Some jobs are *agent jobs*: the house agent (or another configured agent)
  runs the step, sees the run's output, adapts and can reach Bake.
- Guardrails block dangerous caps by default, and the guardrail defaults are
  themselves configurable.

## 2. The plugin

- `rook/hub/plugins/jobs/` (package), `NAMESPACE = "job"`, `NAME = "jobs"`,
  `PLACEMENT = place("is_hub", run="one")`, like `knowledge` and `tasks`.
  It runs in the MCP service (`rook-band-mcp`), so it deploys with
  `--services dashboard,mcp`.
- Storage: its own SQLite database in the hub data directory, with numbered
  migrations under `jobs/migrations/` (same pattern as knowledge).
- No new third-party dependencies. Cron parsing is implemented in the
  plugin; time zones use stdlib `zoneinfo`.
- One scheduler loop per hub (`run="one"`). It must be safe for clustered or
  HA hubs later: claim a due run with a single atomic DB update
  (`UPDATE ... WHERE state='due' AND lease_until < now`) holding a lease, so
  two hub processes never start the same run.

## 3. Time

- All stored instants are UTC (ISO 8601 with offset, or epoch seconds).
  Never naive datetimes.
- A hub setting `job.timezone` (IANA name, default `America/Toronto`;
  the hub is in Montreal) is the default zone for schedules and display.
  Setting keys take the plugin's namespace, `job`, so every jobs setting is
  `job.<name>` (`job.timezone`, `job.retention_days`, …).
  Never use the host clock's zone: the VM runs in UTC.
- Each schedule may carry its own `tz`, which overrides the hub's.
- Cron is evaluated in the schedule's zone, then converted to UTC. DST rules:
  a local time that does not exist (spring forward) fires at the next valid
  instant once. A local time that happens twice (fall back) fires once, on
  the first occurrence. These need tests.
- The API and UI accept and return zone-aware strings. The UI shows local
  time plus the zone.

## 4. The job

```json
{
  "id": "j_…", "name": "nightly-backup", "description": "…",
  "enabled": true,
  "owner": "<principal id of the creator>",
  "identity": {"mode": "creator|key|vault|fallback", "ref": "…", "fallback": "…"},
  "triggers": [
    {"kind": "cron", "expr": "0 3 * * *", "tz": "America/Toronto"},
    {"kind": "at", "when": "2026-10-11T09:00:00-04:00"},
    {"kind": "after", "every": "15m", "on": "success|finish|failure"},
    {"kind": "manual"}
  ],
  "overlap": {"mode": "queue|skip|parallel", "max_queue": null},
  "missed": {"mode": "run_once|skip|all", "grace": "10m"},
  "retention_days": 30,
  "access": {"read": "*", "edit": "*", "run": "*"},
  "guardrails": {"inherit": true, "allow": [], "deny": []},
  "alerts": {"on_failure": [], "on_success": []},
  "entry": "check",
  "steps": { "<step id>": { … see section 5 … } },
  "vars": {}
}
```

- **Triggers.**
  - `cron` is 5-field cron with the usual `*`, lists, ranges and steps, plus
    `@hourly`, `@daily`, `@weekly` and `@monthly`.
  - `at` is a one-shot at a set time.
  - `after` re-runs a set interval after the last run finished, filtered by
    its outcome. This is "retrigger X after success".
  - `manual` means the job only runs from `job.run`. A job with no
    triggers is manual.
  - An `after` trigger needs a finished run to count from, so pair it with
    another trigger (or start the first run by hand). An `at` time already in
    the past is saved with a warning and never fires.
- **Overlap.** The default is `queue` with no limit (`max_queue: null`). A
  job may set a limit; a trigger that overflows a set limit is recorded as a
  run with state `dropped`.
- **Missed runs**, for example after the hub was down: `run_once` within
  `grace` is the default. A missed run is recorded with `missed: true`, and
  the graph can branch on it, since `run.missed` is a variable.
  - A fire more than 90 seconds late is missed. Missed fires older than
    `grace` never run.
  - `run_once` runs the latest missed fire within `grace`; `all` runs every
    one (subject to overlap); `skip` runs none and records one `dropped` run
    with `missed: true` saying how many were skipped. A fire that is on time
    always runs.
- **Retention:** run history older than `retention_days` (default 30, a
  setting with a per-job override) is pruned by the scheduler loop.

## 5. Steps and the graph

Each step has this shape:

```json
{
  "kind": "cap|fanout|tool|agent|wait|notify|ask|join|noop",
  "timeout": "5m",
  "on": {"success": ["next"], "failure": ["alert"], "hang": ["alert"]},
  "success": {"exit_codes": [0], "match": "regex", "not_match": "regex"},
  "retry": {"max": 2, "delay": "30s"},
  "allow_failure": false,
  "…kind-specific fields…"
}
```

**Step kinds:**
- `cap`: `{"worker": "<worker name or id>" | "rook" (hub) | {"any_with_cap": true} | {"filter": {...}}, "cap": "shell.exec", "args": {...}}`.
  - A blocking cap waits for its result, or its exit code, up to the timeout.
  - An offline worker is retried until the timeout runs out, and only then
    counts as a hang. A name that matches two live workers is a failure.
  - `any_with_cap` and `filter` pick the first matching live worker by name.
  - A policy denial on the call (`denied` in the reply) ends the step
    `blocked`.
- `fanout`: the same as `cap`, but run on every worker matching a filter
  (`{"has_cap": "...", "os": "...", "names": [...], "tags": [...]}`). The step
  succeeds by its join rule: `all` (the default), `any`, or `{"at_least": N}`.
- `tool`: an MCP-tool style action on the hub,
  `{"tool": "rook_task", "args": {"action": "deck"}}`. It calls the hub
  plugins' own `mcp_tools(invoke)` tool, with `invoke(cap, args)` running the
  cap in process as the run's identity, so attribution matches. A JSON reply
  with `ok: false` is a failure.
- `agent`: hands a prompt to an agent. Section 6 covers it.
- `wait`: a fixed delay, or until a set time: `{"for": "10m"}` or
  `{"until": "2026-10-11T09:00:00-04:00"}`. The step's timeout must be
  longer than the wait.
- `notify`: sends to Bake by Rook voice, phone notification or Telegram
  (`{"via": "voice|notify|telegram", "text": "…", "title": "…", "worker": …}`,
  `via` defaults to `notify`). It uses the same caps as the server
  instructions: `voice.speak` (with `wait: false`) or `notify.post` on the
  step's `worker`, else the hub setting `job.notify_worker`, else the first
  live worker with the cap; `telegram` is `notify.send` on the hub with
  `channel: "telegram"`. No worker name is built in.
- `ask`: a voice or notification question that waits for a reply (`reply`
  with a timeout). The reply text lands in `steps.<id>.reply`. No answer is
  `failure`, so it branches.
- `join`: waits for its incoming branches. Its `condition` is `all` (the
  default), `any`, `{"at_least": N}`, or an expression over earlier step
  outcomes, e.g. `steps.a.ok and not steps.b.ok`. Expressions use a small
  safe evaluator (no `eval`), with the same restrictions as `policy.py`'s
  `ast` use: `and`, `or`, `not`, comparisons and `in`, constants, and
  lookups under `steps`, `run`, `vars` and `job` (`steps['id-with-dash']`
  for ids that are not identifiers).
  - An incoming branch *arrives* when its step finishes with an outcome
    whose list names the join. `all` fires when every incoming branch has
    arrived, `any` on the first, `{"at_least": N}` on the Nth. An
    expression is evaluated once every branch has arrived.
  - When nothing else in the run is still working, a waiting join settles:
    an expression is evaluated, and a count that was not reached is a
    `failure` (so it can branch).
  - A join fires once per run. Validation refuses a join inside a loop and
    a join as the entry.
- `noop`: used as a branch label or an end point.

**Branching:**
- After a step, every step listed under the outcome key runs. They run in
  parallel when there are several. Outcome keys are `success`, `failure` and
  `hang`.
- Retries happen before branching, on `failure` and `hang` (never on
  `blocked`). A failure branch may point back to an earlier step to loop,
  limited by a per-run cap on step executions (setting
  `job.max_step_executions`, default 100) so a bad graph cannot spin
  forever. Going over it ends the run `failure`.
- A `blocked` step ends its branch.
- The graph is validated on save: every step reachable from `entry`, no
  dangling ids, known kinds, a timeout on every step (default `5m`),
  join inputs that exist.

**Success** defaults to the cap's `ok` (a result that itself says
`ok: false` is a failure too). For exec-like results, exit codes
`[0]` count as success. `match` / `not_match` are regexes on the
stdout/result text. Anything more complex is an agent step.

**Variables:**
- `{{secret:name}}` resolves at execution time through the standard vault
  substitution. Resolved values are never stored. Run logs are masked with
  the existing secret masker (`band_mcp/secret_mask.py`).
- `{{run.id}}`, `{{run.started}}`, `{{run.missed}}`, `{{job.id}}`,
  `{{job.name}}` and `{{vars.x}}` are available. `job.run` may pass
  `data.vars`, which override the job's `vars` for that run. An unknown
  variable is left as written.
- The call journal records each step's call with the placeholders, never
  the values.
- Step outputs are kept in the run record. Chained cap steps do **not**
  template earlier output into args. Output is for agents (section 6) and
  for join conditions (`steps.<id>.ok`, `.exit_code`, `.state`).

**Run record:**
- `{id, job_id, trigger, missed, state: queued|due|running|success|failure|hang|interrupted|dropped|blocked|cancelled, scheduled, created, started, finished, identity_used, vars, executions, error, alerts, steps: {id: {state, started, finished, attempts, runs, exit_code, output (truncated to 4000 characters, masked), error}}}`.
- `due` is a run waiting to be claimed; `queued` waits behind an active run
  of the same job. `cancelled` comes from `job.cancel`.
- The run's state is the worst final step state: `blocked`, then `hang`,
  then `failure`, then `success`. A step with `"allow_failure": true` does
  not count.
- `alerts.on_success` / `alerts.on_failure` are lists of notify specs
  (`{via, text, title?, worker?}`) sent after the run; `on_failure` covers
  every state but `success` and `cancelled`. What each one did is kept in
  the run's `alerts`.
- On hub start, runs left `running` are marked `interrupted`, and that
  counts as a failure for branching. They are not resumed.

## 6. Agent steps and supervised jobs

`agent` step:

```json
{"kind": "agent", "agent": "home" | {"session": {...sessions.md spec...}},
 "prompt": "…", "context": ["run", "steps.check"], "mode": "delegate|wait",
 "tools": "job" , "model": null}
```

- The default agent is the house agent (`home` plugin). The hub setting
  `job.default_agent` can change that, and a step can override it.
  `model` is optional and defaults to the agent's own model.
- `{"session": …}` starts or pokes a Claude/Codex session through the
  sessions verbs (docs/design/sessions.md 3.2), on a worker chosen like a
  `cap` step.
- `context` chooses which run output the agent is given: the run so far,
  particular steps, or the job definition.
- `mode`:
  - `delegate`: the step succeeds once the hub has handed the prompt off,
    and the graph moves on.
  - `wait`: the step waits for the agent's final answer (up to its timeout).
    The agent's verdict (`ok` / `failed` plus text) sets the outcome.
- The house agent needs MCP tools for this. The `home` plugin gains an
  opt-in tool set: the same caps an MCP client has, called as the house
  agent identity *on behalf of* the job's identity. The policy chain is
  `home` → job → job identity, so it can never exceed what the job could do.
  `tools: "job"` (the default) grants it the job's own permissions.
  Everything it does is journaled.
- To reach Bake, an agent uses the normal notify/voice caps. A job can also
  model this as explicit `notify` / `ask` steps.

## 7. Identity, access and guardrails

Built in J2 (`identity.py`, `principals.py`, `guardrails.py`, `service.py`).

**Identity**, i.e. who a run acts as. A job's `identity` block is
`{"mode": …, "ref": …, "fallback": …}`:
- `creator` (the default): the job's owner (`owner_info`): the principal
  that created it, or the last non-owner who edited it (below).
- `key`: a named API key or user. `ref` is `token:<agent_id>`,
  `human:<id>`, a key id, an agent id or a key's unique name.
- `vault`: a Rook API key held in the vault. `ref` is a secret name or
  `{{secret:name}}`. The key is read at run time (the vault logs the read as
  `job:<id>`) only to find its principal; it is never stored or shown.
- `fallback`: the run always acts as the fallback identity.
- `fallback` (field): used when the primary identity is revoked, expired or
  deleted. It is `"creator"`, a key reference, `"{{secret:name}}"` or a
  `{mode, ref}` object. A job that names none uses the hub setting
  `job.default_fallback` (empty by default).
- Revocation is checked at every run against the hub's live stores: the MCP
  token store (revoked or expired keys) and the dashboard accounts (deleted
  users). If the primary is unusable and a fallback works, the run uses it
  (`identity_used` ends in ` (fallback)`). If nothing usable is left, the run
  is recorded `blocked` and the job is paused (`enabled: false`,
  `paused_reason: identity_revoked`, with a history row). A hub that cannot
  check a key at all (no token store, no vault) blocks the run without
  pausing the job.
- Who may give a job a `key` or `vault` identity (or fallback): the
  operator, or the principal that key *is* (or owns). Creating a job with
  someone else's key needs the operator. An identity that is unchanged by an
  edit is not re-checked.
- Editing a job someone else owns (`update`, `enable`) hands it to the
  editor: the owner becomes the editor, `identity` resets to
  `{"mode": "creator"}` (unless the edit sets an identity the editor may
  use), and `guardrails.allow` is cleared. The owner and the operator keep
  everything as it is. The history row (`history` on `get`) records
  `identity_reset: {from, to}`, and the reply carries it too. This rule is
  fixed, not configurable: it stops anyone borrowing another identity
  through the open edit default. `disable` does not hand a job over.
- The operator is what `require_hub_admin` accepts: band owners
  (`human:owner`, dashboard admins), operator-role tokens, the static token
  and in-process hub code.

**Access:**
- `access.read/edit/run` hold principal patterns, as a string (comma or
  space separated) or a list: `*` (any authenticated principal), an exact
  id (`token:agent_…`, `human:<id>`), a glob (`token:*`), `role:<role>` or a
  group (`human:owner`, `human:member`). The default is `*` for all three.
- The hub setting `job.default_access` applies to new jobs that do not set
  `access`.
- The owner and the operator always have full access. `list` and `next`
  leave out jobs the caller cannot read; `get`, `runs`, `run_get` and
  previews need read; `update`, `delete`, `enable`, `disable` and per-job
  `set_guardrails` need edit (so changing `access` does too); `run` and
  `cancel` need run. Job views carry `can: {edit, run}` for the caller.

**Guardrails:**
- Policy rules evaluated through `rook/hub/policy.py`, with the job as a
  principal (`job:<id>`, its run identity in the on-behalf-of chain). Three
  layers, in order:
  1. the job's `guardrails.deny`: a match blocks;
  2. the job's `guardrails.allow` (operator only): a match allows, even over
     a default deny;
  3. the defaults: `job.guardrails` for jobs with `inherit: true` (the
     default), else the copy saved as `guardrails.base` when the job stopped
     inheriting (only the operator can choose a different base). Within the
     defaults the most specific rule wins, so `allow: ["secret.set"]` beats
     `deny: ["tier:admin"]`.
  Then the hub's own policy: an operator rule whose `who` is `job:*` or
  `job:<id>` and that denies the call in `enforce` mode blocks it too (tier
  defaults of the hub policy do not count).
- An entry is a cap selector as in a policy rule: exact (`worker.update`),
  glob (`selfupdate.*`), `tier:admin`, `tag:destructive`, or
  `<cap>:<action>` for action-style caps (`job.write:delete`). An object
  `{"cap": …, "on": <target selector>, "note": …}` limits it to some
  workers. A call whose args carry `action` is checked as the cap and as
  `<cap>:<action>`; either blocked blocks it. A cap's tier comes from the
  built-in table, the worker's announced tiers or the hub cap's risk.
- Where it is checked: before each step, statically, as far as the step
  names its cap and worker (`cap`, `fanout`, `tool` via the tool's hub cap,
  `notify`); and at every call with the worker it actually resolved to (a
  fan-out checks each worker, and a refused worker counts as `blocked` in
  the join; a `tool` step checks every cap the tool calls). A refused call
  is journaled with `denied.guardrail`, never sent, and the step ends
  `blocked` without retries.
- The default deny list (setting `job.guardrails`, `{"deny": [...],
  "allow": [...]}`):
  - every admin-risk cap: `tier:admin`;
  - deauth, re-band and enrollment: `worker.deauth`, `band.deauth`,
    `band.admin`, `member.admin`, `token.admin`, `worker.reconfigure`,
    `worker.config_apply`, `worker.config_revert`, `worker.config_confirm`,
    `worker.enrollment_prepare`, `worker.enrollment_move_prepare`,
    `worker.enrollment_finish`, `worker.enrollment_prove`, `enrollment.*`;
  - self-update and worker updates: `selfupdate.*`, `worker.update`,
    `worker.apply`, `worker.check`, `worker.ota_begin`, `worker.hold`,
    `worker.restart`, `worker.plugin.enable`, `worker.plugin.disable`,
    `customcap.add`, `customcap.remove`, `settings.apply_worker`;
  - hub service restart or deploy: `hub.restart`, `hub.restart_*`,
    `hub.deploy`, `hub.deploy_*`, `hub.update`, `service.restart*`,
    `*.deploy`;
  - policy and guardrail edits: `policy.set`, `settings.set`,
    `settings.reset`, `guidance.write`, `job.write:set_guardrails`,
    `job.write:settings`;
  - permanent deletes: `secret.delete`, `persona.delete`, `chat.delete`,
    `msg.clear`, `deluge.remove`, `job.write:delete`, `*.delete`,
    `*.purge`, `*:delete`, `*:purge`.
  - `allow: ["secret.set"]`: vault writes are allowed. `exec` caps
    (`shell.exec`, `proc.*`, `cmd.*`, `file.write`) are not denied.
  A missing or invalid setting means the built-in list: guardrails never
  fail open.
- `job.read guardrails_preview`:
  - without `id`: `data` = `{deny, allow}` (or `{defaults: {deny, allow}}`,
    or `{reset: true}` for the built-in list), the proposed defaults;
  - with `id`: `data.guardrails` = the job's proposed `guardrails` block.
  - Reply: `{scope: "defaults"|"job", proposed, newly_blocked, unblocked,
    blocked, jobs_newly_blocked, checked}`. `newly_blocked`, `unblocked` and
    `blocked` (everything blocked under the proposal) are lists of
    `{job_id, job, step, cap, target, rule, reason}`; `jobs_newly_blocked`
    is `[{id, name, enabled, steps}]`. Only jobs the caller can read are
    checked; jobs that do not inherit are unaffected by a default change.
- `job.write set_guardrails`: without `id`, saves the defaults (operator
  only; same `data` as the preview) and returns `{defaults, preview}`; each
  newly blocked job gets a `guardrail_blocked` history row. With `id`,
  `data.guardrails` replaces that job's block (edit access; `allow` is
  operator only) and returns the job view.
- Every job view carries `blocked_by_guardrail` (true when a step would be
  blocked now) and, when true, `guardrail_blocks` (`[{step, cap, target,
  rule, reason}]`). `list` takes `data.blocked: true|false`. The dashboard
  overview counts these jobs as blocked.
- Saving or validating a job whose step would be blocked now is a
  warning (`warnings`), not an error.

## 8. Interfaces

**Caps (worker `rook`):**
- `job.read` (risk `read`) takes `action` = `list | get | runs | run_get | next | validate | guardrails_preview | describe_schema`.
- `job.write` (risk `write`) takes `action` = `create | update | delete | enable | disable | run | cancel | set_guardrails | settings`.
- Arguments are `action`, `id`, `query` (reads) and `data`. `id` is a job
  id or name; for `run_get` and `cancel` it is a run id (`cancel` with a job
  id cancels all of that job's waiting and running runs).
  - `create` / `validate`: `data` is the job. `update`: `data` holds the
    fields to replace, plus an optional `revision` that refuses the write if
    someone changed the job since.
  - `runs`: `data {states, limit, since, missed, steps: true}`. `next`
    without `id` lists upcoming fires across jobs; with `id`, each trigger's
    next `count` fires in its own zone and the hub zone. Without `id` but
    with `data.trigger` (an unsaved `cron` or `at` trigger), it previews
    that trigger the same way: `{trigger, reads, zone, timezone, next:
    [{local, hub}]}` (the editor's cron helper).
  - `run`: `data {vars}`. A disabled job does not run. `disable`:
    `data {reason}`.
  - `settings`: no `data` reads the `job.*` settings; `data {name: value}`
    writes them and is for owners and operator tokens.
- A read cap refuses a write action, so the band's read ceiling holds.

**MCP tool:**
- `rook_jobs(action, id, data, query)` is action-style, like `rook_task`,
  provided through `mcp_tools(invoke)`.
- Agents can read run logs (masked) through it.
- `describe_schema` returns the job JSON schema so agents can write jobs
  without guessing.

**Dashboard:**
- A Jobs page in the account area, next to Knowledge and Vault. It follows
  the minimal-UI rule: no chatty status, only actionable notices.
- Tabs:
  - **Overview** (the default): widgets for running now, next runs, recent
    failures, blocked jobs, success rate over 7 days, and a queue depth.
  - **Jobs**: list, enable/disable, run now.
  - **Editor**: raw JSON first, with validation errors inline, plus a
    human-readable cron helper. It shows the next 5 fire times in the
    schedule's zone and the hub zone.
  - **Runs**: filterable log with per-step output (masked).
  - **Guardrails**: defaults, with the preview before saving.
  - **Settings**: timezone, retention, default agent, default access,
    default identity fallback.
- A visual graph editor comes later.

**CLI:** a `rook band` jobs panel with list, runs, run now, enable/disable
and view JSON.

## 9. Workstreams (each one PR into `beta-jobs`)

**J1 – core** (first; everything else builds on it):
- plugin skeleton, storage and migrations, the time and cron module, the
  scheduler loop with leases, the graph executor;
- step kinds `cap`, `fanout`, `tool`, `wait`, `notify`, `join`, `noop`;
- retries, timeouts, branching, overlap, missed runs, retention, the run
  record, secret substitution and masking;
- the `job.read` / `job.write` caps, the `rook_jobs` MCP tool, and tests;
- identity is `creator` only, and the guardrail hook is a stub that allows
  all.

**J2 – identity and guardrails:** the `key` / `vault` / `fallback`
identities, revocation handling, access patterns, the edit-resets-identity
rule, policy integration, the default deny list, inheritance, and
`guardrails_preview`.

**J3 – dashboard and CLI:** the Jobs page tabs and widgets, the raw editor
with the cron helper, and the `rook band` panel.

**J4 – agents:** the `agent` and `ask` step kinds, house-agent MCP tools
on behalf of the job, sessions integration, `job.default_agent`, and
supervised-mode docs with examples (e.g. "poke an agent to work the task
deck every hour").

J2, J3 and J4 start after J1 is merged, and they touch separate modules.

**J1 as built** (`rook/hub/plugins/jobs/`):

| Module | What it holds |
|---|---|
| `__init__.py` | the plugin: settings, caps, `rook_jobs`, start/stop of the loop |
| `cron.py` | durations, zone-aware instants, 5-field cron, DST rules |
| `model.py` | defaults, validation on save, `describe_schema` |
| `store.py`, `migrations/` | `jobs.db`: jobs, trigger state, runs; atomic fires and claims |
| `steps.py` | the step-kind registry, `StepContext`, success rules |
| `kinds.py` | the built-in kinds |
| `expr.py` | the join-condition evaluator |
| `executor.py` | walks one run's graph |
| `scheduler.py` | the loop: leases, triggers, missed runs, queue, retention |
| `runtime.py` | band calls, hub tools, vault substitution, the journal |
| `identity.py`, `principals.py` | run identities, revocation, who may set them (J2) |
| `guardrails.py` | guardrail evaluation, the default deny list, preview scans (J2) |
| `service.py` | the `job.read` / `job.write` actions |

Seams for later workstreams:
- **J2** replaces `identity.resolve_identity(job, owner_info) -> RunIdentity`
  (J1: `creator` only; other modes fail validation) and
  `guardrails.check_step(job, step, identity) -> Verdict(allow, reason, rule)`
  (J1: always allows). The executor calls `check_step` before every step. J2
  also fills in `guardrails_preview` and `set_guardrails`. Done: section 7
  describes what was built. Step kinds that call caps should go through
  `ctx.runtime` (the executor hands each step a guarded runtime) or the hub
  tool `invoke`, so the per-call guardrail check applies.
- **J4** registers its kinds with
  `steps.register_step_kind(name, handler, validate=..., schema=...)`. A
  handler is `async (ctx: StepContext, step) -> StepResult`. `agent` and
  `ask` validate as "not available on this hub yet" until they are
  registered. `StepResult.extra` holds fields kept on the step record, such
  as `reply`.
- **J3** reads everything through `job.read` / `job.write`.

Storage: `jobs.db` in the plugin data dir (`<state>/plugins/job/`, setting
`job.db_path`). The plugin is on by default (`job.enabled`, env
`ROOK_JOBS`) and stays off on a hub without a state directory.

## 10. Rules for every PR

- Base branch `beta-jobs`. Never commit `android/rook.properties`, the
  private wake model, secrets, real host names, user names or home paths.
- Tests never run with the real `HOME`: use a scratch `HOME`.
- No deploys and no changes to live hosts from a workstream.
- Update this file when a contract changes, and the user docs for what
  people see.
