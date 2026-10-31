# Castling design-drift audit

Claude Code, Oct 31 2026 (wave 7, branch `c7/audit`). Report only: nothing here changes code.

Asked by Bake, Oct 31 12:07-12:08: "I thought we decided against merge votes" ... "Check all the work
going in so far for similar drift like this".

**Read against:** `origin/castling` at 579d323a (PR #329 merged; #326 merged since, read from its
diff), the open PRs into castling (#327 worker keys, #328 desktop update, #331 stream index, #332
OAuth), the unmerged branch `c7/transport2`, and every wave prompt in
`~/rook-backups/castling-wave1..7/`. Folds in the two helper passes `castling/drift-A.md` (core and
security, branch c7/audit-A) and `castling/drift-B.md` (product layers, branch c7/audit-B):
duplicates merged, strongest evidence kept, every finding re-checked against the code.

**Sources of truth, in order:** Bake's own words on the castling-* knowledge pages, then the pages'
decisions. The strongest single source turned out to be **castling-caps "Hard calls: decided by
Bake (Oct 30): All six recommendations accepted"** (C1-C6) together with the per-cap table in
`~/rook-backups/castling-caps/decisions.json` (keep / reshape / bin per 0.4 cap). Several wave-7
lanes rebuilt caps that table had binned.

**Severity:** *contradiction* (does what a decision rejected), *over-built* (machinery the decision
didn't ask for; Bake: "weigh complexity against value every time; cut what doesn't earn its
place"), *missing* (a decision's piece is absent and something else stands in, or a gate depends on
it), *renamed* (same name, different thing, or the decided thing under another name).

## How the drift got in

The merge-vote case and most findings below share one path:

