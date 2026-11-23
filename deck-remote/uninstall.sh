#!/bin/sh
# Remove the Rook remote app and plugin (and, with --data, its profiles and settings).
# Take the plugin line out of /home/deck/.rook05/worker.yaml yourself and restart
# the worker (systemctl --user restart rook05-worker); remove the Steam shortcut in Steam.
set -eu
DEST=${ROOKMOTE_HOME:-$HOME/.local/share/rookmote}
rm -rf "$DEST/app" "$DEST/plugin" "$DEST/venv" "$DEST/rookmote.sh"
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/rookmote"      # Baketris's synthesized sound: made again when needed
if [ "${1:-}" = "--data" ]; then
    rm -rf "$DEST"
    echo "removed $DEST (profiles and settings too)"
else
    echo "removed the app, plugin and venv; profiles and settings stay in $DEST (--data removes them)"
fi
echo "Now take '- $DEST/plugin/remote-deck' out of /home/deck/.rook05/worker.yaml's plugins and restart the worker."
