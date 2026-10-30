#!/usr/bin/env bash
# Remove the 0.5 sidecar: stop and delete its three units, then move
# ~/.rook-castling aside (to ~/.rook-castling.removed-<time>) so its keys,
# journal and records can still be looked at. --purge deletes it instead.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
"$here/stop.sh" >/dev/null
for u in "${UNITS[@]}"; do
    systemctl --user disable "$u" 2>/dev/null || true
    rm -f "$UNIT_DIR/$u.service"
done
systemctl --user daemon-reload
if [ -d "$ROOT" ]; then
    if [ "${1:-}" = "--purge" ]; then
        rm -rf "$ROOT"; say "removed $ROOT"
    else
        dest="$ROOT.removed-$(date +%Y%m%d-%H%M%S)"
        mv "$ROOT" "$dest"; say "moved $ROOT to $dest (delete it when you're sure)"
    fi
fi
say "sidecar removed; the 0.4 install was not touched"
