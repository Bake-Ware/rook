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
