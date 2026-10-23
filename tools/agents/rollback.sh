#!/usr/bin/env bash
# Flip ~/rook-agents/current back to ~/rook-agents/previous (and previous to
# what current was), then restart the session host yourself:
#   systemctl --user restart rook-agent-sessions
set -euo pipefail
base=${ROOK_AGENTS_HOME:-$HOME/rook-agents}
[ -L "$base/previous" ] || { echo "rollback.sh: no previous release in $base" >&2; exit 1; }
cur=$(readlink "$base/current" || true)
prev=$(readlink "$base/previous")
[ -d "$base/$prev" ] || { echo "rollback.sh: $base/$prev is gone" >&2; exit 1; }
ln -sfn "$prev" "$base/current.new"
mv -T "$base/current.new" "$base/current"
if [ -n "$cur" ]; then
  ln -sfn "$cur" "$base/previous.new"
  mv -T "$base/previous.new" "$base/previous"
fi
echo "current -> $prev (previous -> ${cur:-none})"
echo "restart with: systemctl --user restart rook-agent-sessions"
