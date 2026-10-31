# Design-drift audit, part B: product layers

Claude Code, Oct 31 2026, branch `c7/audit-B` (report only, no code changed).
Asked by Bake, Oct 31 12:07-12:08: "I thought we decided against merge votes"
... "Check all the work going in so far for similar drift like this".

**Scope:** castling-agents, castling-attention, castling-workbench,
castling-dashboard, castling-inference, castling-streams, castling-jobs,
castling-design (components and products), castling-caps (the C1-C6 calls
Bake accepted), castling-switchover-decisions.

**Checked against:** `origin/castling` at 579d323a (PR #329 merged), the open
PRs into castling (#326-#328, #331, #332), and the wave prompts in
`~/rook-backups/castling-wave1..7/prompt-*.txt`.

**What counts as drift:** what's built contradicts a decision, quietly
ignores it, renames it, or builds more machinery than the decision asked for.
Bake has cut complexity more than once ("too much complexity", "way off the
rails"), so over-building counts too.

## How the merge votes got back in

This is the root cause of F1, and the same pattern produced F3, F4 and F12.

1. `docs/castling-switchover.md` (wave 6, #307) listed gate **R14 "Huddles
   with a merge vote"**. Its reason was "Agents land PRs today through the
   0.4 huddle's `huddle.vote` and the chair's pin." Recommendation D5 then
   held the cutover for "R1-R5, R10 and R14".
2. Bake's Oct 31 10:17 "Yes use your recommendations" accepted D5 as a
   whole. R14 rode along inside it, and nobody showed it to him as a reversal
   of castling-workbench.
3. The wave 7 prompt (`castling-wave7/prompt-huddles.txt:29`) read the
   decision and overrode it on purpose: "castling-workbench (sign-off
   replaces merge votes, **but agents need this flow to keep landing work
   after 0.4 stops**). Build the 0.5 equivalent ... Keep the cap names agents
   use today (huddle.pin, huddle.vote, huddle.table, huddle.post)".
4. The process that builds castling depends on the 0.4 flow. Every wave
   prompt's boilerplate (lines 23-24 of each `prompt-*.txt`) tells agents to
   ask the chair to pin and then call `huddle.vote`. Because of that, the old
   flow looked like a blocker instead of a decision to retire.

The same failure shows up elsewhere: a prompt summarised a decision too
loosely ("producer", "Claude Code CLI (and a fake backend)", "include a
migration script"), and the agent built what the summary said rather than
what the page said.

## Findings, most severe first

| # | Severity | What | Where |
|---|---|---|---|
| F1 | contradiction, over-built | Huddles rebuilt as a merge-vote room: chair, pins, table, motions, gates | #329: `rook/plugins/huddle`, `rook/plugins/github`, `agents/skills/chair.md` |
| F2 | missing, contradiction | Sign-off, the agreed replacement, is built but nothing uses it; agents can't do GitHub reviews | `rook/plugins/tasks`, `rook/plugins/github` |
| F3 | renamed | The "producer" became a release manager | `agents/personas/producer.md` |
| F4 | missing | No Codex backend (and no local or API backend), though the enum offers them | `rook/plugins/agents*`, `llm-cli` |
| F5 | missing | Rook-run agents don't act as their own identity; persona grants aren't what the gate checks | `agents/scripts/agents.py:26-29`, `agents-claude-code` |
| F6 | over-built | The 0.4 migration ships as product code and a manual page | `rook/core2/migrate`, `knowledge/scripts/import_04.py`, `docs/manual-0.5/migrating.md` |
| F7 | missing | Skills live only in plugin files, not in the knowledge store; the concept pool's skills can't be loaded | `agents.py:416-433, 643` |
| F8 | missing, package drift | Only the agents plugin's own manifest can ship personas; the workbench roles live in agents; the manual says otherwise | `agents.py:158-175, 401-410, 464`; `manual-0.5/agents.md:75` |
| F9 | missing, reinvented | C4 `approval.ask` and rule-10 `page.show` aren't built; jobs and huddles each built their own ask | builtin `approval`, `jobs.py:605`, `huddle carry` |
| F10 | renamed | Streams: hub streams have no `stream.read/write`; each plugin has its own verbs; no chat rooms on 0.5 | `core2/builtin/stream.yaml`, huddle, agents, speech |
| F11 | missing (known) | No attention job on the hub; the phone keeps the 0.4 AttentionStore and card taxonomy | `notification-android`, `docs/castling-phone.md:111` |
| F12 | missing | The house agent doesn't tag work with concepts or bootstrap new kinds of work | `agents/personas/house.md`, dream prompt |
| F13 | over-built | `github` is in the core package and lets only the huddle write | `rook/plugins/github/plugin.yaml` |
| F14 | over-built (minor) | Fixed channel order; jobs notify the person directly, not through the house agent | `notify`, `jobs.py:461-487` |
| F15 | over-built (plausible) | The desktop app ports 0.4's Work, Knowledge and Sessions panels as built-in views | `docs/castling-desktop.md:16-18`, `desktop/ui/panel.js` |
| F16 | bug found on the way | Archivist grants use plural namespaces, so it can't touch tasks, projects or concepts | `agents/plugin.yaml:210` |

---

### F1. Huddles rebuilt as a merge-vote room (PR #329)

**Decision**
- castling-workbench, "Sign-off replaces merge votes": "**Drop the
  merge-votes room** (chair, pins, unanimous huddle.vote). Code review alone
  doesn't generalise: chip design, 3D graphics, grocery lists (Bake)." Also:
  "Checks and balances live at prompt/persona level, not in machinery."
- castling-caps rule 7 (Bake accepted all calls, Oct 30): "Votes, tables and
  pins become sign-off."
- castling-workbench: "Huddles = streams." castling-streams: "Huddle: a chat
  stream plus `status` and question events; huddle logic is a plugin on top."

**What was built**
- `rook/plugins/huddle`: a 726-line script plus `plugin.yaml`. It has three
  record types. `huddle` has a `chair`. `artifact` is a table of pins at
  exact SHAs, with supersede logic. `motion` is a 9-state machine: gating,
  asked, carrying, merged, done, gates_failed, declined, void, failed.
- Caps `start/post/read/pin/table/vote/close/carry`, all enforced in code:
  - only a person or the chair may pin;
  - only a person names a chair;
  - `self_review` must be 80+ characters;
  - five gates run (version, mergeable, ci, tests, secrets);
  - a `repos` setting maps a repo to a project's trust, "the stricter wins";
  - an admin `carry` cap runs behind a hold for sacred projects;
  - qa labelling and deploy go through change records.
- The house persona got a `chair` skill (`agents/skills/chair.md`) that
  teaches the 0.4 flow.
- `docs/castling-switchover.md` R14 row: "Huddles with a merge vote".
- The huddle has no question events, which were the one thing the streams
  decision gave it.

**Severity:** contradiction and over-built. This is the example Bake caught.
It is a faithful port of the 0.4 flow that castling-workbench retired.

**Smallest fix**
1. Redefine R14 as "an agent can land a PR after sign-off" (F2), not "a
   merge vote".
2. Cut `huddle` to a stream kind with post and read (or drop it in favour
   of F10's `stream.*`).
3. Delete `artifact`, `motion`, `pin`, `table`, `vote` and `carry`, the
   `chair` setting and the chair skill.
4. Move the five gates into the reviewer persona or skill as a checklist,
   which is prompt level as decided. CI status stays readable through
   `github.checks`.
5. Update `readiness.py` and the R14 and D5 rows.
6. Tell Bake that D5 contained R14.

### F2. Sign-off is built but inert, and agents can't review on GitHub

**Decision**
- castling-workbench: "**Human pattern = sign-off:** any task can ask for
  sign-off before it's done. Reviewers come from the concept's pool of
  experts; the project's trust level decides whether the user must be one of
  them ... **Software: sign-off happens as GitHub PR reviews by agents under
  their own identities.**"
- castling-agents: agents "wake when addressed (message, huddle mention,
  job, **handed-off task**)".

**What was built**
- `rook/plugins/tasks` has `signoff` records and the caps `request_signoff`
  and `signoff`. They are good, but nothing uses them:
  - no persona or skill mentions sign-off (searching `rook/plugins/agents`
    for "signoff" or "sign-off" finds nothing);
  - nothing listens for `task.signoff_requested` or `task.handed_off`, so a
    named reviewer agent is never woken and a person reviewer is never told;
  - nobody calls `concept.pool`.
- `rook/plugins/github` has no review cap. It lets only the plugins in
  `writers: [huddle]` merge, label or comment, "with the hub's token, never
  an agent's". That is the opposite of "GitHub PR reviews by agents under
  their own identities".

**Severity:** missing, plus a contradiction in github. The decision's
replacement exists only as storage, while the retired flow is fully wired.

**Smallest fix**
1. `github.review` (approve or request changes, with a body) as the caller.
   Ideally use the agent's own token; until F5 lands, sign the review
   "agent:<name>" in the body.
2. Allow `github.merge` for any caller once the PR's task has every
   requested sign-off approved. "The person too" applies when the project's
   trust or `concept.signoff` says so.
3. Add one `signoff` skill to the agents package, covering both the asking
   side and the reviewing side.
4. An agents `on:` hook for `task.signoff_requested` and `task.handed_off`
   that wakes the named agent (`agents.chat.send`). A person goes through the
   house agent.

### F3. The "producer" became a release manager

**Decision:** castling-workbench, "Bootstrapping a new kind of work: the
producer". "**Producer** agent (like assembling a crew for a new show): the
first time a new kind of activity becomes real work, it (1) asks the user how
they like to work, (2) researches how practitioners actually do it ...
(3) writes the concept: expert personas, skills, sign-off norms."

**What was built**
- `rook/plugins/agents/personas/producer.md`: "You get a release out the
  door, end to end: from 'merged' to 'running everywhere'". It covers
  checklists, staged rollouts and announcements.
- `plugin.yaml` summary: "Gets a release out the door".
- `docs/manual-0.5/agents.md:40`: "gets releases out the door".
- Cause: the wave 4 prompt listed "producer" among the roles without saying
  what it does.

**Severity:** renamed. Same name, a different role. The role Bake designed
doesn't exist.

**Smallest fix:** rewrite `producer.md` and its summary to the decision:
interview the person, research practitioners, then `concept.create` with
roles, skills and sign-off norms. Release work is a skill for the lead or QA
if it's wanted at all. Change one line in the manual.

### F4. No Codex backend (and no local or API backend)

**Decision:** castling-inference: "**Day one: support Claude Code and
Codex** as backends." Defaults per backend: "ChatGPT/Codex: e.g. Sol for
System Two, Luna for System One and maintenance."

**What was built**
- Only `rook/plugins/agents-claude-code`.
- `agents/plugin.yaml` has `uses: ["agents-codex.*", "agents-local.*",
  "agents-api.*"]` and an enum `backend: [default, claude-code, codex,
  local, api]`. None of those plugins exist, so choosing `codex` fails at
  call time.
- `llm-cli` defaults for codex are "the CLI's default", not Sol and Luna.
- Cause: `castling-wave4/prompt-agents.txt` asked for "the backend contract
  implementations for Claude Code CLI (and a fake backend for tests)".

**Severity:** missing, plus an enum that offers what isn't there.

**Smallest fix**
1. `agents-codex`: `codex exec --json`, resumed by session id, mirroring
   `agents-claude-code/scripts/claude.py`, about 150 lines.
2. Set the Sol and Luna defaults in `llm-cli`.
3. Take `local` and `api` out of the enum until they exist.

### F5. Rook-run agents don't act as their own identity

**Decision**
- castling-agents: "**Every agent:** ... caps for every action, gated by
  permissions."
- The agents prompt: "Agents act with their own identity; acting for a
  person = intersection."
- castling-workbench F2: reviews "under their own identities".

**What was built**
- `agents.py:26-29`: "TODO(castling:identity): run turns as Caller(person,
  agent:<name>) once the hub registers agent identities from here".
- Hub identities come only from `hub.yaml agents:` (`runtime.py:399-400`).
  An agent made by `agents.create`, including the eight personas seeded at
  install, is not an identity.
- The Claude Code backend reaches Rook through one static `mcp_config` file
  (an `agents-claude-code` setting). Every Rook-run agent's tool calls
  therefore carry that file's token. The `grants` stored on each persona are
  not what dispatch checks.
- The result is two separate "agent" concepts: `hub.yaml` agents, which the
  MCP door and OAuth #332 use, and agents-plugin records. castling-agents
  asked for one mechanism.

**Severity:** missing. This is security-relevant: persona grants such as the
archivist's are advisory today.

**Smallest fix**
1. On seed and `agents.create`, call `Identities.add_agent(name, {grants})`.
2. Per turn, mint a short-lived MCP door token for `(person, agent:<name>)`
   and hand it to the backend in the env or a per-turn `mcp_config`.

### F6. The 0.4 migration ships as product code

**Decision:** castling-design, Scope: "**Migration from 0.4 is not a product
feature** (Bake, Oct 30): only Bake and one other user; a one-off move
(export knowledge, projects/tasks, memory, vault as plain files; an agent
imports and reshapes them into 0.5 ...). General migration tooling waits for
a larger user base."

**What was built**
- `rook/core2/migrate`: 886 lines in the core package, with dry-run and
  `--apply`, plus `docs/castling-migrate.md` (#281).
- `rook/plugins/knowledge/scripts/import_04.py`: 401 lines, shipped inside
  the workbench's knowledge plugin to every install (#277).
- A user manual page, `docs/manual-0.5/migrating.md`.
- Cause: `castling-wave4/prompt-workbench.txt` said "Include a migration
  script that imports today's 0.4 knowledge/tasks/projects".

**Severity:** over-built. About 1.3k lines of general tooling, shipped and
documented as a feature.

**Smallest fix**
1. Move both tools under `castling/switchover/`, which is repo tooling and
   not shipped, beside `dryrun.py`.
2. Take `import_04.py` out of the knowledge plugin.
3. Replace `migrating.md` with a one-line pointer, or drop it.
4. Keep using them for the switchover, since they work.

### F7. Skills aren't in the knowledge store

**Decision:** castling-agents: "**Skill library** lives in the same store
and search as knowledge, but as its own kind, not knowledge articles."
castling-workbench: "Keep: pages, kinds (incl. skills as their own kind)".

**What was built**
- `agents.skill` and the system prompt read only `skills:` files from the
  agents plugin's own `plugin.yaml` (`agents.py:416-433, 643-648`).
- Knowledge does have `kind: skill`, and `concept.skills` is documented as
  "Skill page slugs (knowledge kind skill)". An agent still can't load one,
  so a concept's pool of skills is unreachable.
- The gap is acknowledged at `agents.py:32`
  (`TODO(castling:workbench)`).

**Severity:** missing. There are two skill stores, and the one the concept
pool points at is dead.

**Smallest fix**
1. `agents.skill` falls back to `knowledge.get` when the page has
   kind=skill.
2. The prompt's skill list takes the pool's skills when the task's project
   has concepts.
3. Optionally, seed manifest skills as knowledge pages on install, so there
   is one store.

### F8. Personas from any plugin: missing; workbench roles in the wrong package

**Decision:** castling-dashboard, Package boundaries: "**Agent personas are
a plugin-shippable component.** Any plugin's manifest can ship personas
(alongside caps, skills, pages). The agents package provides the capability
to run them; **the Workbench's team roles (PM, reviewers, producer,
archivist) are personas it ships**, active when agents are present."

**What was built**
- `rt.seed(manifest())` seeds only the agents plugin's own `plugin.yaml`
  (`agents.py:158-175, 401-410, 464`). The manifest validator accepts
  `personas:` in any plugin (`rook/kit/manifest/validate.py:575`), but those
  personas never become agents.
- pm, producer and archivist ship in the agents package, along with
  `ticket-sweep` and `wiki-sweep`.
- `docs/manual-0.5/agents.md:75-76` says "plugins ship personas and skills
  in their manifest". That's untrue at runtime.

**Severity:** missing, plus package drift.

**Smallest fix**
1. Seed from every installed manifest (the hub's `hub.manifests`). Persona
   prompts resolve against the shipping plugin's folder.
2. Move pm, archivist, producer and their skills into a workbench plugin's
   manifest (tasks or knowledge).

### F9. C4 `approval.ask` and `page.show` aren't built; asks were reinvented

**Decision** (castling-caps, accepted by Bake Oct 30)
- C4: "a core `approval.ask` any agent (including external MCP agents) can
  use, routed through the house agent when installed."
- Rule 10: "show.present becomes core `page.show`."

**What was built**
- The builtin `approval` plugin has only `list` and `decide`.
- Each plugin built its own way to ask:
  - jobs: `jobs.answer` and a direct `notify.send` (`jobs.py:605`);
  - huddles: a `carry` cap behind a hold;
  - the phone: `notification.post` cards with `kind: ask`.
- External MCP agents have no ask at all.
- `page.show` doesn't exist.

**Severity:** missing, reinvented per plugin.

**Smallest fix**
1. Add `approval.ask {question, options?, why}` to the builtin approval
   plugin. Route it to the house agent when installed, otherwise to
   `notify.send`. Answers come back like holds.
2. Have jobs' ask call it.
3. Add `pages.show {page, worker?}`, which opens a page on the person's
   surface.

### F10. Streams: one API in name only; no chat rooms

**Decision:** castling-streams: "**One API:** `stream.open`, `read` (by
cursor, long-poll), `write`, `attach`, `list`, `close`." It names chat rooms
as streams: "Chat room: a stream of `text` events from many participants."
"Wake: attaching an agent producer to a stream."

**What was built**
- `stream.read/write/list/close` exist only on workers
  (`rook/core2/builtin/stream.yaml`), for terminals and processes.
- Durable hub streams each have their own verbs:
  - `huddle.read/post`;
  - `agents.chat.read/send`;
  - `speech.read/hear`.
- The MCP door's `stream` tool is worker-only (`doors/mcp.py:195-197`,
  `TODO(castling:streams)`).
- There's no `attach`.
- No chat-room plugin exists on 0.5. The switchover moves the 37 rooms to
  "Nothing".
- #331 adds a hub index (`stream.list/get/search`), but still no read or
  write.

**Severity:** renamed. The huddle got about 1k lines of new machinery while
the plain chat room it was meant to reduce to doesn't exist.

**Smallest fix**
1. Add hub `stream.read/write` over hub-plugin streams, with the same
   rights rule as #331's index.
2. Make `huddle.post/read` and `agents.chat.read` deprecated aliases.
3. A room becomes `stream.open kind=room`.

### F11. No attention job on the hub; the phone keeps the 0.4 attention store

**Decision:** castling-attention: "**Thin — no heavy logic in the app**
(e.g. no attention service)." "Attention becomes an **aggregation job on the
hub** run by the house agent: periodically collect everything trying to get
the user's attention ... and curate it in one place."

**What was built**
- Nothing on the hub consumes `notification-android.received` or
  `sms.received`. No job template or skill exists; the house persona has one
  line about it.
- The 0.5 `notification-android` plugin posts through the Kotlin
  `AttentionStore` and `AttentionChannels`, using the 0.4 taxonomy: kinds
  `card|hold|job|ask|alert`, channels `urgent|family|approvals|digest|jobs`.
- `docs/castling-phone.md:111` points at "the hub's attention job", which
  doesn't exist.
- This is known: attention is on the D5 "not ported" list.

**Severity:** missing (known). The carried-over taxonomy is minor.

**Smallest fix:** no new code. One jobs template `attention`, with
`on: notification-android.received, sms.received` (batched) and an agent
job to house with an `attention` skill. It goes in the agents manifest
`jobs:` section. Keep AttentionStore as a plain card store.

### F12. The house agent doesn't tag work or bootstrap new kinds of work

**Decision:** castling-workbench:
- "**The house agent asks 'what are we working on?'** to tag new work with
  concepts."
- "the house agent's persona says noticing when something has become real
  work and offering to set it up is part of its job".
- "the idle/dream pass ... notices recurring topics and leaves a note
  ('crochet, third time this month, no project yet')".

**What was built:** none of it. `house.md` doesn't mention concepts or
projects, and `DREAM_SYSTEM` in `agents.py` only curates memory.

**Severity:** missing. These are prompt-level by design, so they cost a few
lines.

**Smallest fix:** add three bullets to `house.md` (tag with concepts, offer
a project when work becomes ongoing, engage the producer when the concept is
new) and one instruction to the dream prompt (note recurring topics with no
project).

### F13. `github` is in the core package and writes only for the huddle

**Decision**
- castling-dashboard: "**Core:** workers, settings, approvals (holds on
  sacred calls), journal." "Ship three boilerplates: core, agents,
  workbench."
- castling-design: "The platform stays boring; clever parts are plugins."

**What was built:** `rook/plugins/github/plugin.yaml` has `package: core`
and a `writers: [huddle]` allow-list in code. It looks to be the only
integration in core whose purpose is the workbench's software flow.

**Severity:** over-built (package).

**Smallest fix:** `package: workbench`, or optional. Replace the `writers`
list with the sign-off rule from F2.

### F14. Reaching the person: fixed channel order; jobs bypass the house agent

**Decision:** castling-agents: "**Channels are changeable, not fixed
rules.** The house agent picks channel, device and timing from context."
"Other agents (PM etc.) ask the house agent to get word to the stakeholder."

**What was built**
- `notify` has a hub `channels` order and a per-person `reach` order.
- Jobs call `notify.send` directly on failure and on ask
  (`jobs.py:461-487, 605`), even when the agents package is installed.
- Rook-run agents are told to go through the house agent (the REACH
  directive). That part is right.

**Severity:** over-built (minor). The order is a sensible fallback for a
core-only install.

**Smallest fix:** when agents are installed, jobs' notify and ask go to the
house agent (`agents.chat.send to house`), the same path F9's
`approval.ask` uses. Keep the order only as the core-only fallback.

### F15. Desktop app: 0.4 panels ported as built-in views (plausible)

**Decision**
- castling-attention: "No built-in views needed for things like knowledge
  or tasks: the agent opens the relevant page/link when the user needs it."
  The page says this about the phone; the principle is "answer the question,
  don't build the spreadsheet".
- castling-dashboard: "All four combinations must work cleanly ... Tasks
  are not core."

**What was built:** `docs/castling-desktop.md:16-18` and
`desktop/ui/panel.js` keep Work (`task.list`), Knowledge
(`knowledge.search`, `task.search`) and Sessions (`agents.list`) tabs that
call workbench and agents caps directly. I saw no hiding when those packages
are absent.

**Severity:** over-built, a carry-over. Plausible, not run.

**Smallest fix:** the panel shows holds plus the hub's `pages.list`, which
it already fetches for the tray. Drop the Work and Knowledge tabs, or show
them only when `task.*` / `knowledge.*` caps exist.

### F16. Archivist grants use the wrong namespaces (bug)

`agents/plugin.yaml:210` gives the archivist `"concepts.*", "projects.*",
"tasks.*"`. The plugins' namespaces are singular (`plugin: concept`,
`project`, `task`), so those grants match nothing. The archivist can't read
the projects and tasks it's meant to cross-check. Once F5 lands, this
becomes an actual refusal.

