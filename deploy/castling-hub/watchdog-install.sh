#!/usr/bin/env bash
# Install the castling watchdog (watchdog.py beside this file) as a systemd
# timer, once a minute. Run as root on the machine that watches.
#
#   On the hub's machine (bakenetus):
#     watchdog-install.sh --url http://127.0.0.1:8750 --services rook05-hub,telesthete-hub,cloudflared \
#         --chat <telegram chat id> --token-vault plugin.telegram.token [--enable]
#   On another machine (sojourn), reusing 0.4's watchdog env file for the bot:
#     watchdog-install.sh --url https://castling.bakeforge.com --token-env /etc/rook-watchdog.env [--enable]
#
# --token-vault reads the bot token from the castling hub's vault (as its user,
# rook05); --token-env copies ROOK_WATCHDOG_TELEGRAM_TOKEN/_CHAT from an env
# file. The token is never printed. Without --enable the timer is installed
# but not started: `systemctl enable --now castling-watchdog.timer` starts it,
# `systemctl disable --now castling-watchdog.timer` stops it (the rollback).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
URL= SERVICES= CHAT= VAULT= FROM= ENABLE=0
while [ $# -gt 0 ]; do
  case $1 in
    --url) URL=$2; shift 2;; --services) SERVICES=$2; shift 2;; --chat) CHAT=$2; shift 2;;
    --token-vault) VAULT=$2; shift 2;; --token-env) FROM=$2; shift 2;; --enable) ENABLE=1; shift;;
    *) sed -n '2,17p' "$0"; exit 2;;
  esac
done
[ -n "$URL" ] || { echo "--url is required" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }

install -d -m 0755 /usr/local/lib/castling-watchdog
install -m 0644 "$HERE/watchdog.py" /usr/local/lib/castling-watchdog/watchdog.py
install -d -m 0700 /var/lib/castling-watchdog

ENVF=/etc/castling-watchdog.env
umask 077
{
  echo "ROOK_WATCHDOG_URL=$URL"
  if [ -n "$SERVICES" ]; then echo "ROOK_WATCHDOG_SERVICES=$SERVICES"; fi
  if [ -n "$FROM" ]; then
    grep -E '^ROOK_WATCHDOG_TELEGRAM_(TOKEN|CHAT)=' "$FROM"
  fi
  if [ -n "$VAULT" ]; then
    printf 'ROOK_WATCHDOG_TELEGRAM_TOKEN='
    sudo -u rook05 /opt/rook05/venv/bin/python -c 'import sys; from rook.core2.hub.vault import Vault
print(Vault("/var/lib/rook05/data").unlock().get(sys.argv[1]))' "$VAULT"
  fi
  if [ -n "$CHAT" ]; then echo "ROOK_WATCHDOG_TELEGRAM_CHAT=$CHAT"; fi
} > "$ENVF.new"
grep -q '^ROOK_WATCHDOG_TELEGRAM_TOKEN=.' "$ENVF.new" || { rm -f "$ENVF.new"; echo "no bot token: give --token-vault or --token-env" >&2; exit 1; }
grep -q '^ROOK_WATCHDOG_TELEGRAM_CHAT=.' "$ENVF.new" || { rm -f "$ENVF.new"; echo "no chat id: give --chat or --token-env" >&2; exit 1; }
mv "$ENVF.new" "$ENVF"

cat > /etc/systemd/system/castling-watchdog.service <<EOF
[Unit]
Description=Castling hub watchdog (alerts on Telegram when the 0.5 hub is down)
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENVF
ExecStart=/usr/bin/python3 /usr/local/lib/castling-watchdog/watchdog.py --state /var/lib/castling-watchdog/state.json
TimeoutStartSec=90
Nice=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/castling-watchdog
PrivateTmp=yes
EOF
cat > /etc/systemd/system/castling-watchdog.timer <<'EOF'
[Unit]
Description=Run the castling watchdog every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
if [ "$ENABLE" = 1 ]; then systemctl enable --now castling-watchdog.timer; fi
echo "installed; timer: $(systemctl is-enabled castling-watchdog.timer 2>/dev/null || true)/$(systemctl is-active castling-watchdog.timer 2>/dev/null || true)"
