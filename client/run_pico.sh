#!/usr/bin/env bash
# Launch the Rook HID-KVM client over the soldered twin-Pico UART bridge.
# Needs root for /dev/input (evdev grab) and /dev/ttyACM* (serial write).
# Usage: sudo ./run_pico.sh                 # auto-detects the bridge Pico
#        sudo ./run_pico.sh --port /dev/ttyACM0
set -e
VENV=/home/bake/.local/share/rook-venv
exec "$VENV/bin/python" -u "$(dirname "$0")/rook_hid_pico.py" "$@"
