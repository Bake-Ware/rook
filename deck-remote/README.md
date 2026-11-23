# Rook remote for the Steam Deck

The firmware remote (`firmware-remote/`) as a fullscreen app on a Steam Deck,
driven by its buttons and touchscreen, minus IR.

- `rookmote/`: the app (Python + pygame-ce). `keys`, `paths`, `profiles`,
  `bridge`, `lan`, `router` are standard library only: the worker plugin
  uses them too.
- `../rook/plugins/remote-deck/`: the plugin for the Deck's worker. It emits
  `remote.button` as the firmware does (the hub's jobs filter on profile and
  button, so the Deck's presses drive the same jobs) and serves `remote.state`,
  `remote.press`, `remote.ir` (profiles), `remote.chat`, `remote.display`.
- `install.sh` / `uninstall.sh`: run on the Deck as user `deck`.
- `tests/`: `test_core.py` (store, router, socket, plugin), `render_screens.py`
  (every screen to PNG, headless).

## Buttons

On Home the Deck's buttons are the remote's keys (shown on screen; every key
is also on the touch keypad). View opens the menu tabs, as Guide does on the
bridge; L1/R1 switch tabs there. Leave with Settings > Exit, the close button,
or by holding View and Menu for a second.

## Left out

The OS does these, or a Deck has none: IR (an imported profile's IR keys show
as unavailable), the Wi-Fi manager, over-the-air updates, Bluetooth pairing,
sleep and screen-off, the button learner, the bridge's remote.bt, remote.map,
remote.update, remote.coredump, remote.wifi.
