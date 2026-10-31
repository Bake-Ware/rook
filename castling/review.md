# Castling hardening review (wave 6)

An adversarial read of `origin/castling` against `origin/master` (at
a387b537, 2026-10-31), against docs/spec/core-v2.md and the castling-*
knowledge pages. Scope: security first (tickets, replay, ceilings, leases,
dispatch rights, presets, gates, holds, secrets, the page sandbox, the MCP
door, enrollment, plugin signatures, path traversal, argv/URL injection),
then spec correctness, crash/robustness, concurrency and needless
complexity.

Every fixed finding has a test that fails on castling and passes with the
fix, in its own pull request (one theme each). Open findings say what to do.

All fixes together pass the whole test suite, `tests/e2e` included
(`rook-pytest tests`: 5241 passed, 2026-10-31).

Severity: **critical** = a narrow identity gets full rights; **high** = a
secret leaks, a signature or spec guarantee is bypassed, or one call can
take a worker or the hub down; **medium** = a real gap that needs an
unusual setup or a second step; **low** = hardening.

## Fixed

| # | Sev. | Finding | PR |
|---|---|---|---|
| D1 | critical | An agent-made page's calls ran as the viewer alone, so whatever it created was recorded as the viewer's: a page limited to `pages.create` made a page with `caps: ["*"]` and the owner as its only author, which then ran `shell.exec` as the owner. A made page now acts as its agent author for the viewer. | #294 |
| W1 | high | `core.plugin.rollback` loaded the kept version without re-checking its files against `plugin.sig` or the signing hub's role, and joined its `plugin` arg into a path (`../x` loaded any folder with a `state.json` and `plugin.yaml`). | #292 |
| H1-redos | high | `rights.matches` compiled globs to `.*` regexes; an agent's session caps (`*a*a*a…*!`) backtracked exponentially and froze dispatch. Linear matcher. | #293 |
| D2 | high | `{{secret:band-psk-*}}` resolved in any call by an owner, agents for the owner included, on any cap: `shell.exec echo` printed another band's PSK. Now only in the owner's own `core.trust.set`. | #295 |
| H8 | medium | Secret values came back in error text (a schema error quotes the resolved argument) to the caller and the journal (spec 6.4). Dispatch redacts what its secret step substituted. | #295 |
| D3 | high | An agent with an owner's token could `POST /enroll/code` and claim the code itself, getting the bundle with the home band PSK; or approve a stranger's device code. Enrollment is now for the person alone. | #296 |
| H1 | high | The hub never sent `core.lease.drop` (spec 7.4). Ending a session now drops its leases on the workers holding them. | #299 |
| H3 | medium | `elevation: upfront` caps always run elevated but dispatch used their declared risk: admin-matching gates, the sacred-machine hold (`risk != read`) and the journal missed them (spec 7.1). | #299 |
| H4 | medium | `core.elevate` checked person, agent, session and ceiling but dropped the call's extra layers (a page's caps, a plugin's `uses`), and didn't refuse an ended session or expired person. | #299 |
| H5 | medium | `core.call` sub-calls applied only the calling plugin's `uses`, not the parent call's layers: a page declaring `file.read` reached whatever `file` uses. | #299 |
| H6 | medium | Releasing a hold reused the cap and worker data from hold time: a cap re-announced as `destructive` (or a machine made sacred) after a gate-flag hold could be released by triage. | #300 |
| K5 | medium | `agents-claude-code.turn` passed `session` after `--resume`, whose value is optional: `session="--dangerously-skip-permissions"` became a flag and bypassed `permission_mode`, `allowed_tools`, `isolate`. | #301 |
| K4 | medium | The action engine escaped a `..` URL path argument to `%2E%2E`, which yarl decodes and resolves: each templated path segment could climb one level. Latent (no shipped plugin templates a path). | #301 |
| K1 | high | `file.search` (risk read) ran the caller's regex in the worker; `re` holds the GIL, so `(a+)+$` froze every thread, heartbeats included, past any timeout. The scan runs in a killable child now. | #302 |
| K2 | high | `file.read` on a FIFO blocked in `open()` forever; timed-out calls keep their executor thread, and a few dozen exhaust the loop's default executor (every sync cap, `getaddrinfo`). Regular files only now. | #302 |
| K3 | high | Config-action `exec` skipped core's launch path (spec 7.1): root workers ran actions as root, leases never applied. Every exec child also inherited the hub's `ROOK_SECRET_*` vault stand-in. | #305 |
| D8 | low/med | An unread request body on a kept-alive connection was parsed as the next request (smuggling behind a shared proxy connection). | #303 |
| D10 | low | No socket timeout on the HTTP door: idle connections held threads forever. 60 s now. | #303 |
| H7 | high | Hard-stop releases were open to any person-only caller: `approval.decide` is an ordinary hub cap, so a job a person made or a hub plugin's `ctx.call_as` released that person's destructive or `sends_as` holds (spec 10.3). Hard stops now release only from a door where the person is there (the site, `POST /holds` with their own token); `system:` releases narrowed to the security triage component, for its own gate's flags. | #310 |
| H5 (hub caps), H10 | medium | A hub cap's `ctx.call` didn't carry the layers of the call it ran in (a page's caps), and sub-calls dispatched with `door=None`. Hub caps now carry the door's layers and the door (plugin `uses` don't compose across hub hops: each plugin's own apply); `core.call` goes through the parent's door. | #311 |
| H9 | medium | Agents read any project's journal by naming it at session start. A session names only a project its caller may see (owners any; others need rights to read projects and the project on the workbench). | #312 |
| H11 | medium | An exception inside dispatch gave the caller a traceback and no journal entry. Dispatch and release answer `internal error` and journal in a `finally` (`error: internal`); a failing journal store is logged, never raised to a caller whose call ran. | #313 |
| K6 | medium | Canary scans stopped at 8 MiB and re-dumped args on the hub loop. The scan reads head and tail, dumps once, and runs off the loop. | #314 |
| D5 | — | Did not reproduce: the shell's `frame-src 'self'` governs every navigation of the frames it embeds, including one the frame starts itself (verified in Chrome 149, with a control run). A real-browser test locks it in. WebRTC is a separate channel: see Open. | #315 |
| D7 | medium | The MCP kill switch covered only `dispatch()`. A closed door now refuses everything after auth (views, sessions, calls, no-token routes); a closed MCP door refuses agents' tokens on every door; it persists (`doors.json`) and has a route (`POST /doors/<name>`) and a cap (`hub.door`). | #316 |
| D6 | medium | Page events weren't scoped to the viewer: an adult's page heard other people's messages and holds. Events with a `person` are that person's and owners'; `taints` events naming nobody are owners'. | #317 |
| D4 | medium | Device-code queue flood. Unpolled device codes go after 30 s; approvers see each code's address and age. | #318 |
| K2 (general) | medium | Sync plugin code ran on the loop's shared default executor. Each plugin has its own bounded pool; kvm reads are capped (16 MiB, 30 s). | #319 |
| D9, D11 (part) | low | Tokens are revoked live (`tokens.json` is the whole list), a removed agent's token is nobody, `POST /login` refuses cross-site browser requests (login CSRF), `_poll` reads the session under the lock. | #320 |
| K9 (part) | low | `shell.exec` keeps at most 8 MiB per stream; kvm, llm-anthropic and llm-openai refuse redirects (urllib re-sent the password/key to any host). | #323 |
| H2 | high | `core.call`/`core.elevate` took a bearer ticket: any PSK holder who read a ticket in flight got sub-calls and elevation as the call's person. Folded into core 2.0 (Claude's call: 2.0 never shipped beyond the cachyrig sidecar, which re-keys on upgrade): workers have their own key (pinned per hub at enrollment, else first announce) and sign these two requests over the request, the parent ticket's nonce, a fresh nonce and the time; the hub checks sender = ticket's worker, refuses replays, caps sub-calls at 32 per parent call, each journaled with `parent`. | #327 |
| CI | — | `test_ask_answer_cancel_and_timeout` (jobs) raced on CI py3.12 and failed unrelated PRs. | #304 |