1. **The switchover plan framed gates as "replace the 0.4 X".** `docs/castling-switchover.md` (wave
   6, #307) lists 0.4 pieces to replace ("CR runner", huddle votes, OTA, the OAuth shim) and
   writes gates against them: R4 "Without them nothing replaces the 0.4 CR runner", R14 "Huddles
   with a merge vote ... Agents land PRs today through the 0.4 huddle's `huddle.vote`".
   `castling/switchover/readiness.py:46-47` checks literally for the cap `huddle:vote`, so R14
   could only pass by building merge votes.
2. **Bake accepted the plan wholesale** (Oct 31 10:17, "Yes use your recommendations",
   castling-switchover-decisions). D5 held cutover for "R1-R5, R10 and R14"; nobody showed him
   that R4 and R14 reversed castling-caps C2 and castling-workbench.
3. **Wave-7 prompts put the plan above the design pages.** Each gate prompt opens "Read
   docs/castling-switchover.md first ... Bake accepted its recommendations", then says "port the
   0.4 X": changes.txt ("read ... the 0.4 change.* caps on master ... Port the 0.4 deploy flow's
   shape"), release.txt ("Today 0.4 has OTA ... Build the 0.5 equivalent"), huddles.txt
   ("sign-off replaces merge votes, but agents need this flow ... Keep the cap names agents use
   today"), oauth.txt, workerkeys.txt.
4. **The required reading was too narrow from wave 1.** `common.txt` makes castling-design,
   castling-manifest and castling-plugins required, plus "the pages in your scope".
   castling-workbench (sign-off, producer, archivist) and castling-caps (C1-C6, the binned caps)
   were never required; the wave-4 agents prompt summarised the roles as bare names
   ("archivist, producer") without pointing at the page that defines them.
5. **The build process itself runs the 0.4 flow.** Every prompt's LANDING section (pin via the
   house agent, `huddle.vote` call kind merge, `self_review` 80+ chars) is the live 0.4 production
   process and is not drift by itself. But all 73 merges on castling went through it, and every
   merge commit reads "no vote, ship-it" (no ballot ever happened), and the 0.5 huddle copied it to the
   letter (chair pins, 80-character `self_review`, `call kind=merge`). The same goes for the live
   0.4 MCP server instructions every agent has in context ("standard ones the hub checks, runs,
   tests and rolls back by itself; risky ... one card"): the changes lane rebuilt them almost
   word for word.
6. **The hardening review didn't look for cuts.** `castling/review.md` §Complexity: "Nothing found
   that asks for a redesign or a cut", with the migration tool and tiered retention already in.

**Process fix (no code):** in the wave prompts, make castling-workbench and castling-caps required
reading; mark the LANDING section "0.4 production process only, do not port to 0.5"; in any gate
that names a 0.4 piece, say which castling decision replaces it; and before Bake accepts a plan
"wholesale", list every place it departs from a decision page.

## Summary

| # | Severity | Finding | Where |
|---|---|---|---|
| 0 | contradiction, over-built | Huddles rebuilt as a merge-vote room (known; rework in flight) | #329 `rook/plugins/huddle`, `github`, `agents/skills/chair.md` |
| 1 | contradiction, over-built | Change records rebuilt as the 0.4 CR runner | #322 `rook/plugins/changes` |
| 2 | contradiction | Core refuses plugin pushes and worker updates without the change plugin | `rook/core2/hub/runtime.py:1354`, #326 |
| 3 | missing | Sign-off (the decided replacement) is storage only; agents can't review PRs | `rook/plugins/tasks`, `rook/plugins/github` |
| 4 | missing | Rook-run agents have no identity of their own; persona grants are decorative. **Fixed: PR #344** | `agents/scripts/agents.py:26`, `agents-claude-code` |
| 5 | renamed, missing | "Producer" became a release manager; the house agent doesn't tag work or offer projects (**fixed, PR #340**) | `agents/personas/producer.md`, `house.md` |
| 6 | contradiction | Archivist and wiki-sweep teach 0.4 verification and supersede (**fixed, PR #340**) | `agents/personas/archivist.md`, `skills/wiki-sweep.md` |
| 7 | over-built | The one-off 0.4 migration ships as a product tool in core (**fixed, PR #339**) | `rook/core2/migrate`, `knowledge/scripts/import_04.py` |
| 8 | missing | C4 `approval.ask` not built; plugins built their own asks; `page.show` absent (**fixed, PR #350**: approval.ask; `page.show` open) | builtin `approval`, `jobs`, `huddle carry`, `changes` |
| 9 | contradiction (partial) | Streams: three private hub stores, no hub `stream.read/write`, no chat rooms (**fixed, PR #347**) | `agents`, `speech`, `huddle`, #331 |
| 10 | missing, package | Personas only from the agents manifest; workbench roles in agents; skills not in the knowledge store (**fixed, PR #340**) | `agents.py:158,416,464` |
| 11 | missing | No Codex backend, while the enum and manuals offer codex/local/api. **Fixed: PR #336** | `rook/plugins/agents*` |
| 12 | contradiction, over-built | Core "2.1" is breaking but labelled minor; extras beyond H2 | #327 |
| 13 | over-built | Worker updates port 0.4 OTA as caps, including caps Bake's pass binned | #326 |
| 14 | over-built | OAuth door is a full authorization server; 0.4 cut that back to a shim | #332 (**fixed in #337**) |
| 15 | missing | Backup has no default target and no house-agent nudges (**fixed, PR #343**) | `rook/core2/hub/backup` |
| 16 | missing | `core.elevate` skips the gate hook (**fixed, PR #338**) | `rook/core2/hub/dispatch.py:550` |
| 17 | contradiction | The security gate ships in enforce mode with heuristic holds (**fixed, PR #338**) | `rook/plugins/security/plugin.yaml:26` |
| 18 | missing (bug) | The pinned-task check calls a cap that doesn't exist, fails quietly (**fixed, PR #342**) | `security/plugin.yaml:20` |
| 19 | missing | Telegram/Discord messages reach no agent after cutover. **Fixed: PR #341** | `telegram`, `discord`, switchover step 3 |
| 20 | missing | No agent-led install path; README and skill are still 0.4. **Fixed: PR #345** | `README.md`, `skills/rook/` |
| 21 | missing | Appliance routing and appliance settings on the worker | `dispatch.py`, `worker/host.py` (N3) |
| 22 | missing | Worker secret settings have no vault path | `worker/host.py:395` |
| 23 | contradiction (source) | Switchover "not ported" list and R2 use retired 0.4 names (huddles, Feed, `show.present`, N23) (**fixed, PR #339**) | `docs/castling-switchover.md` |
| 24 | over-built (low) | 96 0.4 cap aliases in 27 manifests (**fixed, PR #349**) | `rook/plugins/*/plugin.yaml` |
| 25 | over-built (low) | Child preset `approve_new` queue for a deferred feature (**fixed, PR #351**) | `rights.py`, `identity.py`, spec 8.5 |
| 26 | over-built (low) | Journal retention tiered by risk (**fixed, PR #338**) | `rook/core2/journal.py:26` |
| 27 | over-built (low) | `github` in the core package with a huddle-only writer list | `rook/plugins/github/plugin.yaml` |
| 28 | over-built (low) | Jobs notify the person directly with a fixed channel order | `jobs.py:461-487,605`, `notify` |
| 29 | over-built (low) | Desktop app ports 0.4 Work/Knowledge/Sessions panels | `desktop/src-tauri/src/hub05.rs:108` |
| 30 | missing (known) | No attention job on the hub; phone keeps the 0.4 attention store | `notification-android`, `docs/castling-phone.md:111` |
| 31 | over-built | Hub plugins rebuild storage instead of declaring records | `agents.py:110`, `telegram`/`discord` `links.json`, hub pages |
| 32 | contradiction (mild) | Core service plugins call providers by plugin name, not contract. **agents fixed: PR #344**; notify, speech: follow-up | `notify`, `speech`, `agents` |
| 33 | renamed (low) | Plural namespaces `jobs.*`, `agents.*`, `pages.*`; `jobs.start` not `job.run` | `jobs`, `agents`, hub pages |
| 34 | over-built (low) | Manifest 1.1/1.2 features nothing uses; jobs rolls its own webhook HMAC (**partly fixed, PR #349**: format frozen, unused keys reserved; code and the jobs webhook left) | `rook/kit/manifest`, `hub/plugins.py`, `jobs.py:870` |

Watch items (ask Bake, not drift yet) are after the findings.

---

## 0. Huddles rebuilt as a merge-vote room (known; for the record)

- **Decision.** castling-workbench: "**Drop the merge-votes room** (chair, pins, unanimous
  huddle.vote). Code review alone doesn't generalise: chip design, 3D graphics, grocery lists
  (Bake)." "Checks and balances live at prompt/persona level, not in machinery." "Huddles =
  streams." castling-caps rule 7 (Bake accepted): "Votes, tables and pins become sign-off." The
  cap table binned `huddle.table` ("Merge votes dropped"), `huddle.pin` (-> task.link),
  `huddle.vote` (-> signoff.request/give), `huddle.post/read` (-> stream.write/read) and kept only
  `huddle.start`.
- **Built instead.** #329: `rook/plugins/huddle` (726-line script; `huddle`, `artifact`, 9-state
  `motion` records; pin/table/vote/carry; five gates; chair), `rook/plugins/github`
  (`writers: [huddle]`), `rook/plugins/agents/skills/chair.md`, readiness R14 `huddle:vote`.
  The huddle has no question events, the one thing castling-streams gave it.
- **Status.** Rework in flight: task "R14 reworked: sign-off + github.merge replaces merge votes;
  huddles are plain streams" (branch c7/signoff). **Caution for that rework:** its task text keeps
  "the gates (head pinned, mergeable, CI, tests, secrets)" as hub machinery. Per castling-workbench
  those checks belong in the reviewer's persona/skill; GitHub branch protection already enforces
  CI. Keep at most the head-SHA check inside `github.merge`.
- **Also fix:** `readiness.py` R14, the switchover R14/D5 rows, and the chair skill. Tell Bake that
  D5 contained R14 when he accepted it.

## 1. Change records rebuilt as the 0.4 CR runner (contradiction + over-built)

- **Decision.** castling-caps C2 (Bake accepted, Oct 30): "**Change requests** (4): records only,
  with apply, test and rollback as a job the record links." The cap table binned
  `change.revert` (-> "job.run (the record's rollback job)"), `change.catalog` ("Standard-change
  kinds replaced by trust levels"), `change.decide` (-> approval.decide), `change.reduce` ("No
  huddle round"). castling-changes (Bake's priority order): "1. A preparation ritual ... 2. Audit
  and context ... 3. **Gating, the least important.** If things work well, gating is rare." "sacred
  = full record + owner approval; normal = full record, proceeds on its own". "Deploy-specific
  extras (diff, test run, rollback) come from the deploy cap, **not a separate system.**"
- **Built instead.** #322 `rook/plugins/changes` (649 lines): `change.submit` runs
  `checks -> scripts -> tests -> rollback` step lists itself through dispatch, auto-rolls back,
  waits up to `rejoin: 5m` for restarted workers, and keeps a 9-state machine
  (refused/rolled_back/failed/reverted ...). It brings back the binned `kind: standard|risky`
  axis: `changes.py:490` `asks = bool(fields["scripts"]) and (fields["kind"] == "risky" or level
  == "sacred")`, so an agent-chosen `risky` asks for a card in a normal project. To force that
  card, `change.apply` is declared `effects: [destructive]` (`plugin.yaml:99`), so every risky
  change is journaled as "erases data for good" and the spec 10.3 hard stop is bent into "needs a
  card". Each step then dispatches as the author, so a held step inside an approved change is a
  second card. The binned `change.revert` is back. `Runtime.online()` polls for rejoining workers
  (`changes.py:268`) and `recover()` re-asks and rolls back runs cut off by a hub restart
  (`changes.py:413-435`): a second runner beside jobs, with the durability castling-jobs refused
  ("No durability/checkpointing").
- **Source.** wave7 `changes.txt` / `prompt-changes.txt`; switchover R4; the 0.4 MCP server
  instructions.
- **Smallest fix.** `change` becomes a record: what, why, risk, testing, rollback, steps; ceremony
  from the project's trust level; a card only for sacred, through the existing sacred hold. Drop
  `kind`, `change.apply`, `change.revert`, the step runner and `rejoin`. Apply/test/rollback become
  a jobs-plugin job the record links (castling-jobs already designed "scripts with steps and
  branching"). Rewrite the R4 row.

## 2. Core refuses work without the change plugin (contradiction)

- **Decision.** castling-changes: what counts as a lasting change "is **guidance, not a hard
  rule.**" castling-design: "The platform stays boring; clever parts are plugins"; "Plugins talk
  to each other only through caps." castling-dashboard: core alone must work.
- **Built instead.** `rook/core2/hub/runtime.py:1354-1356` refuses every plugin push, even an
  owner's: "refused: plugin pushes go through change records; put the change plugin ... in the
  hub's library", then calls `change.submit` with a hard-coded scripts/rollback shape
  (`runtime.py:1389-1397`). #326 (merged) does the same for every worker update ("Without the
  change plugin a rollout is refused"; `hub/updates.py` calls `change.submit`). Core now depends
  on a plugin. The worker already installs atomically and rolls back on failed checks.
- **Source.** wave7 `changes.txt` ("Plugin pushes ... go through a change record"), `release.txt`.
- **Smallest fix.** Core pushes and updates on its own and journals the call (that *is* the audit
  trail); when the change plugin is present it writes a one-line record. Never refuse because a
  plugin is missing.

## 3. Sign-off is storage only, and agents can't review PRs (missing)

- **Decision.** castling-workbench: "**Human pattern = sign-off:** any task can ask for sign-off
  before it's done. Reviewers come from the concept's pool of experts; the project's trust level
  decides whether the user must be one of them ... **Software: sign-off happens as GitHub PR
  reviews by agents under their own identities.**"
- **Built instead.** `rook/plugins/tasks` has `signoff` records, `task.request_signoff` and
  `task.signoff`, and `concept.pool` exists. Nothing calls or teaches them: no persona or skill in
  `rook/plugins/agents` mentions sign-off or concepts, no hook wakes a named reviewer on
  `task.signoff_requested`/`task.handed_off`, and the only landing flow agents are taught is
  `skills/chair.md`. `rook/plugins/github` has no review cap and merges/labels/comments only for
  `writers: [huddle]` "with the hub's token" - the opposite of reviews under agents' own
  identities. `concept.signoff: [none, experts, user]` and `project.trust` both answer "must the
  person sign off".
- **Source.** wave4 `prompt-agents.txt` (didn't point at castling-workbench); wave7 huddles (#329).
- **Smallest fix.** A `github.review` cap as the caller; `github.merge` allowed to any caller once
  the PR's task has its sign-offs; one `signoff` skill (asking and reviewing) for PM, lead and QA;
  an agents `on:` hook that wakes the named reviewer. Let project trust alone decide whether the
  person signs (drop `concept.signoff=user`). This is what the R14 rework should land.

## 4. Rook-run agents have no identity of their own (missing, security-relevant). Fixed: PR #344

- **Decision.** castling-agents: "**Every agent:** ... caps for every action, gated by
  permissions"; tighten "by narrowing that role's grants or persona". castling-workbench: reviews
  "by agents under their own identities".
- **Built instead.** `rook/plugins/agents/scripts/agents.py:20-29` runs every turn "as whoever sent
  the message" with `TODO(castling:identity)`. Hub agent identities come only from static
  `hub.yaml agents:` (`runtime.py:399`, `identity.py:144`); the persona `grants:` in
  `agents/plugin.yaml` are never registered, so the eight seeded personas are not identities.
  `agents-claude-code` passes one `mcp_config` setting to every turn on a worker, so every
  Rook-run agent reaches the hub with the same token. `docs/manual-0.5/jobs.md:43` promises
  persona jobs run with "the agent's own, narrower grants". `castling:identity` is in neither
  coverage.md nor the switchover gates. #332 OAuth inherits the split (connectors pick a
  `hub.yaml` agent).
- **Smallest fix.** On seed and `agents.create`, `Identities.add_agent(name, grants)`; per turn,
  mint a short-lived door token for `(person, agent:<name>)` and pass a per-turn `--mcp-config`.
  Add it to the switchover gates (#3 depends on it).

## 5. The producer became a release manager; the house agent doesn't bootstrap work (renamed + missing)

- **Decision.** castling-workbench: "**Producer** agent (like assembling a crew for a new show):
  the first time a new kind of activity becomes real work, it (1) asks the user how they like to
  work, (2) researches how practitioners actually do it ... (3) writes the concept: expert
  personas, skills, sign-off norms." "**The house agent asks 'what are we working on?'** to tag
  new work with concepts." "the house agent's persona says noticing when something has become
  real work and offering to set it up is part of its job"; the dream pass "notices recurring
  topics and leaves a note". Bake liked the outcome: "hey, you might be into crochet now."
- **Built instead.** `rook/plugins/agents/personas/producer.md`: "You get a release out the door,
  end to end"; the manifest summary and `docs/manual-0.5/agents.md:40` say the same.
  `personas/house.md` never mentions concepts or projects; `DREAM_SYSTEM` only curates memory.
  No plugin ships a `concepts:` section, so the concept pool is empty.
- **Source.** wave4 `prompt-agents.txt` listed "producer" without saying what it does.
- **Smallest fix.** Rewrite `producer.md` to the decision (interview, research, `concept.create`
  with roles/skills/sign-off norms); three bullets in `house.md`; one line in the dream prompt.
  Release work, if wanted, is a skill for lead or QA.
- **Fixed** in PR #340 (c8/roles).

## 6. Archivist teaches the 0.4 verification model (contradiction)

- **Decision.** castling-workbench: "**Drop verification flags and claims.** Verification was a
  stopgap ... **Replace with provenance** ... When an agent finds a page wrong, it fixes the page
  and cites why, like a person editing a wiki."
- **Built instead.** `rook/plugins/agents/personas/archivist.md:11-12`: "Correct a fact with a new
  page that supersedes the old one"; "A claim without evidence is marked unverified, not
  deleted." `skills/wiki-sweep.md` says the same. The 0.5 knowledge plugin (rightly) has no
  verification or supersedes field, so the persona points at things that don't exist.
  Separately, the archivist's grants (`plugin.yaml:210`) are `concepts.*`, `projects.*`, `tasks.*`;
  the namespaces are singular, so once #4 lands they match nothing.
- **Smallest fix.** "Fix the page and add a `sources` entry saying why; an unsupported statement
  gets a source or comes out." Correct the three grant patterns.
- **Fixed** in PR #340 (c8/roles).

## 7. The one-off 0.4 migration ships as a product tool (over-built)

- **Decision.** castling-design (Bake, Oct 30): "**Migration from 0.4 is not a product feature**:
  only Bake and one other user; a one-off move (export knowledge, projects/tasks, memory, vault as
  plain files; **an agent imports and reshapes them** into 0.5 ...). Bake helps the other user
  directly. **General migration tooling waits for a larger user base.**"
- **Built instead.** `python -m rook.core2.migrate` (886 lines in the shipped core package:
  dry-run/`--apply`, idempotent re-runs, `--rename`, `--json`, 0.4 grants -> invented
  `member-<tier>` presets though the dry run found 0 member grants, trust-map conversion, vault
  re-encryption) + `tests/test_core2_migrate.py` (518) + `rook/plugins/knowledge/scripts/import_04.py`
  (401, shipped to every install) + a user manual page `docs/manual-0.5/migrating.md` +
  `docs/castling-migrate.md`. `docs/castling-switchover.md:227-230` then plans a re-run merge at
  cutover.
- **Source.** wave4 `workbench.txt` ("Include a migration script that imports today's 0.4
  knowledge/tasks/projects"); wave5 `migrate.txt` specifies the full tool and credits
  "castling-first-run (migration is a one-off move)" while the decision on castling-design says
  the opposite of building a tool.
- **Smallest fix.** It works, so use it once: move `migrate/` and `import_04.py` under
  `castling/switchover/` (repo tooling, not shipped), drop `manual-0.5/migrating.md`, no more
  merge/re-run work, delete after cutover.
- **Fixed:** PR #339 (c8/ops): moved to `castling/switchover/migrate` + `import_04.py`, manual page
  dropped, one migration at step 5, deleted at step 6.

## 8. C4 `approval.ask` was never built; plugins built their own asks (missing)

- **Decision.** castling-caps C4 (Bake accepted): "a core `approval.ask` any agent (including
  external MCP agents) can use, routed through the house agent when installed." Rule 10:
  "show.present becomes core `page.show`." castling-changes: "One approval path."
- **Built instead.** The builtin approval plugin has only `list` and `decide`. `approval.ask` is
  named only in coverage.md, cap-decisions.json and `rook/streams/README.md:69` (which already
  sends huddle questions to it). In its place: `change.apply` (`changes/plugin.yaml:95-99`) and
  `huddle.carry` (`huddle/plugin.yaml:201-205`) are self-calls tagged `effects: [destructive]`
  though neither erases anything (castling-manifest-caps: destructive = "Erases or overwrites data
  for good"), only so the hard stop makes an owner card; each then regex-parses `"(hold h_...)"`
  out of the error text to find its hold (`changes.py:63`, `huddle.py:67`). The journal records
  "destructive" for non-destructive actions. Jobs invents a third way (`jobs.answer` + direct
  `notify.send`, `jobs.py:605`), and the phone has `kind: ask` cards. External MCP agents can't ask at all except
  by getting a call held. No `page.show`. The switchover plan never mentions C4.
- **Smallest fix.** `approval.ask {question, options?, why}` in the approval builtin, same card path
  as holds, no call attached, routed to the house agent when present; jobs, huddle and changes
  use it. Add `page.show`. Add C4 to the switchover open list.
- **Fixed** (approval.ask, jobs) in PR #350 (c8/roles-ask). `change.apply` goes with the change-records fix (#1), `huddle.carry` with #333; `page.show` is not built yet.

## 9. Streams: one API in name only (contradiction, partial)

- **Decision.** castling-streams: "**One API:** `stream.open`, `read` (by cursor, long-poll),
  `write`, `attach`, `list`, `close`." "The hub keeps one index of all streams and one fan-out
  relay." "Chat room: a stream of `text` events from many participants." "Huddle: a chat stream
  plus `status` and question events; huddle logic is a plugin on top."
- **Built instead.** `stream.read/write/list/close` exist only on workers
  (`rook/core2/builtin/stream.yaml`). Each hub plugin opens a private store with its own verbs:
  agents `Streams(SqliteStore(sessions.db))` (`agents.py:117`, `agents.chat.read/send`), speech a
  second `sessions.db` (`speech.py:69`, `speech.read/hear`), huddle `huddles.db`
  (`huddle.py:124`, `huddle.read/post`). A voice turn is written twice. No `attach`. No chat-room
  plugin on 0.5 (and the switchover carries none of 0.4's 37 rooms over). Open #331 adds a hub
  `stream.list/get/search` index (metadata only); its builtin says to read a hub stream through
  "its plugin's caps", and huddles don't call `ctx.index_streams`, so they are missing from it.
- **Source.** #280, #287, #329 each built a store with a TODO; #331 inherited the layout.
- **Smallest fix.** In #331, index huddles. Follow-up: one hub `Streams` store handed to hub plugins
  through ctx, with hub `stream.read/write` serving it under #331's rights rule; plugin verbs
  become aliases; a room is `stream.open kind=room`.
- **Fixed: PR #347.** One durable hub `Streams` (`ctx.streams`) for agents, speech and huddle,
  reported into #331's index; hub `stream.open` (rooms), `stream.read` (long-poll = follow),
  `stream.write` (text in a room) and `stream.close` under the index's rights. Plugin-owned
  streams are read with `stream.read` and written through their plugin's verbs (they do more
  than append). No relay yet (`TODO(castling:relay)`).

## 10. Personas and skills: one plugin, one store (missing + package drift)

- **Decision.** castling-dashboard (package boundaries): "**Agent personas are a plugin-shippable
  component.** Any plugin's manifest can ship personas ... **the Workbench's team roles (PM,
  reviewers, producer, archivist) are personas it ships**"; "All four combinations must work
  cleanly." castling-agents: "**Skill library** lives in the same store and search as knowledge,
  but as its own kind."
- **Built instead.** `rt.seed(manifest())` seeds only the agents plugin's own manifest
  (`agents.py:158-175, 464`), although the validator accepts `personas:` anywhere and
  `docs/manual-0.5/agents.md:75` says plugins ship them. PM, producer, archivist and their skills
  ship in `package: agents`, so core + agents gets a PM sweeping a task deck that isn't installed.
  `agents.skill` reads only the agents manifest's `skills:` files (`agents.py:416-433, 643`);
  knowledge has `kind: skill` and `concept.skills` points at those pages, but no agent can load
  them (`TODO(castling:workbench)` at `agents.py:32`).
- **Smallest fix.** Seed personas from every installed manifest; move PM/archivist/producer and
  their skills into a workbench manifest; `agents.skill` falls back to knowledge pages of kind
  skill.
- **Fixed** in PR #340 (c8/roles).

## 11. No Codex backend, but the enum and manuals offer it (missing). Fixed: PR #336

- **Decision.** castling-inference: "**Day one: support Claude Code and Codex** as backends."
  castling-agents: "backend chosen when the agent is created (Claude Code, local model, API)".
- **Built instead.** Only `rook/plugins/agents-claude-code`. `agents/plugin.yaml:22,63` declares
  `uses: agents-codex.*, agents-local.*, agents-api.*` and `backend: [default, claude-code, codex,
  local, api]`; none of those plugins exist, so choosing codex fails at call time. The manuals
  describe Codex and local backends. Not tracked in coverage or the switchover.
- **Source.** wave4 `prompt-agents.txt`: "Claude Code CLI (and a fake backend for tests)".
- **Smallest fix.** `agents-codex` (`codex exec --json` resumed by session id, mirroring
  `claude.py`, ~150 lines); remove `local`/`api` from the enum and manuals until they exist.

## 12. Core "2.1" is a breaking change labelled minor (contradiction + over-built). PR #327, open

- **Decision.** castling-protocol (Bake): "anything non-breaking in v2 is a minor version (v2.1,
  v2.2, ...); every v2.x is compatible with the v2 spec"; "anything added in a minor version is
  optional with a sensible default when missing". castling-switchover-decisions H2: "workers get
  their own key and sign core.call / core.elevate requests". castling-multiband rejected "keys to
  manage" as "way off the rails" (the H2 key is invisible to users, so that part holds).
- **Built instead.** A 2.1 hub refuses `core.call`/`core.elevate` from a 2.0 worker ("denied:
  core.call needs core 2.1"), so `proof` is required: breaking, labelled minor. Two bridges exist
  only for that: trust-on-first-use key pinning from the first announce (spec 15) and the 2.0
  refusal path. Beyond H2: a signed `rook-reply` object on every core result and a 256 sub-call
  cap per call. The only 2.0 node anywhere is the cachyrig sidecar; 0.5 hasn't shipped.
- **Source.** wave7 `prompt-workerkeys.txt` asked for both halves ("core v2.1 (minor-compatible:
  ... a v2.0 worker gets a clear refusal").
- **Smallest fix.** Before merging: fold H2 into 2.0 (Castling is a clean cut and nothing outside
  this repo implements 2.0), keep enrollment-time key pinning, drop first-announce TOFU and the
  2.0 refusal, re-enroll the sidecar. Ask Bake about `rook-reply` and the sub-call cap separately.

## 13. Worker updates port the 0.4 OTA shape, including binned caps (over-built). PR #326, merged

- **Decision.** Bake's cap pass: `worker.update` "One cap for desktop and APK; absorbs check";
  `updates.prepare_hub` binned -> "`rook hub update` CLI"; rule 8 "Plumbing is not a cap: ...
  OTA begin/apply, commit-confirm". castling-design: "nothing survives just because it exists";
  D5 does not hold cutover on R6.
- **Built instead.** #326 (+2350 lines): a new `worker.rollback` cap, `update.prepare`, a
  canary-then-batch rollout orchestrator in core (`builtin/update.yaml`, `hub/updates.py`),
  trial boots with crash counting, and one change record per worker update through the #1 runner,
  undone with the binned `change.revert`.
- **Source.** wave7 `release.txt`: "Today 0.4 has OTA ... staged rollout ... Build the 0.5
  equivalent ... update.* caps on the hub (status, prepare, rollout, history)": the prompt named
  `prepare`, which Bake's pass binned.
- **Smallest fix.** Keep the signed release and `worker.update` with trial + rollback inside it
  (`worker.update {version}` covers going back). `prepare` becomes the CLI. One change record for
  the rollout, not per worker, and not through the runner (#1, #2).

## 14. The OAuth door is a full authorization server (over-built). PR #332, merged

**Fixed in #337:** #332 reverted; `rook/core2/hub/doors/mcp_oauth.py` is the 0.4 shim's shape on the MCP
door (agent token = client secret = access token, no sign-in, no DCR, no store).

- **Decision.** 0.4 history: the full OAuth server was deleted on 2026-07-31 for bearer tokens;
  2baf1a2 (Aug 10) re-added only `oauth_shim.py` for claude.ai web (PKCE `/authorize`, no
  registration, `client_secret` = a Rook bearer token, access token = that token, no token
  store). Castling: "Weigh complexity against value every time." Gate R13 itself is justified
  (claude.ai connectors only speak OAuth).
- **Built instead.** `rook/core2/hub/oauth.py` + routes: RFC 8414/9728 metadata, open RFC 7591
  dynamic client registration (500 clients, oldest evicted), a consent page with an agent
  picker, 1 h access + rotating refresh tokens with reuse detection, RFC 7009 revocation, RFC
  9207 `iss`, an `oauth.json` token store, a builtin `connector` plugin and a Connectors page.
- **Source.** wave7 `prompt-oauth.txt` asked for exactly this, citing the 0.4 shim but not why
  it is small.
- **Smallest fix.** Port the shim: `/authorize` (PKCE, hub sign-in, no consent screen), `/token`
  exchanging `client_secret` = an existing agent token (`token --person --agent` already
  intersects rights), access token = that token, revocation = token revocation. Add DCR only if
  claude.ai mobile proves to need it. Confidence medium: depends on what the mobile connector
  requires.

## 15. Backup: no default, no nudges (missing)

- **Decision.** castling-backup (Bake): "**Multiple backup paths, with a reasonable one by
  default**"; the proposed default is "nightly snapshots to another worker on the band (no setup,
  data stays on own machines)"; the house agent raises "backups aren't set up" / "did you save
  your recovery key?" (skill-driven).
- **Built instead.** `rook/core2/hub/backup/plugin.yaml:21` `target: ""`; `backup/__init__.py:636`
  raises "no backup target", so the shipped nightly job fails until an owner sets a target and
  installs `backup-store` on a worker by hand (switchover doc 222-226). No persona or skill
  mentions backups or the recovery key. R5 is ticked anyway.
- **Source.** wave7 `backup.txt` asked for a "configurable target", no default.
- **Smallest fix.** Blank target = another online worker of the owner's running `backup-store`
  (in the default worker install); a short house-agent skill for the two nudges.
- **Fixed:** PR #343 (c8/ops-backup): blank target = an owner's online worker with `backup-store`
  (not the hub's machine); house skill `housekeeping`. No default worker install: the house agent offers it.

## 16. `core.elevate` skips the gate hook (missing)

- **Decision.** castling-security: the gate hook sits at dispatch; classifier tiers "reads log,
  writes/exec can hold, **elevation must pass**"; "elevation must be rare and journaled loudly".
- **Built instead.** `Dispatcher.elevate` (`rook/core2/hub/dispatch.py:550-590`) checks rights,
  ceiling and session only, never `gates_mod.run`. An `exec` call that hits access-denied gets a
  15-minute admin lease without a gate verdict, skipping e.g. the security plugin's "admin far
  from the pinned task" flag. Spec 7.2 step 3 omits the gate too.
- **Smallest fix.** Run the gates on `core.elevate` with risk admin and the parent call's
  args/why/session; a flag is a hold or deny. One line in spec 7.2.
- **Fixed: PR #338.** `core.elevate` runs the gates as admin (enforced flag or block refuses: no
  hold mid-call); not re-run for a call already gated as admin at dispatch; spec 7.2 + 10.2.

## 17. The security gate ships in enforce mode with heuristic holds (contradiction)

- **Decision.** Spec 10.2, written from castling-security: "Gates ship in shadow mode first."
  castling-security: "Alarm fatigue is one of the biggest real threats ... only reaches the human
  for rare, high-confidence events"; canaries are the "near-zero false positives - build first"
  piece.
- **Built instead.** `rook/plugins/security/plugin.yaml:26` `gate.mode: enforce`. Besides canaries,
  word-overlap and regex heuristics (taint + risky + no overlap with the pinned task; taint +
  egress-looking regex; admin/persistence with no overlap) become holds, i.e. cards, with
  unmeasured false-positive rates. (security-ml correctly ships in shadow.)
- **Smallest fix.** Canary blocks enforced; taint/intent checks journal and allow until a
  `heuristics: shadow|enforce` setting (default shadow) is flipped after clean nights.
- **Fixed: PR #338.** Exactly that: `heuristics` setting, default `shadow`, `security.status`
  counts `would_hold`; canaries still block.

## 18. The pinned-task check is dead (missing, bug)

- **Decision.** castling-security: "pinned task recorded at session start, checked by the gate".
- **Built instead.** `rook/plugins/security/plugin.yaml:15,20` uses and defaults `task_cap` to
  `task.task.get`; the records kit names a plugin-named record's caps `task.get`
  (`rook/kit/records/store.py:78-81`), and nothing provides `task.task.get`. `security.py:325`
  catches the failure at info level and stores an empty word set, so `intent_mismatch` and the
  triage agent's `matches_task` evidence never apply on a real hub. The test fakes `task.get`.
- **Smallest fix.** `task.get` in `uses` and the default; log at warning; one test against the real
  tasks manifest. (Fixing it makes #17's heuristics fire for real, so do #17 first.)
- **Fixed** in PR #342 (c8/roles-pin).

## 19. Telegram/Discord messages reach no agent after cutover (missing) Fixed: PR #341

- **Decision.** castling-streams: "Worker chat and `msg`: a stream with the local human as a
  participant, or a person's inbox stream." "Message into a running agent: an `input` event."
  castling-design lists Telegram/Discord under Rook Assistant.
- **Built instead.** `rook/plugins/telegram` and `discord` emit `telegram.message` /
  `discord.message` events; nothing in `rook/plugins` subscribes to them by name (grep), agents
  hooks only `start`, `stop`, `every`, and jobs' catch-all `on:` fires only for a job someone made
  with that trigger - no ready-made one ships. `docs/castling-switchover.md` step 3 moves both bots to 0.5, and
  today Bake reaches Sojourn through Discord: after cutover a message to the bot reaches no
  agent. Not on the D5 not-ported list.
- **Smallest fix.** An agents `on: telegram.message|discord.message` hook that appends an `input`
  to the linked person's house-agent session and replies through `notify.send`; or list it under
  D5.

## 20. No agent-led install path (missing) Fixed: PR #345

- **Decision.** castling-first-run: "README leads with: **the easiest way to get started with Rook
  is to ask whatever agent you're already using to set it up** (point it at the GitHub repo)."
  "The repo carries **instructions for agents** (a setup skill / agent section of the README)."
- **Built instead.** `README.md` on castling is the 0.4 README; `skills/rook/` is the 0.4 skill.
  D1 makes `castling` the new `master`, so this is what new users land on.
- **Smallest fix.** Before D1: a short 0.5 README opening with the "ask your agent" line, and
  `skills/rook/setup/SKILL.md` with the install conversation from castling-first-run and
  castling-inference.

## 21. Appliance routing (missing, tracked)

- **Decision.** castling-workers-appliances: "The agent only targets a name ... the hub looks up
  the appliance's profile, which records its proxy (delegate) route"; "use the known-good worker;
  if offline, try other workers with the same reach and remember what worked". D6: service
  containers "become appliances or nothing".
- **Built instead.** No appliance profile or route state on the hub; the door requires `worker`,
  and `appliance` is just a name checked against that worker's announce (`dispatch.py:284`).
  Coverage N3 is open: `rook/core2/worker/host.py` never passes `appliance`, so tv, kvm, adb and
  cec run only against stand-ins. D6 depends on this.
- **Smallest fix.** N3 first (`ctx.appliance` in the host); then a hub table `appliance ->
  {plugin, delegates[]}` written by `appliance.test`, with dispatch trying the next delegate on a
  transport failure.

## 22. Worker secret settings have no vault path (missing, tracked)

- **Decision.** castling-enrollment (Bake confirmed): "Secrets: referenced only by vault name; the
  hub sends the value with each call; never written to worker disk."
- **Built instead.** The worker runtime gives the host no `resolve_secret`, so a worker plugin
  setting marked `secret` fails (`rook/core2/worker/host.py:395`); authors fall back to plain
  worker-local settings, on worker disk. Listed as a gap on castling-vault-built.
- **Smallest fix.** The hub resolves the target plugin's secret settings at dispatch and sends them
  beside `args`, as it already does for `{{secret:...}}` args.

## 23. The switchover plan's open lists use retired 0.4 names (contradiction, drift source)

- **Decision.** castling-caps: huddle -> `huddle.start` only; `show.present` -> core `page.show`;
  castling-attention: attention is "an aggregation job on the hub run by the house agent", the
  phone is thin, "no attention service".
- **Built instead.** `docs/castling-switchover.md` line 68 "Not ported ... should be done or
  explicitly dropped": attention N21/N23 (the 0.4 `attention.remind -> voice.speak
  {only_if_quiet}` rule), `show.present`, huddles. R2's rationale: "Attention and the Feed ...
  Without them the phone is a remote." Listing them under 0.4 names invites porting them, exactly
  as R14 did.
- **Smallest fix.** Rewrite those rows with the castling-caps names and outcomes; drop the Feed
  wording; R4 -> "change records (C2)", R14 -> "sign-off on tasks + agent PR reviews".
- **Fixed:** PR #339 (c8/ops): R2/R3/R4/R14 and the not-ported list in the decisions' terms;
  `readiness.py` R4 checks C2 (record, no apply/revert runner), R14 is sign-off + `github.merge`.

## 24-30. Lower severity

- **24. 0.4 cap aliases (over-built).** castling-protocol (Bake): "**Clean cut:** no 0.4/0.5
  compatibility." 96 `aliases:` entries in 27 manifests, all of them 0.4 names (`devkit.*`,
  `android.*`, `device.*`, `ccmod.*`, `pikvm.*`, `deluge.*`, `work.stream.*`, `attention.post` ...);
  dispatch resolves them (`_target_for`). The alias rule is for renames within 0.5 releases.
  *Fix:* drop the 0.4 names, or keep them to cutover + 1 release and say so in the switchover
  decisions. **Fixed:** PR #349 (c8/ops-cuts): all 91 dropped; the mechanism stays for 0.5 renames.
- **25. Child `approve_new` (over-built).** castling-design: "Deferred: kids and guest accounts";
  castling-manifest-presets: "Bake, Oct 30: canned, don't overthink." Built: a per-person "first
  call of each cap waits for an owner" queue (`rights.py:26`, `core.yaml:518`, `identity.py:160`,
  spec 8.5) on top of the `max_risk`/`effects` ceilings. *Fix:* remove `approve_new`. **Left for
  Bake (PR #338):** castling-manifest-presets itself lists `approve_new: owner`, so this needs his
  call: keep it, or cut it until kids' accounts are designed?
  **Fixed:** PR #351 (c8/ops-child): cut, by the coordinator's call under Bake's delegation (recorded
  as Claude's call): a child gets more only when an owner edits the preset or grants that person more.
- **26. Journal retention tiered by risk (over-built).** castling-journal: "The general chain is
  pruned by age (~30-90 days) in daily segments." Built: `RETENTION_DAYS = {read:30, write:30,
  exec:60, admin:90}` (`rook/core2/journal.py:26`), a segment kept for its riskiest entry, and a
  kept admin day holds back the days after it - in practice 90 days with extra code. Source:
  wave2 `journal.txt` ("tiered retention by risk"). *Fix:* one `retention_days`. **Fixed: PR #338**
  (`RETENTION_DAYS = 90`, `JournalStore(retention_days=)`).
- **27. `github` in core (over-built, package).** castling-dashboard: "**Core:** workers, settings,
  approvals ..., journal." `rook/plugins/github/plugin.yaml:12` `package: core` with
  `writers: [huddle]`. *Fix:* `package: workbench`; the sign-off rule (#3) replaces `writers`.
  **Fix in** PR #333, still open (`package: workbench`, `writers` gone).
- **28. Jobs notify the person directly (over-built, minor).** castling-agents: "**Channels are
  changeable, not fixed rules.** The house agent picks channel, device and timing"; "Other agents
  ... ask the house agent to get word to the stakeholder." `jobs.py:461-487, 605` call
  `notify.send` with a fixed channel order even when agents are installed. *Fix:* route through
  the house agent (the #8 path) when present; keep the order as the core-only fallback.
- **29. Desktop app ports 0.4 panels (over-built, low).** castling-attention: "No built-in views
  needed for things like knowledge or tasks: the agent opens the relevant page/link";
  castling-dashboard: tasks are not core. `desktop/src-tauri/src/hub05.rs:108-135` hard-codes Work
  (`task.list`), Knowledge and Sessions tabs that assume the workbench. Source: wave6
  `desktop.txt`. *Fix:* show holds + `pages.list` + the dock, or show tabs only when the caps exist.
  **Fix in** PR #352 (c8/ops-desktop): on 0.5 the panel shows holds, pages and the house agent only (split out of #349: its macOS job can't run).
- **30. No attention job on the hub (missing, known).** castling-attention: "Attention becomes an
  **aggregation job on the hub** run by the house agent". Nothing consumes
  `notification-android.received`/`sms.received`; the 0.5 phone plugin posts through the 0.4
  `AttentionStore` taxonomy; `docs/castling-phone.md:111` cites "the hub's attention job" as if it
  existed. On the D5 not-ported list. *Fix:* one jobs template (`on:` those events, batched) that
  runs the house agent with an `attention` skill.

## 31. Hub plugins rebuild storage instead of declaring it (over-built)

- **Decision.** castling-design: "**Declare, don't rebuild:** shared needs (records, permissions,
  schedules, streams) live in core once." castling-plugins: "Records: declare a record type ...
  the kit gives storage, migrations, change history, list/get/search, and generates the caps".
  castling-manifest-records: "Knowledge, tasks, projects, jobs, rooms, notepads and pages are all
  records." castling-enrollment: plugin settings can be per person.
- **Built instead.** agents opens raw `sqlite3` with hand-written `CREATE TABLE agent/note/memory`
  (`agents.py:110-116`) and hand-written `agents.list/get/create/update/archive`, so notepads and
  memory are not records. Telegram and Discord keep person -> chat links in `links.json` in the
  plugin data dir (`discord.py:228-234`, telegram likewise) though settings support
  `where: person`. Hub pages are folders of `meta.json` with hand-written `pages.*` caps
  (`doors/web/caps.py`). The private stream stores are #9. Not prompt-driven: the wave-4 agents
  prompt asked for streams and records.
- **Smallest fix.** Declare `agent`, `note`, `memory` as kit records; chat links as `where: person`
  settings; pages as records when the dashboard is next touched.

## 32. Core service plugins call providers by name, not by contract (contradiction, mild). agents fixed: PR #344

- **Decision.** castling-design: "Cap names say what they do, not who ships them"; "Discovery is
  passive". castling-manifest-contracts: provider routing exists so "a session declared for
  `notify.send` alone reaches `telegram.send`". C1: shared contracts any plugin can provide.
- **Built instead.** `notify/plugin.yaml:19-52`: `uses: [telegram.send, discord.send ...]` and
  channels as a fixed `enum: [phone, telegram, discord]`; notify calls `f"{ch}.send"`. speech loops
  over `("speech-engine", "speech-elevenlabs")` (`speech.py:181`), falls back to
  `speech-engine.tts` by name (292) and `uses` three named llm plugins. agents calls
  `f"agents-{kind}.turn"` (`agents.py:303`). A third-party notifier, TTS engine or backend can't
  plug in without editing a core plugin.
- **Smallest fix.** A `notify.channel` contract that phone/telegram/discord provide, with notify
  listing providers from the registry; speech and agents call only `voice.*`/a `tts` contract,
  `llm.complete` and `agent.backend`.

## 33. Plural namespaces (renamed, low)

- **Decision.** castling-caps rule 5: "Singular namespace = plugin name"; rule 4 verbs include
  "run/cancel"; the table's new names are `job.*`/`job.run`, `agent.*`, `page.*`/`page.show`.
- **Built instead.** `plugin: jobs` with `jobs.start`, `plugin: agents`, hub `pages.*`, while task,
  project, change and concept went singular. Inconsistent for agents, which the caps pass was
  meant to fix ("ergonomic for agents").
- **Smallest fix.** Rename to `job`, `agent`, `page` before switchover, while there are no 0.5
  users to alias for.

## 34. Manifest features nothing uses (over-built, low)

- **Decision.** castling-design: "Weigh complexity against value every time"; castling-plugins:
  "Manifest schema: written last, once all features are designed"; the manifest's own principle 2
  "Small by default". (The manifest pages are Claude's under Bake's delegation, so weaker than his
  words.)
- **Built instead.** Three format revisions in two days (wave2 `manifest-v11.txt`, wave6
  `gaps.txt`, both driven by the converter's gap list over 0.4 plugins). Across all 57 plugins:
  bare-namespace cap `""` used by 0; `requires.plugins` range enforcement (`kit/manifest/diff.py`,
  `hub/distribute.py:293`) by 0; route `auth: {hmac|token}` (`hub/plugins.py:267-284`) by 0; record
  `aliases` by 1 (claudecode's 0.4 `ccmod.*`); `assets`, `replaces`, `concepts` sections by 0. The
  one webhook route (jobs) uses `auth: none` and rolls its own HMAC with a master key in
  `<data>/webhook.key` (`jobs.py:870`), not the vault.
- **Smallest fix.** Freeze the format; mark unused keys "reserved"; drop the unused code paths
  until a plugin needs them; move the jobs webhook onto core route auth (key in the vault).
- **Partly fixed:** PR #349: format frozen at 1.2, unused keys marked reserved
  (`conformance/manifest/README.md`, castling-manifest pages); their code and the jobs webhook move
  are left (vectors shared with other validators; churn without a user).

## Watch items (ask Bake; not drift yet)

- **YAML creeping toward a language.** castling-plugins: "No loops, conditionals or variables in
  YAML; logic means `run:`. Keeps YAML from becoming a bad language." Arg `map:` on booleans is an
  if/else (`kvm/plugin.yaml:143` `force` -> `off_hard`/`off`; `enter` -> `"\n"`/`""`), argv groups
  appear only when their args are set, `fail_if` regex is in 12 caps. Each piece is small (engine
  ~816 lines) and `try` fallbacks are what Bake asked for ("ordered fallbacks"). Stop adding
  rendering logic; list the 1.1/1.2 additions on castling-manifest-lifecycle as Claude's calls
  under the delegation.
- **`c7/transport2` (unmerged).** Adds a Rook-layer ack/resend/dedupe transport extension and edits
  0.4 modules shared with the live line (`rook/worker/ota_drop.py`,
  `rook/worker/transports/telesthete_hub.py`). castling-protocol records Telesthete's reliable
  Channel; check whether it can carry these messages before opening the PR.
- **Enrollment without Google sign-in.** castling-enrollment: "sign in with the user's login
  (Google or Rook account)". Only the Rook account path exists; fine for Bake, note it for others.
- **Wave-agent pages that read like decisions.** castling-huddles-built ("Decisions" block),
  castling-journal-store ("tiered retention"), castling-switchover (R4/R14 as blockers),
  castling-manifest ("Bake delegated") record agents' own calls without marking them as awaiting
  Bake. Mark them "Claude's call" so later agents don't treat them as Bake's.

## Checked and honoured

- **Journal** (castling-journal): only the hub writes; project chains + a general chain, SHA-256
  links, write-once triggers, heads in memory + hourly check, pruning without gaps that journals
  itself; no HMAC, stubs or off-box signing (retention tiers aside, #26).
- **Tickets and elevation** (castling-protocol/-security, spec 6-7): one ticket per call bound to
  worker/cap/args/person/agent/session/nonce, 60 s expiry, 15-min replay cache, quiet re-mint;
  shell fully open, non-admin start, 15-min lease, fail closed, dropped on access change and hub
  loss, journaled never prompted (gate aside, #16).
- **Trust map and holds:** no 0.4 trust map, relays, proposals or votes in core; trust is a
  `sacred` list + `project.trust`; one hold path (`approval.list/decide`); hard stops (sacred,
  destructive, secret reveal, sends_as, spends) per spec 10.3 and released only by the person.
- **Multi-band** (castling-multiband): home hub owns the worker, invite never pull, signed
  memberships and per-hub ceilings, nothing for the user to manage; no personal keypairs or
  device-key hierarchies.
- **Enrollment** (castling-enrollment): QR one-time code, mDNS, device code, config bundle over
  HTTPS; recovery key a plain Steam-style string viewable again (castling-backup).
- **Security plugins:** canary + taint + pinned task + triage release (capped) as a replaceable
  gate plugin; security-ml separate, shadow mode, nightly retrain excluding flagged sessions,
  locked attack set. MCP door kill switch (castling-protocol input 6).
- **Plugins** (castling-plugins): in-process Python, config-first YAML with `run:` as the escape
  hatch, signed plugins, push over Drop with hot-load and rollback, plugins talk only through
  caps (`uses`; no cross-plugin imports, checked), shell is a plugin, process-starting plugins go
  through core's `launch`, dependencies resolved with uv against each worker. Telesthete untouched
  (pinned v0.3.0); the hub stays Python. Nine hub plugins use kit records (#31 aside).
- **Caps** (castling-caps): C1 shared contracts in `builtin/core.yaml`; C3 `decide.run/status/drive`
  kept; C5 no persona push; C6 `claudecode` install/remove/status + in-hook rules; no product
  prefixes (`torrent`, `kvm`, `tv`); action arguments split into real caps. Naming B9 (`call`,
  `clock`, `location`) and B10 (`speech` service, `voice.*` contract).
- **Workbench:** knowledge carries provenance `sources`, no verification or claims field, and
  `import_04` drops claims; concepts are tags (`project.concepts` is a list), projects parent
  tasks; whiteboards per task/project, volatile, "its current state is the handoff"; tasks,
  projects, knowledge, concepts in `package: workbench`, not core.
- **Agents and voice** (castling-agents): no special house agent (persona + grants); one brain
  (speech hands off to `agents.chat.send`; jobs, security, huddle go through agents); voice is a
  motor skill with System One only in voice sessions, speaker ID advisory; notepad, read-only
  memory during work, dream on idle (System Three); reach-the-user and presence are skills.
- **Inference** (castling-inference): Claude Code is the default backend; `llm-cli` covers
  subscriptions; `llm-anthropic` (API key) is opt-in; llmanifold optional.
- **Dashboard** (castling-dashboard): core pages are workers, approvals, journal, settings; no 0.4
  sections (Feed, Intelligence sources, Trust) came back; sandboxed cap-only pages, page lifecycle
  as caps, agent-made pages carry a note and link a task.
- **Jobs** (castling-jobs): cron/every/on/manual/webhook triggers; a job runs a cap, a script or an
  agent as its creator or a persona; no checkpointing, as decided; ~1.1k lines against 0.4's 6.4k,
  so the plumbing really was dropped.
- **Switchover decisions D1-D9 and H2** are recorded word for word; the standing rule "never leave
  Rook broken" is in every wave 6/7 prompt.

## Suggested order

1. **#0 + #3 + #4 together** (the R14 rework already in flight): sign-off and agent PR reviews
   under agents' own identities, then retire the huddle machinery and the chair skill. Change the
   prompt boilerplate at the same time (see "How the drift got in").
2. **#1 + #2 + #13:** change records back to records (C2); core stops depending on the plugin.
3. **#12 before #327 merges; #14 before #332 merges.**
4. **#5, #6, #10, #18** are prompt/manifest edits; **#8** (`approval.ask`) is small, unblocks
   external agents and removes the fake `destructive` tags. **#33** (renames) is cheapest now.
5. **#7, #9, #11, #15, #16, #17, #19, #20** before cutover (D1/step 3 depend on #19 and #20).
6. The rest as they come up.
