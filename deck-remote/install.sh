#!/bin/sh
# Install the Rook remote app and its worker plugin on a Steam Deck, as user
# deck, under ~/.local/share/rookmote (no pacman, no root). Run it from this
# repository's deck-remote/ (the app and the plugin are both in it), e.g.
# after copying just that folder over:
#   git archive <branch> deck-remote | ssh deck@<deck> 'mkdir -p ~/rookmote-src && tar -x -C ~/rookmote-src'
#   ~/rookmote-src/deck-remote/install.sh
# It does not edit the worker's config or restart it: it prints the line to add
# and the command for the Steam shortcut. Run it again to update.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PLUGIN_SRC=$HERE/plugin/remote-deck
DEST=${ROOKMOTE_HOME:-$HOME/.local/share/rookmote}
PY=${PYTHON:-python3}
[ -f "$HERE/rookmote/app.py" ] || { echo "run this from the repository's deck-remote/" >&2; exit 1; }
[ -f "$PLUGIN_SRC/plugin.yaml" ] || { echo "missing $PLUGIN_SRC" >&2; exit 1; }
mkdir -p "$DEST"
echo "== the app's venv (pygame-ce) in $DEST/venv"
[ -x "$DEST/venv/bin/python" ] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/python" -m pip install -q --upgrade pip
"$DEST/venv/bin/python" -m pip install -q "pygame-ce==2.5.8"
echo "== the app in $DEST/app"
rm -rf "$DEST/app.new" && mkdir -p "$DEST/app.new"
cp -r "$HERE/rookmote" "$DEST/app.new/"
find "$DEST/app.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$DEST/app" && mv "$DEST/app.new" "$DEST/app"
echo "== the worker plugin in $DEST/plugin/remote-deck"
rm -rf "$DEST/plugin.new" && mkdir -p "$DEST/plugin.new"
cp -r "$PLUGIN_SRC" "$DEST/plugin.new/remote-deck"
# the app's standard-library modules, beside the plugin's script (the worker's venv has no pygame)
mkdir -p "$DEST/plugin.new/remote-deck/scripts/rookmote"
for m in __init__ paths keys profiles bridge lan router; do
    cp "$HERE/rookmote/$m.py" "$DEST/plugin.new/remote-deck/scripts/rookmote/"
done
find "$DEST/plugin.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$DEST/plugin" && mv "$DEST/plugin.new" "$DEST/plugin"
cat > "$DEST/rookmote.sh" <<EOS
#!/bin/sh
# The Rook remote (add this to Steam as a non-Steam game).
cd "$DEST/app" && exec "$DEST/venv/bin/python" -m rookmote "\$@"
EOS
chmod +x "$DEST/rookmote.sh"
echo
echo "Installed. Two steps are yours:"
echo
echo "1. The worker plugin: add this line under 'plugins:' in /home/deck/.rook05/worker.yaml"
echo "     - $DEST/plugin/remote-deck"
echo "   then: systemctl --user restart rook05-worker"
echo
echo "2. The Steam shortcut (then set its controller layout to 'Gamepad'):"
echo "     steamos-add-to-steam $DEST/rookmote.sh"
echo
echo "Try it in a window first: $DEST/rookmote.sh --window"
