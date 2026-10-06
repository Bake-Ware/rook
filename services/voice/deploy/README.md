# Deploying the voice service

The voice host runs one systemd unit (default `voice-agent.service`) whose
`WorkingDirectory` is a release directory built from a tagged commit of this
repository. Releases are never edited in place: a fix is a new commit, a new
release, a candidate smoke test, then a switch. Rolling back is a switch to the
previous release.

`release.sh` does each step. Run it on the voice host as the service user.

## Privileges

`release.sh` runs `sudo` only for one root-owned helper, `voice-select`
(committed here as `services/voice/deploy/voice-select`). The exact sudo it
needs:

| Command | When |
| --- | --- |
| `sudo -n /usr/local/sbin/voice-select activate <tag>` | `activate` |
| `sudo -n /usr/local/sbin/voice-select rollback` | `rollback`, and `activate` when the health check fails |
| `sudo -n /usr/local/sbin/voice-select environ` | only if the unit's `/proc/<pid>/environ` is not readable by the service user (the unit runs as another user): the health check and `candidate` read the live bind address, port, TLS setting and environment |
| `sudo -n /usr/local/sbin/voice-select cwd` | only if `/proc/<pid>/cwd` is not readable by the service user: the post-switch working-directory check |

Nothing else. The drop-in and its directory are read without sudo, and
`systemctl show`/`is-active` need none. `environ` hands the voice unit's whole
environment (including its secrets) to the service user; when the unit already
runs as that user it is never called.

`voice-select` takes only a release tag. It checks the tag against
`^[A-Za-z0-9][A-Za-z0-9._-]*$`, resolves it with `realpath`, refuses anything
outside the configured releases directory or without `services/voice/server.py`,
refuses if another drop-in would sort after the managed one, writes the single
managed drop-in itself (`[Service]` plus `WorkingDirectory=` only), runs
`systemctl daemon-reload` and restarts the unit. `rollback` restores the
previous selection from its root-owned history (`/var/lib/voice-select/history`)
after re-checking the path. Its settings come from the root-owned
`/etc/voice-select.conf`, never from the caller.

### One-time operator setup (as root)

```sh
# 1. Install the helper, root-owned and writable by nobody else.
install -o root -g root -m 0755 services/voice/deploy/voice-select /usr/local/sbin/voice-select

# 2. Configure it (KEY=value lines; nothing is sourced). RELEASES is the
#    service user's $VOICE_HOME/releases.
cat > /etc/voice-select.conf <<'CONF'
RELEASES=/home/<service-user>/voice-agent/releases
UNIT=voice-agent.service
DROPIN=zzzzzzzzzz-release.conf
CONF
chown root:root /etc/voice-select.conf; chmod 0644 /etc/voice-select.conf

# 3. Allow the service user that one command and nothing else.
echo '<service-user> ALL=(root) NOPASSWD: /usr/local/sbin/voice-select' > /etc/sudoers.d/voice-select
chmod 0440 /etc/sudoers.d/voice-select
visudo -cf /etc/sudoers.d/voice-select
```

Re-install the helper (step 1) when `voice-select` changes in a release you
deploy, after reviewing the diff: it runs as root. Remove any older sudoers
lines that let the service user run `install`, `sed`, `rm`, `cat`, `test`, `ls`
or `systemctl` for releases. For `systemd-run --user` candidates the service user
needs a user manager (`loginctl enable-linger <service-user>`); without one,
`release.sh` falls back to a verified pid.

