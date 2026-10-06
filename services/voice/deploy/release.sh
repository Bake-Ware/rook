#!/usr/bin/env bash
# Build, try and switch voice-service releases on the voice host.
#
#   release.sh build <ref> [tag]         archive services/voice (+ voice tests) at <ref> into releases/<tag>;
#                                        <ref> is a tag, a commit SHA or a branch on origin (never a local branch)
#   release.sh test <tag>                run the release's voice tests with a scratch HOME
#   release.sh candidate <tag> [port]    run <tag> beside the live service on a spare loopback port
#   release.sh smoke [port]              run services/voice/smoke.py against the candidate
#   release.sh candidate-stop            stop the candidate and delete its scratch state
#   release.sh activate <tag>            select <tag> (via voice-select), restart, health-check
#   release.sh rollback                  restore the previous selection, restart, health-check
#   release.sh status | list
#   release.sh validate-tag <tag> | resolve <tag>   check a tag / print its release directory
#
# Configuration (environment; defaults in brackets):
#   VOICE_HOME     directory holding releases/ [$HOME/voice-agent]
#   VOICE_REPO     git clone of the rook repository used by build [$VOICE_HOME/rook]
#   VOICE_UNIT     systemd unit [voice-agent.service]
#   VOICE_SELECT   root helper run with sudo -n [/usr/local/sbin/voice-select]
#   VOICE_PYTHON   interpreter for test/candidate [the unit's ExecStart python]
#   VOICE_HEALTH_STABLE   seconds the unit must stay healthy after a switch [10]
#   VOICE_HEALTH_TIMEOUT  seconds to wait for that [120]
#   VOICE_CANDIDATE_RUNNER  systemd (systemd-run --user) or pid [systemd when a user manager is reachable]
#   CANDIDATE_LIVE_DEVICES=1  let the candidate use the live unit's STT/TTS devices (default: CPU for both)
#
# The only privileged operations are `sudo -n $VOICE_SELECT activate <tag>|rollback`
# and, when the unit's /proc entries are not readable by this user,
# `sudo -n $VOICE_SELECT environ|cwd`. See deploy/README.md.
set -euo pipefail

VOICE_HOME=${VOICE_HOME:-$HOME/voice-agent}
VOICE_REPO=${VOICE_REPO:-$VOICE_HOME/rook}
VOICE_UNIT=${VOICE_UNIT:-voice-agent.service}
VOICE_SELECT=${VOICE_SELECT:-/usr/local/sbin/voice-select}
SELECT_CONF=/etc/voice-select.conf
RELEASES=$VOICE_HOME/releases
CANDIDATE_DIR=$RELEASES/.candidate
CANDIDATE_UNIT=voice-candidate
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