## Open

### High

None.

### Medium

- **D5 residual: WebRTC.** A page can open an `RTCPeerConnection` with a
  STUN server named after the data (UDP and DNS out). Chrome 149 ignores
  CSP `webrtc 'block'`, no sandbox flag covers it, and removing the
  constructors in the bridge is defeated by a `srcdoc` child frame running
  its own script (all verified with tests/test_core2_page_sandbox_browser.py's
  harness). With D1 fixed the reach is the page's caps intersected with its
  agent author. A policy call for Bake: e.g. agent-made pages don't get caps
  that read sensitive or personal data, or a browser policy on viewers.

### Low

- **D9 (rest):** `/workers` and `/caps` answer any valid token (a child, a
  guest) with the whole band roster: a policy call, not changed.
- **D11 (rest):** the MCP door accepts person-only tokens (spec 10.1 says
  agents; changing it could cut people's MCP clients off); the mDNS
  responder answers unicast from any source (small reflector); with
  `--discover` an attacker on the LAN can answer mDNS and relay a
  device-code flow (approvers now see the request's address and age, #318).
- **H12:** the journal's `args_hash` is over unresolved args, the ticket's
  over resolved ones, so a journal entry can't be matched to a worker's
  audit line; released holds don't check `session.exp` (holds don't expire
  by design: which wins is a spec call); a worker's manifests supply its
  own `uses`, so a compromised worker can widen its `uses` layer; jobs
  created by an agent for a person store `creator` as
  `"person:x via agent:y"`, which `Caller.problem()` rejects (fails closed:
  those jobs never run).
- **K7:** `on`/`every` hooks have no timeout or backpressure (jobs listens to
  `on: "*"`).
- **K8:** hub-side config actions don't resolve secret settings (they render
  the vault name), skip `cap=` (arg map/join, result coercion) and get no
  `tmp`.