Configuration is environment variables; nothing site-specific is in the script:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_HOME` | `$HOME/voice-agent` | holds `releases/` and the model files |
| `VOICE_REPO` | `$VOICE_HOME/rook` | git clone of this repository |
| `VOICE_UNIT` | `voice-agent.service` | systemd unit (must match `UNIT` in `/etc/voice-select.conf`) |
| `VOICE_SELECT` | `/usr/local/sbin/voice-select` | the root helper |
| `VOICE_PYTHON` | the unit's `ExecStart` interpreter | for tests and the candidate |
| `VOICE_HEALTH_STABLE` | `10` | seconds the unit must stay healthy after a switch |
| `VOICE_HEALTH_TIMEOUT` | `120` | seconds to wait for that |
| `VOICE_CANDIDATE_RUNNER` | `systemd` when a user manager is reachable, else `pid` | how the candidate runs |
| `CANDIDATE_LIVE_DEVICES` | unset | `1` lets the candidate use the live STT/TTS devices (default: CPU for both) |

## 1. Build a release

```sh
services/voice/deploy/release.sh build master               # origin/master; tag = short commit
services/voice/deploy/release.sh build v2026.10.06 voice-2026-10-06
services/voice/deploy/release.sh test <tag>
```

`build` fetches `origin` and its tags and fails if the fetch fails, so it never
builds from a stale clone. The ref must be a tag, a commit SHA or a branch on
`origin` (`master` and `origin/master` both mean `origin/master`; local branches
are never used), and the resolved SHA is printed. Release tags must match
`^[A-Za-z0-9][A-Za-z0-9._-]*$`. It archives `services/voice` and the
`tests/test_voice_*.py` files at that commit into `releases/<tag>/`, writes
`REVISION` (commit, ref, time) and `SHA256SUMS`, and byte-compiles it with the
service interpreter. It refuses to overwrite an existing release. `test` runs the
release's voice tests with a scratch `HOME`. Install new Python requirements into
the service venv first if `requirements.txt` changed.

Every command that takes a tag (`test`, `candidate`, `activate`) validates it and
requires its `realpath` to be inside the real releases directory.
`release.sh validate-tag <tag>` and `release.sh resolve <tag>` run those checks
alone.

## 2. Smoke-test a candidate beside the live service

```sh
services/voice/deploy/release.sh candidate <tag> 8931
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
* CPU speech: `WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE=int8` and
  `ONNX_PROVIDER=CPUExecutionProvider`, so neither a second STT nor a second TTS
  model lands on the live GPU (`CANDIDATE_LIVE_DEVICES=1` keeps the live
  unit's settings).

The candidate runs as a transient user service (`systemd-run --user --unit
voice-candidate`) when the service user has a user manager, and
`candidate-stop` is then `systemctl --user stop voice-candidate`. Otherwise it
runs in its own session, and `candidate-stop` signals the recorded pid only after
checking it is still the same process (start time) and still the candidate (its
command line, and for the server its working directory is the release). A stale
pid file kills nothing.

The model server, Rook MCP endpoint and TLS certificate are shared with the live
service, as in the earlier "loopback candidate" smokes. `smoke` runs
`services/voice/smoke.py` against the candidate: reconnect memory, real TTS
framing and interruption, per-turn timing, and a read-only Rook job
(`VOICE_SMOKE_WORKER`, default `gpu-box`; set it to a real worker name).
`candidate-stop` stops the candidate and deletes the scratch directory
(including the smoke key).

## 3. Switch

```sh
services/voice/deploy/release.sh activate <tag>
services/voice/deploy/release.sh status
```

`activate` validates the tag and checks `/etc/voice-select.conf` selects from the
same releases directory, then `voice-select activate <tag>` records the current
selection, writes the one managed drop-in (only `[Service]
WorkingDirectory=<release>`), reloads and restarts the unit. `release.sh` then
probes `/health` on the unit's configured `VOICE_BIND`/`VOICE_PORT` (loopback for
a wildcard bind; https when `VOICE_TLS_CERT` is set) and requires it healthy for
10 consecutive seconds on one MainPID with `NRestarts` unchanged, then checks
`/proc/<MainPID>/cwd` is the new release. If that does not happen within two
minutes it rolls back by itself.

Older drop-ins stay in place: their `Environment=`/`EnvironmentFile=` lines keep
applying, and the new drop-in only wins the `WorkingDirectory` because it sorts
last. The helper refuses to activate if another drop-in would sort after it.
Retire the old selector drop-ins (the ones that only set `WorkingDirectory`)
separately, once the managed selector has run for a while; do not mix that
cleanup into a release switch.

## 4. Roll back

```sh
services/voice/deploy/release.sh rollback
```

Restores the previous entry in `/var/lib/voice-select/history` (or removes the
managed drop-in if there was none, returning to the older selector drop-ins),
reloads, restarts and runs the same health check against the unit's resulting
`WorkingDirectory`. A recorded directory outside the releases directory is
refused. Repeat to go back further. Releases are kept; delete old ones by hand.

## Environment the code expects

Defaults in the code are neutral (loopback endpoints, assistant name "Rook").
A host that relied on older built-in defaults must set them explicitly in an
EnvironmentFile before switching, in particular:

* `ROOK_MCP_URL` (default `http://127.0.0.1:8765/mcp`) and `ROOK_MCP_TOKEN`;
* `ROOK_VOICE_ASSISTANT_NAME` and `ROOK_VOICE_OWNER` (system prompt and tool
  descriptions; default "Rook" and "the user's");
* `VLLM_URL`/`VLLM_MODEL`, `LLM_PRIMARY_*`, `WHISPER_*`, `VOICE_TTS_THREADS`,
  `VOICE_TTS_DEFAULT`, `VOICE_CHATTERBOX_*` (see the service README, "Voices: Kokoro and Chatterbox"),
  `VOICE_TOOL_REASONING_EFFORT`, `VOICE_TOOL_MAX_TOKENS`;
* `VOICE_ADMIN_DB`, `VOICE_STATE_DB`, `VOICE_IDENTITIES_FILE`, TLS paths;
* `VOICE_TOKEN` empty plus `VOICE_ALLOW_ANONYMOUS=1` for keyless guest chat.

Check with `sudo -n /usr/local/sbin/voice-select environ | tr '\0' '\n' | cut -d= -f1`
(names only) before and after a switch.

Each turn logs one `voice_turn_timing` JSON line (`journalctl -u voice-agent`);
set `VOICE_TIMING_LOG=0` to silence it.
