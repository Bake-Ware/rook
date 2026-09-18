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

Post-deploy measurements and release ID are appended after verification.

Open validation limits: no physical-phone/acoustic test in this rollout; the
correction classifier and live context distribution do not inherit the synthetic
household ECE guarantee. Outcome signals need review before training. Silence,
interruptions and repetitions can have benign explanations.
