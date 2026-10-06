# Deploying the voice service

The voice host runs one systemd unit (default `voice-agent.service`) whose
`WorkingDirectory` is a release directory built from a tagged commit of this
repository. Releases are never edited in place: a fix is a new commit, a new
release, a candidate smoke test, then a switch. Rolling back is a switch to the
previous release.

`release.sh` does each step. Run it on the voice host as the service user (it
uses `sudo -n` only to read the service's process environment and to write one
drop-in, reload systemd and restart the unit).

Configuration is environment variables; nothing site-specific is in the script:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_HOME` | `$HOME/voice-agent` | holds `releases/` and the model files |
| `VOICE_REPO` | `$VOICE_HOME/rook` | git clone of this repository |
| `VOICE_UNIT` | `voice-agent.service` | systemd unit |
| `VOICE_DROPIN` | `zzzzzzzzzz-release.conf` | selector drop-in; must sort after every other drop-in |
| `VOICE_PYTHON` | the unit's `ExecStart` interpreter | for tests and the candidate |
| `CANDIDATE_CPU_STT` | unset | `1` runs the candidate's Whisper on CPU |

## 1. Build a release

```sh
services/voice/deploy/release.sh build origin/master        # tag = short commit
services/voice/deploy/release.sh build v2026.10.06 voice-2026-10-06
services/voice/deploy/release.sh test <tag>
```

`build` uses `git archive` of `services/voice` and the `tests/test_voice_*.py`
files at that commit into `releases/<tag>/`, writes `REVISION` (commit, ref,
time) and `SHA256SUMS`, and byte-compiles it with the service interpreter. It
refuses to overwrite an existing release. `test` runs the release's voice tests
with a scratch `HOME`. Install new Python requirements into the service venv
first if `requirements.txt` changed.

## 2. Smoke-test a candidate beside the live service

```sh
CANDIDATE_CPU_STT=1 services/voice/deploy/release.sh candidate <tag> 8931
services/voice/deploy/release.sh smoke
services/voice/deploy/release.sh candidate-stop
```

`candidate` starts the release as a separate process on a spare loopback port
(check it is free; 8911–8999 are typical). It copies the live unit's exact
environment from the running process (so every drop-in and EnvironmentFile is
already applied) and changes only what keeps it from touching live state:

* `VOICE_BIND=127.0.0.1`, `VOICE_PORT=<port>`;
* `VOICE_MODEL_DIR` is a private scratch directory under
  `releases/.candidate/state` with symlinks to the live model files and the
  live `static/` client, so `voice.state`, rejected-plan logs and the default
  database paths are private;
* fresh `VOICE_STATE_DB` and `VOICE_ADMIN_DB` in that scratch directory (the
  live conversation history, jobs and admin keys are never opened);
* `DECISION_URL` and `DECISION_GATE_THRESHOLD` empty;
* a temporary owner key for the smoke test, merged into a private copy of
  `VOICE_IDENTITIES_FILE` and written only to `state/smoke-token` (mode 0600);
* with `CANDIDATE_CPU_STT=1`, `WHISPER_DEVICE=cpu` so a second speech model is
  not loaded onto the live GPU.

The model server, Rook MCP endpoint and TLS certificate are shared with the live
service, as in the earlier "loopback candidate" smokes. `smoke` runs
`services/voice/smoke.py` against the candidate: reconnect memory, real TTS
framing and interruption, per-turn timing, and a read-only Rook job
(`VOICE_SMOKE_WORKER`, default `gpu-box`; set it to a real worker name).
`candidate-stop` kills the process group and deletes the scratch directory
(including the smoke key).

## 3. Switch

```sh
services/voice/deploy/release.sh activate <tag>
services/voice/deploy/release.sh status
```

`activate` records the current selection in `releases/.selection-history`,
writes one drop-in (`$VOICE_DROPIN`) containing only `[Service]
WorkingDirectory=<release>`, runs `daemon-reload`, restarts the unit and waits up
to two minutes for `/health`. If health fails it rolls back by itself.

Older drop-ins stay in place: their `Environment=`/`EnvironmentFile=` lines keep
applying, and the new drop-in only wins the `WorkingDirectory` because it sorts
last. The script refuses to activate if another drop-in would sort after it.
Retire the old selector drop-ins (the ones that only set `WorkingDirectory`)
separately, once the managed selector has run for a while; do not mix that
cleanup into a release switch.

## 4. Roll back

```sh
services/voice/deploy/release.sh rollback
```

Restores the previous entry in `releases/.selection-history` (or removes the
managed drop-in if there was none, returning to the older selector drop-ins),
reloads, restarts and health-checks. Repeat to go back further. Releases are
kept; delete old ones by hand.

## Environment the code expects

Defaults in the code are neutral (loopback endpoints, assistant name "Rook").
A host that relied on older built-in defaults must set them explicitly in an
EnvironmentFile before switching, in particular:

* `ROOK_MCP_URL` (default `http://127.0.0.1:8765/mcp`) and `ROOK_MCP_TOKEN`;
* `ROOK_VOICE_ASSISTANT_NAME` and `ROOK_VOICE_OWNER` (system prompt and tool
  descriptions; default "Rook" and "the user's");
* `VLLM_URL`/`VLLM_MODEL`, `LLM_PRIMARY_*`, `WHISPER_*`, `VOICE_TTS_THREADS`,
  `VOICE_TOOL_REASONING_EFFORT`, `VOICE_TOOL_MAX_TOKENS`;
* `VOICE_ADMIN_DB`, `VOICE_STATE_DB`, `VOICE_IDENTITIES_FILE`, TLS paths;
* `VOICE_TOKEN` empty plus `VOICE_ALLOW_ANONYMOUS=1` for keyless guest chat.

Check with `sudo cat /proc/$(systemctl show -p MainPID --value voice-agent)/environ | tr '\0' '\n' | cut -d= -f1`
(names only) before and after a switch.

Each turn logs one `voice_turn_timing` JSON line (`journalctl -u voice-agent`);
set `VOICE_TIMING_LOG=0` to silence it.
