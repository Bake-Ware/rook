#!/usr/bin/env bash
# Move the band's Telegram + Discord bots between the 0.4 hub (bakenetcanada)
# and castling (bakenetus), one command each way. Run it from cachyrig (or any
# machine that reaches both hubs over SSH). See castling/switchover/bots.py.
#
#   bots-switch.sh check        # read-only: castling's bot tokens work, links, where the bots run
#   bots-switch.sh to-castling  # stop the bots on 0.4, then start them on castling
#   bots-switch.sh to-04        # rollback: stop them on castling, start them on 0.4 again
#
# A token is polled by one hub at a time, so each move stops one side before
# starting the other. If the castling side fails, to-castling turns the 0.4
# bots back on by itself. 0.4's bots are stopped with an environment drop-in
# (its settings put env over the stored value: ROOK_TELEGRAM / ROOK_DISCORD =
# false), so 0.4's own settings and vault stay untouched; removing the drop-in
# is the way back. Each 0.4 or castling change restarts that hub (MCP clients
# reconnect).
#
# SSH: ROOK04_SSH (default: ssh ubuntu@151.145.63.234; its password is vault
# bakenetcanada-ubuntu-password, e.g. ROOK04_SSH="sshpass -e ssh ubuntu@151.145.63.234"
# with SSHPASS set) and CASTLING_SSH (default: ssh bakenetus).
set -euo pipefail

ROOK04_SSH=${ROOK04_SSH:-"ssh -o ConnectTimeout=20 ubuntu@151.145.63.234"}
CASTLING_SSH=${CASTLING_SSH:-"ssh -o ConnectTimeout=20 bakenetus"}
DROPIN=/etc/systemd/system/rook-band-mcp.service.d/40-castling-bots-off.conf

c05() {   # castling: run the bots module as the hub's user
  $CASTLING_SSH "cd /opt/rook05/src && sudo -u rook05 env PYTHONPATH=/opt/rook05/src /opt/rook05/venv/bin/python -m castling.switchover.bots $1 --config /var/lib/rook05/hub.yaml"
}

c05_restart() {
  $CASTLING_SSH 'sudo systemctl restart rook05-hub && for i in $(seq 1 45); do curl -sf -m 3 http://127.0.0.1:8750/health >/dev/null && echo "castling hub up" && exit 0; sleep 2; done; echo "castling hub not answering" >&2; exit 1'
}

c05_status() {   # telegram.status + discord.status through the hub's door, as the owner
  sleep 10; c05 status
}

o04() {   # 0.4: bots off (on=0) or on (on=1)
  $ROOK04_SSH "set -e
    if [ $1 = 0 ]; then
      printf '[Service]\n# castling bots switch: the bots run on castling (castling/switchover/bots-switch.sh)\nEnvironment=ROOK_TELEGRAM=false\nEnvironment=ROOK_DISCORD=false\n' | sudo tee $DROPIN >/dev/null
    else
      sudo rm -f $DROPIN
    fi
    sudo systemctl daemon-reload
    sudo systemctl restart rook-band-mcp
    for i in \$(seq 1 30); do systemctl is-active -q rook-band-mcp && break; sleep 2; done
    systemctl is-active rook-band-mcp
    echo \"0.4 bots drop-in: \$(test -f $DROPIN && echo present, bots off || echo absent, bots on)\""
}

case "${1:-}" in
  check)
    c05 check
    $ROOK04_SSH "test -f $DROPIN && echo '0.4: bots off (drop-in present)' || echo '0.4: bots on'"
    ;;
  to-castling)
    c05 check
    echo "== 0.4: bots off"; o04 0
    if ! { echo "== castling: stop, stage, on, start"
           $CASTLING_SSH 'sudo systemctl stop rook05-hub' && c05 stage && c05 on && c05_restart; }; then
      echo "== castling side failed: rolling back (castling bots off, 0.4 bots on)" >&2
      c05 off || true; c05_restart || true; o04 1
      exit 1
    fi
    c05_status
    ;;
  to-04)
    echo "== castling: bots off"; c05 off; c05_restart
    echo "== 0.4: bots on"; o04 1
    ;;
  *)
    sed -n '2,20p' "$0"; exit 2
    ;;
esac