- **K9 (rest):** the jobs webhook has no timestamp or nonce (a captured
  signed POST replays; needs a sender-side change); `trust_class` calls
  YAML-only plugins `config` though their `exec` actions run any binary
  (don't let it gate installs as "no code"); `records/store.py`
  `_check_refs` checks band, not visibility (an existence oracle for
  private record ids).
- **Plugin module names** (`rook_plugin_<version folder>_<stem>`) collide
  between two plugins of the same version with the same script name
  (known, castling-worker-host follow-up).
- **Replies are unauthenticated** on the band (spec 15 accepts it): a PSK
  holder can answer a request id it saw, including results the hub acts on
  (`core.manifests`, install reports, an update's `worker.status`). The worker
  key could sign them; kept out of H2's fix (Bake decides).
- **Plugin `python` pins** install from PyPI without hashes: the hub signs
  names and versions, not artifacts.
- **CI flakes seen while landing these** (unrelated to the fixes, rerun
  green): `tests/test_kit_actions_engine.py::test_exec_timeout_kills_the_process_tree`
  (`_alive()` reads `/proc/<pid>/stat` of a process that just exited:
  ESRCH), `tests/test_names_rooms.py::test_console_search_matches_names`,
  `tests/plugins/test_security_ml.py::test_retrain_skips_flagged_sessions_and_promotes_after_clean_nights`.

## Checked and sound

- Worker ticket check (`wire.verify_ticket`, `WorkerGate.check`): order and
  reason codes per 6.3, lifetime bounds, skew, args binding, ceiling with the
  `ALWAYS`/`HOME_ONLY` exceptions; replay cache is file-backed, fsynced,
  bounded, refuses on a lost or torn file for 150 s, refuses when it can't
  record.
- Trust store: anchor read-only with `key_id(root)` check; memberships
  verified on load against the grant that certified their key, no rollback
  (`iat` must grow), role `home` only for the home hub; leases dropped when a
  membership changes and when the hub is lost.
- Leases: bound to hub, band, worker, person, agent, session; ≤ 900 s;
  memory only; elevation fails closed on timeout or refusal.
- Plugin installs: nothing written before the signature checks out; tar
  members regular files only, safe relative paths, case-fold duplicates
  refused, exact file set and hashes; start-up re-check of hashes and role.
- Launch: root workers drop to `run_as` or refuse; elevated calls go through
  `sudo -n`.
- Dispatch: every layer must pass; malformed rights refuse; unknown risk is
  admin, scope external, effects destructive for holds; presets keep
  `max_risk`/`effects`; plugin grants confined to their namespace; gates
  run in parallel under a 200 ms cap, errors/timeouts/malformed results
  count as `flag`, `clean()` keeps only verdict/confidence/reason, shadow
  ignored; gate input has secret references unresolved; held calls are
  deep-copied and run exactly; release/refuse delete the hold before any
  await (no double run); sessions don't grow, `tainted` never clears.
- Journal: write-once rows (triggers), chain hash covers `prev`, chain and
  project stamped by the hub.
- Doors: tokens stored as SHA-256 digests (256-bit tokens); cookie
  HttpOnly + SameSite=Strict; CSRF header on cookie POSTs; page iframe
  `sandbox="allow-scripts"` without `allow-same-origin`, CSP `sandbox`,
  `frame-ancestors 'self'`; postMessage source checks both ways; the shell
  uses `textContent`, escapes injected JSON; static and page files confined
  by `resolve()`; MCP JSON-RPC errors handled, Origin allowlist enforced;
  request bodies capped.
- Enrollment codes: 128/256-bit, single use, 10 min; the device refuses
  plain http off loopback and pins the hub id from the QR.
- Kit: templates render once (no re-templating, no eval), argv slots don't
  split, CR/LF/NUL refused in headers, host slots restricted; records store
  validates identifiers and quotes FTS; manifests load with a SafeLoader
  without aliases; `run:` paths confined to the plugin folder.
- Plugins: jobs scripts owner-only, run through `launch` with `-I` and a
  scrubbed env, killed on timeout; security gates only restrict, triage
  release limited and never for persistence/exfiltration; telegram and llm
  errors never include tokens; migrate seals vault values (SecretBox), files
  0600, reports names only.
- A plugin exception, `SystemExit` or a failing hook never stops the worker
  (the host turns all of it into error replies).

## Complexity

Nothing found that asks for a redesign or a cut: the fixes above are small
and local, and none adds a new abstraction.
