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
- A hub setting `jobs.timezone` (IANA name, default `America/Toronto`;
  the hub is in Montreal) is the default zone for schedules and display.
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
  - `manual` means the job only runs from `job.run`.
- **Overlap.** The default is `queue` with no limit (`max_queue: null`). A
  job may set a limit; a trigger that overflows a set limit is recorded as a
  run with state `dropped`.
- **Missed runs**, for example after the hub was down: `run_once` within
  `grace` is the default. A missed run is recorded with `missed: true`, and
  the graph can branch on it, since `run.missed` is a variable.
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
  "…kind-specific fields…"
}
```

**Step kinds:**
- `cap`: `{"worker": "kaiju" | "rook" (hub) | {"any_with_cap": true} | {"filter": {...}}, "cap": "shell.exec", "args": {...}}`.
  - A blocking cap waits for its result, or its exit code, up to the timeout.
  - An offline worker is retried until the timeout runs out, and only then
    counts as a hang.
- `fanout`: the same as `cap`, but run on every worker matching a filter
  (`{"has_cap": "...", "os": "...", "names": [...], "tags": [...]}`). The step
  succeeds by its join rule: `all` (the default), `any`, or `{"at_least": N}`.
- `tool`: an MCP-tool style action on the hub, such as `rook_task` with
  `action: "deck"`. It is routed through the plugin's `invoke(cap, args)` so
  attribution matches.
- `agent`: hands a prompt to an agent. Section 6 covers it.
- `wait`: a fixed delay, or until a set time.
- `notify`: sends to Bake by Rook voice, phone notification or Telegram
  (`{"via": "voice|notify|telegram", "text": "…"}`). It uses the same caps
  as the server instructions (bakephone `voice.speak` / `notify.post`).
- `ask`: a voice or notification question that waits for a reply (`reply`
  with a timeout). The reply text lands in `steps.<id>.reply`. No answer is
  `failure`, so it branches.
- `join`: waits for its incoming branches. Its condition is `all`, `any`,
  `{"at_least": N}`, or an expression over earlier step outcomes, e.g.
  `steps.a.ok and not steps.b.ok`. Expressions use a small safe evaluator
  (no `eval`), with the same restrictions as `policy.py`'s `ast` use.
- `noop`: used as a branch label or an end point.

**Branching:**
- After a step, every step listed under the outcome key runs. They run in
  parallel when there are several. Outcome keys are `success`, `failure` and
  `hang`.
- Retries happen before branching. A failure branch may point back to an
  earlier step to loop, limited by a per-run cap on step executions
  (default 100) so a bad graph cannot spin forever.
- The graph is validated on save: every step reachable from `entry`, no
  dangling ids, known kinds, a timeout on every step (default `5m`),
  join inputs that exist.

**Success** defaults to the cap's `ok`. For exec-like results, exit codes
`[0]` count as success. `match` / `not_match` are regexes on the
stdout/result text. Anything more complex is an agent step.

**Variables:**
- `{{secret:name}}` resolves at execution time through the standard vault
  substitution. Resolved values are never stored. Run logs are masked with
  the existing secret masker (`band_mcp/secret_mask.py`).
- `{{run.id}}`, `{{run.started}}`, `{{job.name}}` and `{{vars.x}}` are
  available.
- Step outputs are kept in the run record. Chained cap steps do **not**
  template earlier output into args. Output is for agents (section 6) and
  for join conditions (`steps.<id>.ok`, `.exit_code`, `.state`).

**Run record:**
- `{id, job_id, trigger, missed, state: queued|running|success|failure|hang|interrupted|dropped|blocked, started, finished, identity_used, steps: {id: {state, started, finished, attempts, output (truncated, masked), error}}}`.
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
  `jobs.default_agent` can change that, and a step can override it.
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

**Identity**, i.e. who a run acts as:
- `creator` (the default): the principal that created the job.
- `key`: a named API key or user.
- `vault`: a credential held in the vault (`{{secret:name}}`, such as a
  Rook API key), resolved at run time.
- `fallback`: a second identity used when the first is revoked or disabled.
  With no fallback, the job is paused (`enabled: false`, reason
  `identity_revoked`).
- Editing a job someone else owns resets `identity` to the editor's, unless
  the editor is the owner or the operator. This rule is fixed, not
  configurable: it stops anyone borrowing another identity through the open
  edit default.

**Access:**
- `access.read/edit/run` hold principal patterns. The default `"*"` means
  any authenticated Rook principal.
- The hub setting `jobs.default_access` applies to new jobs.

**Guardrails:**
- These are policy rules evaluated at each step's execution, through
  `rook/hub/policy.py`, with the job as a principal kind (`job:<id>`) in the
  on-behalf-of chain.
- The default deny list is a hub setting (`jobs.guardrails`). It starts with:
  - every `admin`-risk cap;
  - deauth, re-band and enrollment changes;
  - `selfupdate.*` and worker updates;
  - hub service restart or deploy caps;
  - policy and guardrail edits;
  - permanent deletes.
- `exec` (including `shell.exec`) and vault writes are **allowed** by
  default.
- Jobs with `guardrails.inherit: true` (the default) follow later changes
  to the defaults. Per-job `allow` / `deny` lists layer on top, and `allow`
  can only be set by the operator.
- Before a change to the defaults is saved, `job.guardrails_preview` lists
  the jobs whose steps it would now block. The dashboard shows this before
  saving.
- A blocked step ends with state `blocked` (not `failure`), and the job is
  flagged on the overview.

## 8. Interfaces

**Caps (worker `rook`):**
- `job.read` (risk `read`) takes `action` = `list | get | runs | run_get | next | validate | guardrails_preview | describe_schema`.
- `job.write` (risk `write`) takes `action` = `create | update | delete | enable | disable | run | cancel | set_guardrails | settings`.

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
on behalf of the job, sessions integration, `jobs.default_agent`, and
supervised-mode docs with examples (e.g. "poke an agent to work the task
deck every hour").

J2, J3 and J4 start after J1 is merged, and they touch separate modules.

## 10. Rules for every PR

- Base branch `beta-jobs`. Never commit `android/rook.properties`, the
  private wake model, secrets, real host names, user names or home paths.
- Tests never run with the real `HOME`: use a scratch `HOME`.
- No deploys and no changes to live hosts from a workstream.
- Update this file when a contract changes, and the user docs for what
  people see.