die() { echo "release.sh: $*" >&2; exit 1; }
valid_tag() { [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid release tag: ${1:-(empty)}"; }
unit_python() {
  if [ -n "${VOICE_PYTHON:-}" ]; then echo "$VOICE_PYTHON"; return; fi
  systemctl show -p ExecStart --value "$VOICE_UNIT" | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1
}
conf_value() { [ -r "$SELECT_CONF" ] && sed -n "s/^$1=//p" "$SELECT_CONF" | tail -1 || true; }
dropin_path() { echo "/etc/systemd/system/$VOICE_UNIT.d/$(conf_value DROPIN | grep . || echo zzzzzzzzzz-release.conf)"; }

# A release is a directory directly named by a valid tag whose real path is
# inside the real releases directory (no symlink or .. escapes).
release_dir() {
  valid_tag "${1:-}"
  local base dir
  base=$(realpath -e -- "$RELEASES") || die "no releases directory at $RELEASES"
  dir=$(realpath -e -- "$base/$1") || die "no release $1"
  case $dir in "$base"/*) ;; *) die "release $1 resolves outside $base" ;; esac
  [ -f "$dir/services/voice/server.py" ] || die "no voice release at $dir"
  echo "$dir"
}

main_pid() { systemctl show -p MainPID --value "$VOICE_UNIT"; }
# The live unit's environment and working directory: read directly when this
# user may, otherwise through the helper (which reads only this unit's).
unit_environ() {
  local pid; pid=$(main_pid); [ "${pid:-0}" != 0 ] || return 1
  cat "/proc/$pid/environ" 2>/dev/null || sudo -n "$VOICE_SELECT" environ
}
unit_cwd() {
  local pid; pid=$(main_pid); [ "${pid:-0}" != 0 ] || return 1
  readlink "/proc/$pid/cwd" 2>/dev/null || sudo -n "$VOICE_SELECT" cwd
}

# Health URL from the live process environment: bind address, port and scheme as served.
health_url() {
  local env bind port scheme=http host
  env=$(unit_environ | tr '\0' '\n') || return 1
  bind=$(sed -n 's/^VOICE_BIND=//p' <<<"$env" | tail -1); bind=${bind:-127.0.0.1}
  port=$(sed -n 's/^VOICE_PORT=//p' <<<"$env" | tail -1); port=${port:-8900}
  grep -q '^VOICE_TLS_CERT=.' <<<"$env" && scheme=https
  case $bind in
    0.0.0.0) host=127.0.0.1 ;;
    ::|'[::]') host='[::1]' ;;
    \[*) host=$bind ;;
    *:*) host="[$bind]" ;;
    *) host=$bind ;;
  esac
  echo "$scheme://$host:$port/health"
}
health() { local url; url=$(health_url) && curl -fsk --max-time 5 "$url"; }

# Healthy for VOICE_HEALTH_STABLE consecutive seconds on one MainPID, with no
# automatic restart since the switch, running in the expected directory.
wait_health() {  # <expected real working directory>
  local expected=$1 need=${VOICE_HEALTH_STABLE:-10} deadline=$((SECONDS + ${VOICE_HEALTH_TIMEOUT:-120}))
  local base restarts pid seen=0 since=0 out cwd
  base=$(systemctl show -p NRestarts --value "$VOICE_UNIT")
  while [ $SECONDS -lt $deadline ]; do
    restarts=$(systemctl show -p NRestarts --value "$VOICE_UNIT")
    [ "$restarts" = "$base" ] || { echo "health: $VOICE_UNIT restarted on its own (NRestarts $base -> $restarts)" >&2; return 1; }
    pid=$(main_pid)
    if [ "${pid:-0}" != 0 ] && out=$(health 2>/dev/null); then
      [ "$pid" = "$seen" ] || { seen=$pid; since=$SECONDS; }
      if [ $((SECONDS - since)) -ge "$need" ]; then
        cwd=$(unit_cwd) || { echo "health: cannot read the unit's working directory" >&2; return 1; }
        [ "$cwd" = "$expected" ] || { echo "health: unit runs in $cwd, expected $expected" >&2; return 1; }
        echo "health: $out (stable ${need}s, pid $pid, cwd $cwd)"; return 0
      fi
    else
      seen=0
    fi
    sleep 1
  done
  echo "health: not healthy for ${need}s within ${VOICE_HEALTH_TIMEOUT:-120}s" >&2; return 1
}

cmd_build() {
  local ref=${1:-} tag=${2:-} sha dir
  [ -n "$ref" ] || die "usage: build <tag|sha|origin-branch> [tag]"
  [[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid ref: $ref"
  [ -z "$tag" ] || valid_tag "$tag"
  git -C "$VOICE_REPO" fetch -q --tags origin || die "git fetch failed; refusing to build from a stale clone"
  if [[ $ref =~ ^[0-9a-f]{7,40}$ ]] && sha=$(git -C "$VOICE_REPO" rev-parse -q --verify "$ref^{commit}"); then :
  elif sha=$(git -C "$VOICE_REPO" rev-parse -q --verify "refs/tags/$ref^{commit}"); then :
  elif sha=$(git -C "$VOICE_REPO" rev-parse -q --verify "refs/remotes/origin/${ref#origin/}^{commit}"); then :
  else die "unknown ref $ref (use a tag, a commit SHA or a branch on origin)"; fi
  echo "resolved $ref -> $sha"
  tag=${tag:-${sha:0:7}}; valid_tag "$tag"
  mkdir -p "$RELEASES"
  dir=$RELEASES/$tag; [ ! -e "$dir" ] && [ ! -L "$dir" ] || die "$dir already exists"
  BUILD_TMP=$(mktemp -d "$RELEASES/.build-XXXXXX"); local tmp=$BUILD_TMP
  trap 'rm -rf "$BUILD_TMP"' EXIT
  local paths=(services/voice); git -C "$VOICE_REPO" cat-file -e "$sha:tests" 2>/dev/null && \
    mapfile -t -O 1 paths < <(git -C "$VOICE_REPO" ls-tree --name-only "$sha" tests/ | grep -E '^tests/test_voice_.*\.py$')
  git -C "$VOICE_REPO" archive "$sha" "${paths[@]}" | tar -x -C "$tmp"
  printf 'commit %s\nref %s\nbuilt %s\n' "$sha" "$ref" "$(date -Is)" > "$tmp/REVISION"
  (cd "$tmp" && find services $([ -d tests ] && echo tests) -type f | LC_ALL=C sort | xargs sha256sum > SHA256SUMS)
  "$(unit_python)" -m compileall -q "$tmp/services/voice" >/dev/null || die "release does not compile"
  mv "$tmp" "$dir"; trap - EXIT
  echo "built $dir from $sha"
}

cmd_test() {
  local dir; dir=$(release_dir "${1:-}")
  local home; home=$(mktemp -d)
  local rc=0
  (cd "$dir" && HOME=$home PYTHONPATH=. "$(unit_python)" -m pytest -q -p no:cacheprovider tests) || rc=$?
  rm -rf "$home"; return $rc
}

candidate_runner() {
  case ${VOICE_CANDIDATE_RUNNER:-} in
    systemd|pid) echo "$VOICE_CANDIDATE_RUNNER"; return ;;
    '') ;;
    *) die "VOICE_CANDIDATE_RUNNER must be systemd or pid" ;;
  esac
  if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then echo systemd; else echo pid; fi
}

proc_start() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20; }

# The recorded candidate pid, only if it is still the very process we started
# (same start time) and still the candidate (its command line and directory).
candidate_pid() {
  local pid start cmd cwd release
  pid=$(cat "$CANDIDATE_DIR/pid" 2>/dev/null) && [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
  start=$(cat "$CANDIDATE_DIR/pid-start" 2>/dev/null) && [ -n "$start" ] || return 1
  [ "$(proc_start "$pid")" = "$start" ] || return 1
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || return 1
  release=$(cat "$CANDIDATE_DIR/release")
  case $cmd in
    *"$HERE/candidate.py"*) ;;
    *services.voice.server*) cwd=$(readlink "/proc/$pid/cwd") && [ "$cwd" = "$release" ] || return 1 ;;
    *) return 1 ;;
  esac
  echo "$pid"
}

candidate_alive() {
  if [ "$(cat "$CANDIDATE_DIR/runner")" = systemd ]; then
    systemctl --user is-active --quiet "$CANDIDATE_UNIT"
  else
    candidate_pid >/dev/null
  fi
}

cmd_candidate() {
  local dir; dir=$(release_dir "${1:-}")
  local port=${2:-8931} runner python
  [[ $port =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || die "invalid port: $port"
  [ ! -e "$CANDIDATE_DIR" ] || die "a candidate already exists (candidate-stop first)"
  runner=$(candidate_runner)
  if [ "$runner" = systemd ] && systemctl --user is-active --quiet "$CANDIDATE_UNIT"; then
    die "$CANDIDATE_UNIT.service is already running (systemctl --user stop $CANDIDATE_UNIT)"
  fi
  mkdir -m 700 "$CANDIDATE_DIR"
  echo "$dir" > "$CANDIDATE_DIR/release"; echo "$port" > "$CANDIDATE_DIR/port"; echo "$runner" > "$CANDIDATE_DIR/runner"
  python=$(command -v python3)
  local args=("$python" "$HERE/candidate.py" --release "$dir" --port "$port" --scratch "$CANDIDATE_DIR/state"
              --unit "$VOICE_UNIT" --python "$(unit_python)" --select "$VOICE_SELECT")
  [ "${CANDIDATE_LIVE_DEVICES:-}" = 1 ] && args+=(--live-devices)
  if [ "$runner" = systemd ]; then
    systemd-run --user --quiet --collect --unit "$CANDIDATE_UNIT" \
      -p "StandardOutput=append:$CANDIDATE_DIR/log" -p "StandardError=append:$CANDIDATE_DIR/log" -- "${args[@]}"
  else
    setsid nohup "${args[@]}" > "$CANDIDATE_DIR/log" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$CANDIDATE_DIR/pid"; proc_start "$pid" > "$CANDIDATE_DIR/pid-start"
  fi
  for _ in $(seq 1 90); do
    if curl -fsk --max-time 3 "https://127.0.0.1:$port/health" 2>/dev/null || curl -fs --max-time 3 "http://127.0.0.1:$port/health" 2>/dev/null; then
      echo; echo "candidate $1 healthy on 127.0.0.1:$port ($runner; log $CANDIDATE_DIR/log)"; return 0
    fi
    candidate_alive || { tail -20 "$CANDIDATE_DIR/log"; cmd_candidate_stop; die "candidate exited"; }
    sleep 2
  done
  tail -20 "$CANDIDATE_DIR/log"; cmd_candidate_stop; die "candidate never became healthy"
}

cmd_smoke() {
  [ -f "$CANDIDATE_DIR/release" ] || die "no candidate running"
  local dir; dir=$(release_dir "$(basename "$(cat "$CANDIDATE_DIR/release")")")
  local port=${1:-$(cat "$CANDIDATE_DIR/port")} scheme=ws
  curl -fsk --max-time 3 "https://127.0.0.1:$port/health" >/dev/null 2>&1 && scheme=wss
  (cd "$dir" && VOICE_SMOKE_URL="$scheme://127.0.0.1:$port/ws" VOICE_SMOKE_INSECURE=1 \
     VOICE_TOKEN="$(cat "$CANDIDATE_DIR/state/smoke-token")" PYTHONPATH=. "$(unit_python)" -m services.voice.smoke)
}

cmd_candidate_stop() {
  [ -d "$CANDIDATE_DIR" ] || { echo "no candidate"; return 0; }
  if [ "$(cat "$CANDIDATE_DIR/runner" 2>/dev/null)" = systemd ]; then
    systemctl --user stop "$CANDIDATE_UNIT" 2>/dev/null || true
  elif pid=$(candidate_pid); then
    # setsid made the candidate its own process group leader.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do candidate_pid >/dev/null || break; sleep 1; done
    if candidate_pid >/dev/null; then kill -KILL -- "-$pid" 2>/dev/null || true; fi
  else
    echo "recorded candidate pid is gone or no longer the candidate; nothing killed"
  fi
  rm -rf "$CANDIDATE_DIR"; echo "candidate stopped"
}

check_helper_releases() {
  local configured; configured=$(conf_value RELEASES)
  [ -n "$configured" ] || die "$SELECT_CONF has no RELEASES (install voice-select first; see deploy/README.md)"
  [ "$(realpath -m -- "$configured")" = "$(realpath -m -- "$RELEASES")" ] || \
    die "$SELECT_CONF selects from $configured, not $RELEASES"
}

cmd_activate() {
  local dir; dir=$(release_dir "${1:-}")
  check_helper_releases
  sudo -n "$VOICE_SELECT" activate "$1"
  if ! wait_health "$dir"; then
    echo "health check failed; rolling back" >&2; cmd_rollback; return 1
  fi
  echo "active release: $dir"
}

cmd_rollback() {
  sudo -n "$VOICE_SELECT" rollback
  local expected; expected=$(systemctl show -p WorkingDirectory --value "$VOICE_UNIT")
  wait_health "$(realpath -m -- "${expected:-/}")"
}

cmd_status() {
  local dir; dir=$(systemctl show -p WorkingDirectory --value "$VOICE_UNIT")
  echo "unit: $VOICE_UNIT  active: $(systemctl is-active "$VOICE_UNIT" || true)"
  echo "working directory: $dir"; [ -f "$dir/REVISION" ] && sed 's/^/  /' "$dir/REVISION"
  local dropin; dropin=$(dropin_path)
  [ -f "$dropin" ] && echo "selector: $dropin" || echo "selector: (none; older drop-ins select the release)"
  health && echo || echo "health: FAILED"
  [ -d "$CANDIDATE_DIR" ] && echo "candidate: $(cat "$CANDIDATE_DIR/release" 2>/dev/null) port $(cat "$CANDIDATE_DIR/port" 2>/dev/null) ($(cat "$CANDIDATE_DIR/runner" 2>/dev/null))" || true
}

case ${1:-} in
  build) shift; cmd_build "$@" ;;
  test) shift; cmd_test "$@" ;;
  candidate) shift; cmd_candidate "$@" ;;
  smoke) shift; cmd_smoke "$@" ;;
  candidate-stop) cmd_candidate_stop ;;
  activate) shift; cmd_activate "$@" ;;
  rollback) cmd_rollback ;;
  status) cmd_status ;;
  list) ls -1 "$RELEASES" ;;
  validate-tag) shift; valid_tag "${1:-}"; echo "ok" ;;
  resolve) shift; release_dir "${1:-}" ;;
  *) sed -n '2,29p' "$0"; exit 2 ;;
esac