**Fix:** use `concept.*`, `project.*`, `task.*`.

---

## Decisions checked and honoured

**Agents**
- **No special house agent:** one mechanism. The only code difference is
  that house skips the REACH line (`agents.py:329`); the rest is persona and
  grants. The dock agent is a setting.
- **Always-on per agent:** warm and sleep times, and a periodic dream pass
  for always-on agents.
- **Notepad and memory:** a private notepad and read-only memory. The dream
  cycle (System Three, default `haiku`) runs on idle.
- **Working together:** a roster stub and the consult directive in every
  prompt. "Ask the house agent to reach a person" is in every non-house
  prompt.
- **Presence** is a skill, not an engine (`skills/presence.md`), and
  respects "turn that off".
- **Lean toward agency:** `grants: ["*"]` defaults.

**Voice and inference**
- Voice is a motor skill: System One only in voice sessions, handing off to
  the agent's System Two. Speaker ID is advisory. Speech is a stream. B10's
  `speech` name is used.
- Inference: Claude defaults are Haiku, Opus and Haiku (`llm-cli`,
  `agents-claude-code`). There's no llmanifold requirement.

**Workbench**
- **Whiteboards:** per task and per project, volatile, with erase. The
  journal keeps history, and System Three doesn't manage them.
- **Knowledge:** provenance sources replace verification and claims, and
  the 0.4 import drops verification. Kinds include skill. Links, backlinks
  and search are in.
