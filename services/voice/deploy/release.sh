#!/usr/bin/env bash
# Build, try and switch voice-service releases on the voice host.
#
#   release.sh build <git-ref> [tag]     archive services/voice (+ voice tests) from the repo into releases/<tag>
#   release.sh test <tag>                run the release's voice tests with a scratch HOME
#   release.sh candidate <tag> [port]    run <tag> beside the live service on a spare loopback port
#   release.sh smoke [port]              run services/voice/smoke.py against the candidate
#   release.sh candidate-stop            stop the candidate and delete its scratch state
#   release.sh activate <tag>            point the unit at <tag> (one drop-in), restart, health-check
#   release.sh rollback                  restore the previous selection, restart, health-check
#   release.sh status | list
#
# Configuration (environment; defaults in brackets):
#   VOICE_HOME     directory holding releases/ [$HOME/voice-agent]
#   VOICE_REPO     git clone of the rook repository used by build [$VOICE_HOME/rook]
#   VOICE_UNIT     systemd unit [voice-agent.service]
#   VOICE_DROPIN   selector drop-in name; must sort after every other drop-in [zzzzzzzzzz-release.conf]
#   VOICE_PYTHON   interpreter for test/candidate [the unit's ExecStart python]
#   CANDIDATE_CPU_STT=1   run the candidate's Whisper on CPU (keeps the live GPU untouched)
#
# Only WorkingDirectory is selected by the drop-in; existing drop-ins and
# EnvironmentFiles stay as they are. Every activate/rollback records the
# previous selection in releases/.selection-history.
set -euo pipefail

VOICE_HOME=${VOICE_HOME:-$HOME/voice-agent}
VOICE_REPO=${VOICE_REPO:-$VOICE_HOME/rook}
VOICE_UNIT=${VOICE_UNIT:-voice-agent.service}
VOICE_DROPIN=${VOICE_DROPIN:-zzzzzzzzzz-release.conf}
RELEASES=$VOICE_HOME/releases
DROPIN_DIR=/etc/systemd/system/$VOICE_UNIT.d
DROPIN=$DROPIN_DIR/$VOICE_DROPIN
HISTORY=$RELEASES/.selection-history
CANDIDATE_DIR=$RELEASES/.candidate
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

die() { echo "release.sh: $*" >&2; exit 1; }
unit_python() {
  if [ -n "${VOICE_PYTHON:-}" ]; then echo "$VOICE_PYTHON"; return; fi
  systemctl show -p ExecStart --value "$VOICE_UNIT" | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1
}
active_dir() { systemctl show -p WorkingDirectory --value "$VOICE_UNIT"; }
release_dir() { [ -n "${1:-}" ] || die "missing release tag"; local d=$RELEASES/$1; [ -f "$d/services/voice/server.py" ] || die "no release at $d"; echo "$d"; }

# Health from the live process environment: port and scheme as the unit serves them.
health() {
  local pid port scheme=http
  pid=$(systemctl show -p MainPID --value "$VOICE_UNIT")
  [ "$pid" != 0 ] || return 1
  port=$(sudo -n cat "/proc/$pid/environ" | tr '\0' '\n' | sed -n 's/^VOICE_PORT=//p'); port=${port:-8900}
  sudo -n cat "/proc/$pid/environ" | tr '\0' '\n' | grep -q '^VOICE_TLS_CERT=.' && scheme=https
  curl -fsk --max-time 5 "$scheme://127.0.0.1:$port/health"
}
wait_health() {
  for _ in $(seq 1 60); do
    if out=$(health 2>/dev/null); then echo "health: $out"; return 0; fi
    sleep 2
  done
  return 1
}

check_order() {
  # The selector must be applied last or an older selector's WorkingDirectory wins.
  local last
  last=$( { sudo -n ls "$DROPIN_DIR" 2>/dev/null | grep '\.conf$' || true; echo "$VOICE_DROPIN"; } | LC_ALL=C sort | tail -1)
  [ "$last" = "$VOICE_DROPIN" ] || die "$VOICE_DROPIN would not sort last in $DROPIN_DIR (last is $last); set VOICE_DROPIN"
}

write_selection() {  # <release dir> <label>
  local tmp; tmp=$(mktemp)
  printf '# Managed by services/voice/deploy/release.sh: selects the voice release.\n# %s, %s\n[Service]\nWorkingDirectory=%s\n' \
    "$2" "$(date -Is)" "$1" > "$tmp"
  sudo -n install -m 644 "$tmp" "$DROPIN"; rm -f "$tmp"
}

restart_and_verify() {
  sudo -n systemctl daemon-reload
  sudo -n systemctl restart "$VOICE_UNIT"
  wait_health && echo "active release: $(active_dir)"
}

cmd_build() {
  local ref=${1:-}; [ -n "$ref" ] || die "usage: build <git-ref> [tag]"
  git -C "$VOICE_REPO" fetch -q origin || true
  local sha; sha=$(git -C "$VOICE_REPO" rev-parse --verify "$ref^{commit}" 2>/dev/null || git -C "$VOICE_REPO" rev-parse --verify "origin/$ref^{commit}") \
    || die "unknown ref $ref"
  local tag=${2:-${sha:0:7}} dir tmp
  dir=$RELEASES/$tag; [ ! -e "$dir" ] || die "$dir already exists"
  mkdir -p "$RELEASES"; tmp=$(mktemp -d "$RELEASES/.build-XXXXXX")
  local paths=(services/voice); git -C "$VOICE_REPO" cat-file -e "$sha:tests" 2>/dev/null && \
    mapfile -t -O 1 paths < <(git -C "$VOICE_REPO" ls-tree --name-only "$sha" tests/ | grep -E '^tests/test_voice_.*\.py$')
  git -C "$VOICE_REPO" archive "$sha" "${paths[@]}" | tar -x -C "$tmp"
  printf 'commit %s\nref %s\nbuilt %s\n' "$sha" "$ref" "$(date -Is)" > "$tmp/REVISION"
  (cd "$tmp" && find services $([ -d tests ] && echo tests) -type f | LC_ALL=C sort | xargs sha256sum > SHA256SUMS)
  "$(unit_python)" -m compileall -q "$tmp/services/voice" >/dev/null || { rm -rf "$tmp"; die "release does not compile"; }
  mv "$tmp" "$dir"; echo "built $dir from $sha"
}

