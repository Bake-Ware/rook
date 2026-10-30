#!/usr/bin/env bash
# Stop the 0.5 sidecar: only the rook-castling-* units (never rook-band-worker).
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
systemctl --user stop rook-castling-worker rook-castling-hub rook-castling-relay 2>/dev/null || true
systemctl --user --no-pager --lines=0 status "${UNITS[@]}" 2>/dev/null | grep -E '^●|^○|Active:' || true
