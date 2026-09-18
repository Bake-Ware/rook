# Kaiju decision shadow deployment

Source branch: `feature/voice-decision-shadow`. Deployment uses a commit-named
release under `/home/bake/voice-agent/releases/`, with `services/`, `REVISION`,
and `90-rook-voice.conf`. The effective unit override is
`/etc/systemd/system/voice-agent.service.d/zz-rook-voice.conf` (the `zz-` prefix
preserves precedence over `front.conf`). Runtime Python, model paths, TLS, auth,
Whisper GPU configuration and the existing bind address are preserved.

Build the release from committed `services/voice` files, copy the old release's
`90-rook-voice.conf`, change only `WorkingDirectory`, and append:

```ini
Environment=DECISION_URL=http://127.0.0.1:8910
Environment=DECISION_TIMEOUT_MS=150
```

Install that file as the effective `zz-rook-voice.conf`, run `systemctl
daemon-reload`, then restart `voice-agent.service`. Do not modify `auth.conf`,
`front.conf`, the old release, or any model service. The new SQLite tables are
additive; old code ignores them.

One-command rollback, **on kaiju**:

```sh
sudo -n sh -c 'install -m 644 /home/bake/voice-agent/releases/84f2e66/90-rook-voice.conf /etc/systemd/system/voice-agent.service.d/zz-rook-voice.conf && systemctl daemon-reload && systemctl restart voice-agent.service'
```

Before deployment, four fresh authenticated protocol-2 text conversations asked
`What is two plus two? Reply in one short sentence.` with `speak: false`:

| Sample | First reply (ms) | Done (ms) |
| --- | ---: | ---: |
| Cold | 630.545 | 630.649 |
| Warm 1 | 246.091 | 246.172 |
| Warm 2 | 245.360 | 245.441 |
| Warm 3 | 244.369 | 244.461 |

Warm median done: **245.441 ms**. Baseline GPU memory: GPU0 **20,838 MiB**,
GPU1 **21,289 MiB**. No real household transcripts were collected for validation.

Pre-deploy validation: 30 tests passed locally and with kaiju's deployed Python
3.12 environment. Test dependencies were isolated in staging; the production
venv was not modified. Live engine smoke with the factual state fields returned
`device_control` (p=0.9971) and `needs_response` p=0.9550 for the voice lights
example, in 100.5 ms cold; the typed request completed in 58.6 ms.

## Deployment result: rolled back

Candidate `fa333b21567c7b9a73b757a921ec172c4b7cdd91` was installed as
`releases/fa333b2`. Health returned 3.86 seconds after restart. All four
`thinking:true` conversations received a normal arithmetic reply and exactly one
successful decision with matching turn ID; all four `thinking:false` conversations
received normal replies and zero decision events, including a 350 ms late-event
observation window. Typed decisions had five answers and omitted `needs_response`.
Live voice-state engine smoke again classified the lights request correctly
(`needs_response` 0.9550, `device_control`), in 60.7 ms.

**The candidate failed the reply-latency gate and was immediately rolled back.**
No second candidate rollout was attempted. There was one deployment restart and
one required rollback restart. Production now runs **84f2e66**, with
`DECISION_URL` absent and shadow mode off. The candidate release remains available
for review; no push or merge was performed.

| Configuration | First sample done (ms) | Subsequent samples done (ms) | Warm median (ms) |
| --- | ---: | --- | ---: |
| Original 84f2e66 | 630.649 | 246.172, 245.441, 244.461 | 245.441 |
| Candidate, thinking true | 309.845 | 271.341, 267.793, 280.365 | 271.341 |
| Candidate, thinking false | 270.551 | 280.932, 281.908, 286.929 | 281.908 |
| Restored 84f2e66 | 293.848 | 248.233, 243.702, 244.406 | 244.406 |

The opt-in candidate's warm median was **25.9 ms / 10.6% slower**. First-reply
latencies were within 0.2 ms of done times for these one-sentence responses.
Decision durations were 108.5 ms cold and 63.3–65.7 ms warm, all below the 150 ms
deadline. This is a small, synthetic text sample, not a general acoustic benchmark.

After rollback, an interleaved read-only check against the original server sent
the same decision requests from an independent client. Median done times were
246.9 ms without concurrent inference and 252.4 ms with it. This supports a small
shared-resource cost even without the new server code; it does **not** explain
the full candidate regression. Profile SQLite writer contention and event-loop
scheduling alongside shared GPU inference before another rollout. The fake-engine
test proves no explicit inference wait in the reply path, not zero resource cost.

All **eight** candidate decisions were verified in the existing mode-0600 SQLite
DB with status `ok`, state, answers, version metadata, latency and reply/completion
timestamps. These synthetic conversations closed before the 15-second silence
window. Outcome linking, repeat/correction/confirmation signals, silence lifecycle,
retention and export were verified by tests rather than fabricated production
feedback. The additive tables remain through rollback. Automatic pruning runs
only with the new feature enabled; the retained rollout rows contain synthetic
test input. The exporter independently excludes expired raw examples.

Candidate and final GPU usage: GPU0 **20,752 MiB**, GPU1 **21,289 MiB**. GPU1
matched baseline exactly; GPU0 was 86 MiB below its pre-restart measurement.
The auth, front and GPU-STT drop-ins had identical SHA-256 digests before and after.
The final voice service is active and its TLS health check succeeds. Gemma,
llamacpp and the decision engine remain active; the two model health endpoints
and engine `/info` respond successfully. No protected model service or repository
was modified.

Detailed synthetic timing artifacts on kaiju:

* `/home/bake/voice-agent/staging/shadow-deployed-smoke.json`
* `/home/bake/voice-agent/staging/shadow-rollback-latency.json`
* `/home/bake/voice-agent/staging/shadow-contention-check.json`

