#!/usr/bin/env bash
# Start the 0.5 sidecar (relay, hub, worker). The live 0.4 units are not touched.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
[ -f "$HUB_CFG" ] || die "not installed; run $here/install.sh"
systemctl --user start "${UNITS[@]}"
wait_http "$LOCAL_URL/health" 40 || die "the hub didn't answer on $LOCAL_URL (journalctl --user -u rook-castling-hub)"
systemctl --user --no-pager --lines=0 status "${UNITS[@]}" | grep -E '^●|Active:'
