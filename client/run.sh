#!/usr/bin/env bash
# Launch the Rook HID-KVM source client. Needs root for /dev/input.
# Usage: sudo ./run.sh   (or it will re-exec under sudo)
set -e
VENV=/home/bake/.local/share/rook-venv
HOST="${ROOK_HOST:-192.168.1.238}"
export ROOK_BAND_PSK="${ROOK_BAND_PSK:-rook-bakenet-default-2026}"
exec "$VENV/bin/python" -u "$(dirname "$0")/rook_hid_kvm.py" --host "$HOST" "$@"
