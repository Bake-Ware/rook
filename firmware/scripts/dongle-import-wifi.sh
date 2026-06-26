#!/usr/bin/env bash
# dongle-import-wifi.sh — push the host's current Wi-Fi SSID + PSK to a
# plugged-in R00K dongle's saved-network list. Stock tools only:
#   Linux : nmcli / iwgetid / wpa_supplicant.conf, stty, cat
#   macOS : networksetup, security, stty, cat
#
# Usage:
#   ./dongle-import-wifi.sh                       # auto port + priority 2
#   ./dongle-import-wifi.sh /dev/ttyACM0 1        # explicit port + priority
set -eu

PORT="${1:-}"
PRIO="${2:-2}"

# ---- find serial port ---------------------------------------------------
if [ -z "$PORT" ]; then
    for p in /dev/ttyACM* /dev/cu.usbmodem*; do
        [ -e "$p" ] || continue
        PORT="$p"; break
    done
fi
[ -n "$PORT" ] && [ -e "$PORT" ] || {
    echo "no /dev/ttyACM* or /dev/cu.usbmodem* found — plug the dongle in." >&2
    exit 1
}

# ---- read host SSID + PSK ----------------------------------------------
SSID=""; PSK=""
case "$(uname -s)" in
Linux)
    if command -v nmcli >/dev/null 2>&1; then
        SSID="$(nmcli -t -f active,ssid dev wifi 2>/dev/null \
                | awk -F: '$1=="yes"{print $2; exit}')"
        if [ -n "$SSID" ]; then
            SUDO=""
            [ "$(id -u)" -ne 0 ] && SUDO="sudo"
            PSK="$($SUDO nmcli -s -g 802-11-wireless-security.psk \
                   connection show "$SSID" 2>/dev/null || true)"
        fi
    fi
    if [ -z "$PSK" ] && command -v iwgetid >/dev/null 2>&1; then
        SSID="$(iwgetid -r 2>/dev/null || true)"
        if [ -n "$SSID" ] && [ -r /etc/wpa_supplicant/wpa_supplicant.conf ]; then
            PSK="$(awk -v s="\"$SSID\"" '
                /network=\{/ {b=1; ss=""; pp=""; next}
                /\}/         {if(b && ss==s) {print pp; exit}; b=0; next}
                b && /ssid=/ {ss=$0; sub(/.*ssid=/,"",ss)}
                b && /psk=/  {pp=$0; sub(/.*psk=/,"",pp); gsub(/"/,"",pp)}
            ' /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null)"
        fi
    fi
    ;;
Darwin)
    if command -v networksetup >/dev/null 2>&1; then
        SSID="$(networksetup -getairportnetwork en0 2>/dev/null \
                | sed -E 's/^[^:]+:[[:space:]]*//')"
        if [ -n "$SSID" ]; then
            # `security` writes the password into stderr; merge then parse.
            PSK="$(security find-generic-password -ga "$SSID" 2>&1 >/dev/null \
                   | sed -nE 's/^password:[[:space:]]+"(.+)"$/\1/p')"
        fi
    fi
    ;;
esac

[ -n "$SSID" ] && [ -n "$PSK" ] || {
    echo "could not read host Wi-Fi credentials." >&2
    echo "  ssid='${SSID:-(none)}'  psk_len=${#PSK}" >&2
    echo "  Linux: try sudo, or install nmcli." >&2
    echo "  macOS: keychain prompt may have been denied." >&2
    exit 2
}

echo "host wifi : ssid='$SSID'  psk=*** (${#PSK} chars)"
echo "target    : $PORT  (priority $PRIO)"

# ---- talk to dongle -----------------------------------------------------
case "$(uname -s)" in
    Darwin) stty -f "$PORT" 115200 raw -echo cs8 -cstopb -parenb ;;
    *)      stty -F "$PORT" 115200 raw -echo cs8 -cstopb -parenb ;;
esac

# Read responses in background; tear it down on exit.
( cat "$PORT" ) >/tmp/.rook-dongle-$$.out 2>/dev/null &
CAT_PID=$!
cleanup() { kill "$CAT_PID" 2>/dev/null || true; rm -f /tmp/.rook-dongle-$$.out; }
trap cleanup EXIT

sleep 0.3
printf '\r\n'                                 > "$PORT"; sleep 0.3
printf 'wifi rm %s\r\n' "$SSID"               > "$PORT"; sleep 0.4
printf 'wifi add %s %s %s\r\n' "$SSID" "$PSK" "$PRIO" > "$PORT"; sleep 0.4
printf 'wifi reconnect\r\n'                   > "$PORT"; sleep 6
printf 'status\r\n'                           > "$PORT"; sleep 1.5

echo "--- dongle reply ---"
cat /tmp/.rook-dongle-$$.out
echo "--------------------"
if grep -q "connected ssid=$SSID" /tmp/.rook-dongle-$$.out; then
    echo "OK: dongle connected to '$SSID'."
else
    echo "note: dongle did not immediately confirm. The save took; the"
    echo "background monitor task will retry within ~45 seconds."
fi