- **Concepts** are tags, not parents. `concept.pool` merges roles and skills
  and takes the strictest sign-off and trust.
- **Projects** are the parents of tasks. A project's trust is its own
  admin-only record.
- **Package boundaries:** tasks, projects, knowledge, concepts and
  whiteboards are in the `workbench` package, not core, and work without
  agents.
- **Handoffs** are ticket-style (`task.handoff` plus the whiteboard).

**Dashboard**
- Page lifecycle as caps: create, update, archive, restore.
- An archive button in the boilerplate, and a note beside each agent-made
  page.
- A page build is a task when the workbench is installed.
- Canned components plus sandboxed agent pages, the house agent dock and the
  start page.
- The core page set is workers, settings, approvals and journal.

**Jobs**
- Triggers: cron, every, on, manual and webhook.
- A job runs a cap, a script with the workflow library, or an agent, as its
  creator or a persona.
- Retries and catch-up, templates from manifests, and no checkpointing,
  as decided.

**Platform calls**
- The MCP door has a small generic tool set.
- C3: decide is kept, including `drive`.
- C5: persona push is dropped.
- C6: `claudecode` keeps install and status plus in-hook rules.

**Switchover (D1-D9, H2):** the open PRs follow the accepted plan: #327
worker keys (H2), #326 releases (R6), #328 desktop update (R10) and #332
OAuth (R13). #332's own "one configured agent per connector" choice inherits
F5's two-agents split.

## Suggested order

1. **F1 and F2 together:** a sign-off path for landing PRs, then retire the
   huddle machinery. Change the wave-prompt boilerplate at the same time,
   because the build process is what keeps the old flow alive.
2. **F5:** agent identities.
3. **F3, F12, F16:** prompt and manifest edits only.
4. **F4:** agents-codex.
5. **F6:** move the migration tools out of the product.
6. **F7 and F8:** one skill store; personas from any manifest.
7. **F9 and F10:** `approval.ask`, `pages.show`, hub `stream.read/write`.
8. **F11, F13-F15** as they come up.