cmd_test() {
  local dir; dir=$(release_dir "${1:-}")
  local home; home=$(mktemp -d)
  local rc=0
  (cd "$dir" && HOME=$home PYTHONPATH=. "$(unit_python)" -m pytest -q -p no:cacheprovider tests) || rc=$?
  rm -rf "$home"; return $rc
}

cmd_candidate() {
  local dir; dir=$(release_dir "${1:-}")
  local port=${2:-8931}
  [ ! -f "$CANDIDATE_DIR/pid" ] || die "a candidate is already running (candidate-stop first)"
  mkdir -p "$CANDIDATE_DIR"; chmod 700 "$CANDIDATE_DIR"
  local extra=(); [ "${CANDIDATE_CPU_STT:-}" = 1 ] && extra+=(--cpu-stt)
  setsid nohup python3 "$HERE/candidate.py" --release "$dir" --port "$port" --scratch "$CANDIDATE_DIR/state" \
    --unit "$VOICE_UNIT" --python "$(unit_python)" "${extra[@]}" > "$CANDIDATE_DIR/log" 2>&1 < /dev/null &
  echo $! > "$CANDIDATE_DIR/pid"; echo "$port" > "$CANDIDATE_DIR/port"
  for _ in $(seq 1 90); do
    if curl -fsk --max-time 3 "https://127.0.0.1:$port/health" 2>/dev/null || curl -fs --max-time 3 "http://127.0.0.1:$port/health" 2>/dev/null; then
      echo; echo "candidate $1 healthy on 127.0.0.1:$port (log $CANDIDATE_DIR/log)"; return 0
    fi
    kill -0 "$(cat "$CANDIDATE_DIR/pid")" 2>/dev/null || { tail -20 "$CANDIDATE_DIR/log"; cmd_candidate_stop; die "candidate exited"; }
    sleep 2
  done
  tail -20 "$CANDIDATE_DIR/log"; cmd_candidate_stop; die "candidate never became healthy"
}

cmd_smoke() {
  local port=${1:-$(cat "$CANDIDATE_DIR/port" 2>/dev/null || echo 8931)} scheme=ws
  curl -fsk --max-time 3 "https://127.0.0.1:$port/health" >/dev/null 2>&1 && scheme=wss
  local dir; dir=$(sed -n 's/^candidate: release=\([^ ]*\) .*/\1/p' "$CANDIDATE_DIR/log" 2>/dev/null | head -1)
  [ -n "$dir" ] || die "no candidate running"
  (cd "$dir" && VOICE_SMOKE_URL="$scheme://127.0.0.1:$port/ws" VOICE_SMOKE_INSECURE=1 \
     VOICE_TOKEN="$(cat "$CANDIDATE_DIR/state/smoke-token")" PYTHONPATH=. "$(unit_python)" -m services.voice.smoke)
}

cmd_candidate_stop() {
  if [ -f "$CANDIDATE_DIR/pid" ]; then
    local pid; pid=$(cat "$CANDIDATE_DIR/pid")
    pkill -TERM -s "$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -rf "$CANDIDATE_DIR"; echo "candidate stopped"
}

cmd_activate() {
  local dir; dir=$(release_dir "${1:-}")
  check_order
  local previous
  if sudo -n test -f "$DROPIN"; then previous="dir $(sudo -n sed -n 's/^WorkingDirectory=//p' "$DROPIN")"; else previous=absent; fi
  echo "$previous" >> "$HISTORY"
  write_selection "$dir" "release $1"
  if ! restart_and_verify; then
    echo "health check failed; rolling back" >&2; cmd_rollback; return 1
  fi
}

cmd_rollback() {
  [ -s "$HISTORY" ] || die "no previous selection recorded"
  local previous; previous=$(tail -1 "$HISTORY")
  sed -i '$d' "$HISTORY"
  if [ "$previous" = absent ]; then
    sudo -n rm -f "$DROPIN"; echo "removed $DROPIN (back to the older selector drop-ins)"
  else
    write_selection "${previous#dir }" "rollback"
  fi
  restart_and_verify
}

cmd_status() {
  local dir; dir=$(active_dir); echo "unit: $VOICE_UNIT  active: $(systemctl is-active "$VOICE_UNIT")"
  echo "working directory: $dir"; [ -f "$dir/REVISION" ] && sed 's/^/  /' "$dir/REVISION"
  sudo -n test -f "$DROPIN" && echo "selector: $DROPIN" || echo "selector: (none; older drop-ins select the release)"
  health && echo || echo "health: FAILED"
  [ -f "$CANDIDATE_DIR/pid" ] && echo "candidate: pid $(cat "$CANDIDATE_DIR/pid") port $(cat "$CANDIDATE_DIR/port")" || true
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
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
