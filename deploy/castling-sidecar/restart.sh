#!/usr/bin/env bash
# Restart parts of the 0.5 sidecar: restart.sh [relay] [hub] [worker] (all three
# when none is named). Only the rook-castling-* units; never rook-band-worker.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
[ -f "$HUB_CFG" ] || die "not installed; run $here/install.sh"
parts=("$@")
[ ${#parts[@]} -gt 0 ] || parts=(relay hub worker)
units=()
for p in "${parts[@]}"; do
    case $p in relay|hub|worker) units+=("rook-castling-$p") ;; *) die "no part $p (relay, hub, worker)" ;; esac
done
systemctl --user restart "${units[@]}"
case " ${parts[*]} " in *" hub "*)
    wait_http "$LOCAL_URL/health" 40 || die "the hub didn't answer on $LOCAL_URL (journalctl --user -u rook-castling-hub)" ;;
esac
systemctl --user --no-pager --lines=0 status "${units[@]}" | grep -E '^●|Active:'