Open validation limits: no physical-phone/acoustic test in this rollout; the
correction classifier and live context distribution do not inherit the synthetic
household ECE guarantee. Outcome signals need review before training. Silence,
interruptions and repetitions can have benign explanations.


## 2026-09-18: turn-failure hotfix and deferred opt-in decisions

Candidate fixes missing/malformed tool calls with one stricter retry including
the rejected assistant attempt. A second invalid plan speaks only
“Sorry, I didn't get that — could you say it again?” Raw rejected outputs are
private mode-0600 rotating diagnostics (three files, 1 MB each), not console output.
Rook inventory supplies live names to `rook_read`, validates them before dispatch,
and expires after 60 seconds. Job failures retain their actual error message.

Backend investigation: deployed llama.cpp `common/chat.cpp`'s Gemma 4 grammar
already makes `required` non-lazy, but includes `scan-to-toolcall` with arbitrary
text before the required call. Generation can exhaust `max_tokens` there. The
backend rejects custom grammar combined with tools; `response_format` selects
JSON content rather than the native tool-call path. No backend flags or model
services were changed. The hotfix retains native tool calls and validates all
plans locally, including reply-only restrictions and function arguments.

Thinking is now strictly opt-in. False/absent/invalid/legacy opt-in creates no
shadow object, engine request or feedback write. Metadata/feedback initialization
is lazy at the first opt-in hello. The background task starts after dispatch and
waits for the foreground turn (including assistant_done and history writes) to
finish before inference and batched telemetry writes. Late decision events retain
their original turn. This intentionally trades earlier thinking display for no
same-turn GPU or SQLite contention; rapid subsequent turns or other clients can
still overlap already-running inference.

Validation before deployment:

- Both session `6dfecb73…` cutoffs (86 user, 89 tool-result follow-up) passed three
  live replays each. Every initial prose plan was rejected; every retry returned
  a valid `respond` call. No tools were executed. Replay reconstructs historical
  job snapshots from cutoff events rather than using future outcomes.
- Replay command on kaiju, with service env supplied privately:
  `python -m services.voice.hotfix_smoke replay --output replay.json`.
- Unit/integration coverage includes repeated missing/malformed calls, safe
  fallback, reply-only tool restrictions, cached worker validation, readable job
  errors, no non-opt-in requests/writes, and inference/persistence deferred until
  assistant completion. Runtime test results and final release follow below.
- A loopback-only text candidate used the actual server/planner/shadow paths but
  omitted STT/TTS model initialization. Production remained on 84f2e66. Seven
  fresh conversations per mode used the same arithmetic prompt and speak:false;
  the first sample in each mode was excluded from the warm median.

| Preflight warm completion | 84f2e66 | Candidate | Delta | Limit |
| --- | ---: | ---: | ---: | ---: |
| thinking:false | 250.893 ms | 219.045 ms | -31.848 ms | +5 ms |
| thinking:true | 253.106 ms | 216.571 ms | -36.535 ms | +10 ms |

The first preliminary cold engine request timed out at 150 ms; later warm
requests succeeded. The smoke permits a cold error event only on sample zero,
requires success for all warm opt-in samples and records every status. This does
not alter the 150 ms deadline. Deployment must also verify a successful opt-in
request. Synthetic latency samples are not an acoustic or multi-client benchmark.

Deployment procedure: commit the candidate, archive services/voice into a new
commit-named release, preserve the existing model/TLS/DB paths in the copied
90-rook-voice.conf, and add loopback DECISION_URL plus the 150 ms timeout. Remeasure
84f2e66 immediately before switching. Arm a dedicated transient systemd rollback
timer before installing the effective drop-in and restarting voice-agent once.
Verify health, both latency gates, opt-in/out events, persisted opt-in rows and
unchanged protected drop-in digests, then disarm only that new rollback timer.
If the combined candidate fails preflight, ship A with DECISION_URL unset instead.

One-command rollback remains the command near the top of this document. The
original 84f2e66 release is retained. Final live measurements and timer details
are appended after deployment verification.


### Combined production gate failed; A-only fallback selected

Release `a004b40` passed 43 tests on kaiju and six live replays, then was tried with
an eight-minute `voice-hotfix-rollback-a004b40.timer` armed. Health returned in
3.31 seconds. All opt-in test turns received successful decisions, but live warm
completion was **267.033 / 268.727 ms** (thinking false/true), compared with the
immediate baseline **250.691 / 251.931 ms**. Deltas +16.341 / +16.796 ms failed
both gates. The script immediately restored 84f2e66 and verified health, then
stopped its timer. The text-only preflight was insufficient to predict the full
service's latency; its success did not override the live failure.

Following the requested fallback, the next release ships A with `DECISION_URL=`
explicitly empty. The B implementation remains available on the review branch,
but production will make no decision calls or feedback writes, including when a
client opts in. A further hotfix reuses the planner's HTTP client across turns
instead of rebuilding its connection and TLS configuration for every request.

Full-model loopback preflight (actual STT/TTS initialization, isolated DB) passed:

| A-only warm completion | Fresh 84f2e66 | Candidate | Delta |
| --- | ---: | ---: | ---: |
| thinking:false | 249.618 ms | 212.368 ms | -37.251 ms |
| thinking:true (shadow disabled) | 251.450 ms | 211.861 ms | -39.589 ms |

All 44 voice tests passed locally. The fallback deployment reruns them with
kaiju's Python and repeats both stored-state replays, followed by a fresh
immediate baseline, dedicated rollback timer, and the live latency/health gates.
There have already been one candidate restart and one necessary rollback restart;
finishing the explicitly requested A-only fallback requires a third restart in
this task. This is an exception to the requested one-restart budget, recorded
rather than describing the failed rollout as a successful single restart.
