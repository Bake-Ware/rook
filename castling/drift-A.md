# Design drift audit, part A: core and security

Oct 31 2026, branch c7/audit-A, read against origin/castling 9d9665b7 (#326 merged) plus the open PRs
#327, #328, #331, #332 and the unmerged branch c7/transport2. Part B (product layers) is in
drift-B.md; the main audit folds both into castling/drift.md. Report only: nothing here changes code.

Sources of truth: the Rook knowledge pages castling-security, -protocol, -multiband,
-workers-appliances, -enrollment, -journal, -changes, -first-run, -plugins, -manifest (and its
section pages), -caps, -naming-decisions, read Oct 31. Context pages: castling-design,
-workbench, -backup, -switchover-decisions. The prompts that steered the agents are in
~/rook-backups/castling-wave1..7/prompt-*.txt.

The pattern behind the huddle case (#329) shows up again here. A wave prompt or a
docs/castling-switchover.md gate says "port the 0.4 X", and the agent ports X's 0.4 shape over
the Castling decision that replaced it. A1, A3 and A8 come from this. A2's prompt asked for the
contradiction word for word.

## Findings, worst first

### A1. Change records rebuilt the 0.4 CR runner (contradiction + over-built). PR #322, merged

- **Decision.**
  - castling-caps C2 (accepted by Bake, Oct 30): "**Change requests** (4): records only, with apply, test and rollback as a job the record links."
  - castling-changes: "Ceremony scales with the project's trust level in the trust map: sacred = full record + owner approval; normal = full record, proceeds on its own; ship-it ... one-line note."
  - castling-changes, priority order: "3. **Gating, the least important.** If things work well, gating is rare."
- **Built instead.**
  - `rook/plugins/changes/` (649 lines) contains its own executor. `change.submit` runs `checks` → `scripts` → `tests` → `rollback` steps, waits up to `rejoin: 5m` for workers, and keeps a state machine of 9 states. That is 0.4's change runner, not a record that links a job.
  - Gating has a second axis Bake never chose: an agent-picked `kind: standard | risky`. `risky` asks for a card even in a normal project (`changes.py:490`: `kind == "risky" or level == "sacred"`).
  - To force that card, `change.apply` is declared `effects: [destructive]` (plugin.yaml). Every risky change is therefore journaled as "erases data for good", and the hard-stop list of spec 10.3 is bent to mean "needs a card".
  - PR #326 (merged) made each worker update a change record through the same runner.
- **Where it came from.**
  - wave7/prompt-changes.txt: "Per ... the 0.4 change.* caps on master (change.submit, decide, get, list, revert...) ... standard changes ... the hub runs, tests and rolls back by itself; risky/permanent ones need one decision card ... Port the 0.4 deploy flow's shape".
  - docs/castling-switchover.md R4: "Without them nothing replaces the 0.4 CR runner."
- **Smallest fix.**
  1. Drop `kind`. Ask for the card only when `level == "sacred"`, through the trust map's sacred hold (spec 10.3 "a change on a sacred machine" is already a hard stop), not a fake `destructive` flag. Remove `effects: [destructive]` from `change.apply`.
  2. Turn `checks/scripts/tests/rollback` into a `job` field: the record links a jobs-plugin job, which castling-jobs already designed as "scripts with steps and branching". Delete the executor and the `rejoin` setting.
  3. Fix the R4 row in castling-switchover.md so it stops naming the 0.4 runner as the target.

### A2. Core "2.1" breaks 2.0 workers yet is called a minor version (contradiction). PR #327, open

- **Decision.** castling-protocol, versioning (Bake): "anything non-breaking in v2 is a minor version ...; every v2.x is compatible with the v2 spec." Also: "anything added in a minor version is optional with a sensible default when missing ... Anything that breaks these is v3."
- **Built instead.**
  - A 2.1 hub refuses `core.call` / `core.elevate` from a 2.0 worker (`denied: core.call needs core 2.1 ...`). That makes `proof` required, so it is a breaking change labelled minor.
  - Two compatibility mechanisms exist only to bridge it:
    - trust-on-first-use key pinning from the first announce, spelled out in spec 15;
    - the 2.0 refusal path.
  - The only 2.0 node anywhere is the cachyrig sidecar. 0.5 hasn't shipped.
- **Where it came from.** wave7/prompt-workerkeys.txt: "Version this as core v2.1 (minor-compatible: ... a v2.0 worker gets a clear refusal for these requests ...)". The prompt asked for both halves of the contradiction.
- **Smallest fix.**
  - Before merging, fold H2 into **2.0** itself. Nothing implements 2.0 outside this repo, and Castling is a clean cut.
  - Keep enrollment-time key pinning. Drop the first-announce TOFU path and the 2.0 refusal. Re-enroll the sidecar.
  - If a version bump is wanted anyway, call it 3.0.
  - Separately, the signed `rook-reply` on core results and the 256 sub-call cap go beyond H2. They are defensible, but ask Bake rather than add them in silence.

### A3. The MCP door rebuilds the full OAuth server that 0.4 deliberately removed (over-built). PR #332, open

- **Decision (0.4).**
  - 2026-07-31: band_mcp's full OAuth authorization server (`oauth.py`, `InMemoryProvider`) was deleted for bearer tokens only.
  - 2026-08-10, commit 2baf1a2: `rook/band_mcp/oauth_shim.py` re-added *only* a front door for claude.ai web. `/authorize` auto-approves with PKCE, there is **no client registration**, the OAuth `client_secret` *is* a Rook bearer token, and the issued access token is that same token. There is no separate token store.
  - Castling principles back the same direction: "Weigh complexity against value every time; cut what doesn't earn its place."
- **Built instead.** `rook/core2/hub/oauth.py` plus routes:
  - RFC 8414/9728 metadata and open RFC 7591 dynamic client registration (up to 500 clients, oldest evicted);
  - a consent page with an agent picker;
  - 1 h access tokens and rotating refresh tokens with reuse detection, which lapse after 30 days;
  - RFC 7009 revocation and RFC 9207 `iss`;
  - an `oauth.json` token store;
  - a new builtin `connector` plugin and a Connectors page.
- **Where it came from.** wave7/prompt-oauth.txt asked for exactly this: "dynamic client registration, PKCE ..., short-lived access tokens + refresh, revocation from the dashboard". It cited the 0.4 shim but not why the shim is so small.
- **Smallest fix.** Port the shim onto the 0.5 door:
  - `/authorize` (PKCE, sign-in on the hub site, no consent screen);
  - `/token` exchanging `client_secret` = an existing agent token (`token --person --agent` already makes one that intersects rights);
  - access token = that token.
  - Revocation is the existing token revocation. Drop DCR, refresh tokens, `oauth.json` and the `connector` plugin and page.
  - Only if claude.ai *mobile* turns out to need DCR, add registration as a no-op endpoint. Note that 0.4's web connector works with manual client id/secret.

### A4. The pinned-task check in the security gate never runs (missing, failing quietly). Merged (#284 and later)

- **Decision.** castling-security, prompt-injection defenses: "pinned task recorded at session start, checked by the gate".
- **Built instead.** `rook/plugins/security/plugin.yaml` sets `task_cap` to `task.task.get` and lists `task.task.get` in `uses`.
  - Manifest 1.1 rule C1 (and `rook/kit/records/store.py:81`) names a record that shares its plugin's name without the middle segment, so the tasks plugin serves `task.get`. `task.task.get` doesn't exist.
  - `security.py:326` catches the failure at `log.info` and stores an empty word set. `near` then stays `None`, so the `intent_mismatch` check and the triage agent's `matches_task` evidence never apply on a real hub.
  - The plugin test hides this by declaring its own fake `task.get` (`tests/plugins/test_security.py:64`).
- **Smallest fix.**
  - Default `task_cap: task.get` and `uses: [... "task.get"]`.
  - Log the failure at warning level.
  - Add a test against the real tasks plugin manifest.

### A5. The security gate ships in enforce mode with heuristic holds (contradiction)

- **Decision.**
  - castling-security: "Alarm fatigue is one of the biggest real threats ... only reaches the human for rare, high-confidence events." Also "Ship in shadow mode first."
  - Spec 10.2, written from that decision: "Gates ship in shadow mode first."
- **Built instead.** `rook/plugins/security/plugin.yaml` has `gate.mode: enforce`. Besides canaries, which Bake rated "near-zero false positives — build first", the gate turns heuristics into holds:
  - taint + "risky" + no word overlap with the pinned task → `flag` → hold;
  - taint + an egress-looking regex → hold;
  - admin or persistence calls with no word overlap → hold.

  These are word-overlap and regex checks with unmeasured false-positive rates. In enforce mode each one becomes a card.
- **Smallest fix.**
  - Keep canary blocks enforced.
  - Have the taint and intent checks return `allow` with their reason journaled (shadow behaviour) until a setting flips them, e.g. `heuristics: shadow | enforce`, default shadow.
  - Or split them into a second gate entry with `mode: shadow`.

### A6. Appliances: no hub routing, and the worker host still can't serve appliance caps (missing)

- **Decision.** castling-workers-appliances, "Model" and "Routing":
  - "The agent only targets a name ('downstairs light switch') and never needs to know how to reach it; the hub looks up the appliance's profile, which records its proxy (delegate) route".
  - "Onboarding sets the route ... use the known-good worker; if offline, try other workers with the same reach and remember what worked".
  - "Retry across routes inside one request".
  - Spec 5.7 restates it.
- **Built instead.**
  - No appliance profile or route state exists on the hub. The door requires `worker` (`runtime.py` `call`: "worker and cap are required"), and `appliance` is just a name checked against that worker's announce (`dispatch.py:284`). There is no known-good route, fallback delegate or learned reachability.
  - On the worker, coverage gap **N3** is still open: `rook/core2/worker/host.py` never mentions `appliance`, so instance settings and credentials don't travel with the call. tv, kvm, adb and cec are tested with stand-ins only (coverage.md lines 91, 119, 179, 329).
- **Smallest fix.**
  1. N3: pass `appliance` from the request into `Host._exec` and give run code `ctx.appliance`. coverage.md already sketches this fix.
  2. A hub table `appliance → {plugin, band, reach, delegates[]}` written by `appliance.test` on success. Dispatch resolves a call with only `appliance` to the first online delegate and tries the next on a transport failure.
  - Neither step needs the profile UI.

### A7. Rook 0.5 has no agent-led install path (missing)

- **Decision.** castling-first-run:
  - "README leads with: **the easiest way to get started with Rook is to ask whatever agent you're already using to set it up** (point it at the GitHub repo)."
  - "The repo carries **instructions for agents** (a setup skill / agent section of the README)".
- **Built instead.**
  - `README.md` on castling is still the 0.4 README ("a hub in one command (or Docker...)", band key as root password).
  - `skills/rook/` is the 0.4 skill.
  - docs/manual-0.5/install.md exists, but nothing tells an arriving agent to run the install conversation (subscription, always-on machine for the hub, what else to add).
  - Switchover decision D1 makes `castling` the new `master` at step 2, so this is the README new users land on.
- **Smallest fix.** Before D1:
  - a short 0.5 README that opens with the "ask your agent" line and links the manual;
  - `skills/rook/setup/SKILL.md` for 0.5 with the install conversation from castling-first-run and castling-inference.

### A8. Worker updates ported 0.4's OTA as caps, and the hub still deploys the 0.4 way (over-built, renamed). PR #326, merged

- **Decision.**
  - castling-caps rule 8: "Plumbing is not a cap: enrollment steps, OTA begin/apply, commit-confirm, settings report/fetch and event polls."
  - castling-protocol: "Clean cut: no 0.4/0.5 compatibility."
  - castling-plugins: "selfupdate ... move[s] into core."
- **Built instead.**
  - `worker.update` and `worker.rollback` in `builtin/worker.yaml` are OTA apply and rollback, exposed as callable caps.
  - A staged canary-then-batch rollout (`update.prepare/rollout/status/history`) wraps one change record per worker around the A1 runner.
  - The PR notes that "0.4's `apply_hub` stays the hub's deploy" for the 0.5 hub.
- **Where it came from.** wave7/prompt-release.txt: "Today 0.4 has OTA: ... staged rollout ... Build the 0.5 equivalent: ... worker.update cap ... staged rollout (canary workers first, then the rest)".
- **Smallest fix.**
  - Keep `update.rollout` as the one person- and agent-facing verb.
  - Make the worker side `core.update` in the core manifest, plumbing like `core.plugin.install`, so it isn't something agents discover and call.
  - Drop the per-worker change record once A1 is fixed. One record for the rollout is the "lasting change".
  - Give the 0.5 hub its own deploy (a `change.submit` that runs `update.prepare` on the hub host) before cutover, rather than keeping 0.4's `apply_hub`.

### A9. Secret settings on workers have no vault path (missing)

- **Decision.** castling-enrollment, confirmed by Bake: "Secrets: referenced only by vault name; the hub sends the value with each call; never written to worker disk." Spec 12 says the same.
- **Built instead.**
  - The worker runtime never gives the host a `resolve_secret` (only the hub side does), so any worker plugin setting marked `secret` fails with "setting X is a secret but no vault is available" (`rook/core2/worker/host.py:395`).
  - castling-vault-built lists this as a gap.
  - The practical effect is that authors put credentials in plain worker-local settings, which are on worker disk. That is the opposite of the decision.
- **Smallest fix.** The hub resolves secret settings of the target plugin at dispatch and sends the values in the request beside `args`, as it already does for `{{secret:…}}` args. The worker host reads them from there.

### A10. 0.4 cap names carried into 0.5 as aliases (over-built, low)

- **Decision.**
  - castling-protocol (Bake): "**Clean cut:** no 0.4/0.5 compatibility. All machines switch together."
  - castling-design: "Migration from 0.4 is not a product feature."
  - The alias rule ("Renames keep the old name as a deprecated alias for a release or two") is for renames within a line of releases.
- **Built instead.** 90 `aliases:` entries in rook/plugins and core manifests map 0.4 names into 0.5: `devkit.*`, `android.*`, `device.*`, `ccmod.*`, `hid.mouse.*`, `contacts.search` and so on. `_target_for` also resolves aliases at dispatch.
- **Smallest fix.**
  - Leave the mechanism and drop the 0.4 names from the manifests. Agents get the 0.5 names from `caps.describe` and SKILL.md.
  - If Bake wants a soft landing for his own agents, keep them until cutover + 1 release and say so in castling-switchover-decisions.

### A11. Child preset holds every first-time cap (over-built, low)

- **Decision.**
  - castling-multiband: "Kids/guest identities (e.g. Logan) — Bake: not now."
  - Later, castling-manifest-presets: "Bake, Oct 30: canned, don't overthink."
- **Built instead.**
  - `approve_new: owner` (rights.py, dispatch.py `new_cap`, identity.py, spec 8.5) adds a per-person "first call of each cap is held for an owner" mechanism.
  - The `max_risk` / `effects` ceilings already bound a child. This is an approval queue for a deferred feature, written by an agent under the schema delegation, not by Bake.
- **Smallest fix.** Remove `approve_new`. Children get the ceilings and the grants plugins add.

### A12. Journal retention picks tiers by the riskiest entry (over-built, low)

- **Decision.** castling-journal (Bake's check is "complexity vs value"): "The general chain is pruned by age (~30–90 days) in daily segments".
- **Built instead.** `journal.RETENTION_DAYS` tiers of 30/30/60/90 days by risk. Each day segment and each closed project chain is kept for the tier of its riskiest entry (`journal_store.py` prune). This is an extra policy axis. The checkpoint row is the "starts from the previous day's final hash" the design asked for, and it is fine.
- **Smallest fix.** One `retention_days` setting (default 60) for the general chain and one `after_close_days` for projects. Delete the risk ranking.

### A13. Core `approval.ask` (question cards) was never built (missing)

- **Decision.** castling-caps C4 (accepted by Bake, Oct 30): "Question cards (1, plus new approval.ask): a core `approval.ask` any agent (including external MCP agents) can use, routed through the house agent when installed."
- **Built instead.**
  - The approval builtin (`rook/core2/hub/doors/web/builtin/approval/plugin.yaml`) has only `list` and `decide`.
  - No `approval.ask` cap exists anywhere on castling. `rook/streams/README.md:69` already sends huddle questions to "`approval.ask` cards", so huddles point at a cap that isn't there.
  - The only way an agent reaches the person with a question is to get a call held.
  - coverage.md N30 records the neighbouring gap: a plugin can't ask for approval of its own step.
- **Smallest fix.** Add `approval.ask {question, choices?, why}` to the approval builtin. It makes a card through the same hold and card path with no call attached, and returns the answer. It goes through the house agent when the agents package is installed.

### Not drift, worth watching

- **Enrollment has no Google sign-in.** castling-enrollment: "sign in with the user's login (Google or Rook account)". Only the Rook account path (QR or device code approved by a signed-in person) exists. Fine for Bake; note it for other users.
- **Unmerged c7/transport2.** It adds a Rook-layer `ack` transport extension (acks, resends, dedupe) and edits 0.4 modules `rook/worker/ota_drop.py` and `rook/worker/transports/telesthete_hub.py`. It stays above Telesthete as Bake required, and it is optional per peer. But it rebuilds reliability that Telesthete's reliable Channel already specifies, and it changes 0.4 code shared with the live line. Ask before merging whether the 0.5 link should use Channel instead.
- **PR #331 adds a ticket field `owner: true` (spec 6.2, MAY).** It is minor-compatible: optional, ignored by older peers. It is honoured only from home/co-owner hubs. No drift.

## Decisions checked and honoured

- **Tickets** (castling-protocol, castling-security), spec 6.2/6.3:
  - one per call, bound to worker/cap/args_hash/person/agent/session/band/nonce;
  - expiry 60–120 s (Bake: "at least a minute");
  - quiet re-mint once on `expired`;
  - replay cache 10–20 min, the reference keeps 15;
  - no multi-call ticket;
  - no separate ticket log: nonce, ids and args_hash sit in the journal call entry (11.2).
- **Elevation** (spec 7):
  - starts non-admin, shell fully open, launch path for every plugin;
  - lease ≤ 900 s bound to person/agent/worker/session;
  - fail closed in 10 s; dropped on access change and hub loss;
  - "journaled, never prompted: no card, no ping" (Bake: "Notification: log only").
- **Per-hub ceilings on the worker** (9.2): anchor not writable by the worker process, memberships signed by the home hub, `core.trust.set` home-only.
- **Multi-band** (castling-multiband):
  - home hub owns the worker, with a full default ceiling;
  - invite, never pull;
  - roles home/co-owner/guest;
  - independent connections, heartbeat per band, filtered announce per band;
  - local management when the home hub dies.
  - No personal keypairs, device-key hierarchies or waiting-period recovery.
  - The H2 worker key was accepted by Bake (switchover decisions).
- **Gate hook at dispatch for every door, block-only, budget, journaled replacement** (spec 10.1/10.2).
  - An internal in-process path for platform agents and the MCP kill switch (`set_door`, `door_closed`) exist, as castling-protocol asked.
- **security-ml**: isolation forest + boosted trees, pure Python, `mode: shadow`, nightly retrain job that excludes flagged sessions, locked attack set, several clean nights before promotion, rollback.
- **Security triage agent**: releases one held call on evidence; never hard stops, canary hits, quarantined sessions, persistence or exfiltration. Canary hits block, quarantine, and tell the person in plain words with "that was me".
- **Journal** (castling-journal):
  - only the hub writes;
  - project chains plus a general chain in one table, SHA-256 links, heads kept in memory and checked;
  - write-once triggers, pruning that journals itself, no stubs, no signed off-box hashes, no HMAC.
- **Hard stops** (spec 10.3): sacred machine, destructive, secret reveal, sends_as, spends. The vault puts `secret.delete` and `secret.reveal` behind them.
- **Plugins** (castling-plugins):
  - in-process Python only;
  - config-first YAML with http/exec/tcp/ws/file and argv-only exec;
  - `run:` escape hatch; no loops or conditionals in YAML;
  - hub-signed `plugin.sig` and the worker loads only signed plugins;
  - push over Drop, hot-load, previous version kept, checks roll back;
  - uv resolution against the worker; shell is a plugin; plugins talk only through caps (`uses`).
- **Manifest**: one `plugin.yaml`, small by default; security facts in the file and decisions on the hub; unknown keys warn; JSON Schema in `conformance/manifest/`; additive 1.1/1.2 keep `manifest: 1`; no guidance key (D4).
- **Caps decisions**:
  - C1 shared contracts in `builtin/core.yaml` (screen, hid, ui, power, media, sound, battery, app, notification, clipboard, voice);
  - C3 `decide.run/status/drive` kept;
  - C5 no persona push;
  - C6 `claudecode` keeps install/remove/status;
  - product prefixes gone (`torrent`, `kvm`, `tv`); real caps instead of `action` args.
- **Naming** B9 (`call`, `clock`, `location` plugins) and B10 (hub service `speech`, device contract `voice.*`).
- **Telesthete untouched**; Rook changes sit above it (core-v2 2.1 profile).
- **Hub stays Python.** No Rust SDK.
- **Enrollment**:
  - QR with one-time code, then mDNS `_rook-hub._tcp`, then a typed address;
  - device code for awkward devices;
  - bundle over HTTPS only, scoped to the person;
  - plugin settings marked worker-local or hub-provided.

  Secret settings on workers are the A9 gap.
- **Backup recovery key**: one Steam-style plain string, viewable while the hub is alive (castling-backup). It does not reintroduce the "recovery codes" multiband rejected, which were for worker identity.
