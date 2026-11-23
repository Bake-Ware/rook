# Rook remote for the Steam Deck

The firmware remote (`firmware-remote/`) as a fullscreen app on a Steam Deck,
driven by its buttons and touchscreen, minus IR.

- `rookmote/`: the app (Python + pygame-ce). `keys`, `paths`, `profiles`,
  `bridge`, `lan`, `router` are standard library only: the worker plugin
  uses them too.
- `plugin/remote-deck/`: the plugin for the Deck's worker (installed by
  `install.sh`, not distributed by the hub, so it lives here and not in
  `rook/plugins/`). It emits
  `remote.button` as the firmware does (the hub's jobs filter on profile and
  button, so the Deck's presses drive the same jobs) and serves `remote.state`,
  `remote.press`, `remote.ir` (profiles), `remote.chat`, `remote.display`.
- `rookmote/games/`: the Games tab. Baketris (`rules.py`, `baketris.py`,
  `sfx.py`, `music.py`, `audio.py`) is the Android Baketris (/home/bake/baketris)
  ported: Game.java's rules, GameView's skins, effects, menus, settings and
  controller mapping, Sfx.java's and Music.java's synthesized sound.
- `install.sh` / `uninstall.sh`: run on the Deck as user `deck`.
- `tests/`: `test_core.py` (store, router, socket, plugin), `render_screens.py`
  (every screen to PNG, headless); `test_baketris_rules.py` and
  `test_baketris_audio.py` hold the port to traces and sounds made by
  Baketris's own Java (`make_baketris_fixtures.py`,
  `make_baketris_audio_fixtures.py` make them; they need a JDK);
  `test_baketris_screen.py` (controls, settings, effects, the app);
  `render_baketris.py` (its screens to PNG, and frame times).

## Baketris's sound

Synthesizing it is pure Python and slow (a few seconds a skin's sounds, tens
of seconds a song), so the app never does it while you play: the first time
Baketris opens, a niced child process (`python -m rookmote.games.audio`)
renders what the game needs first (your skin's sounds, then the song, then
the rest) into `~/.cache/rookmote/baketris-audio-1/`, and the game plays
from memory. Until a sound is cached it is silent. The cache can be deleted
at any time; it is made again.

## Buttons

On Home the Deck's buttons are the remote's keys (shown on screen; every key
is also on the touch keypad). View opens the menu tabs, as Guide does on the
bridge; L1/R1 switch tabs there. Leave with Settings > Exit, the close button,
or by holding View and Menu for a second.

In Baketris the buttons are the Android build's controller mapping (its
CONTROLS screen lists them): d-pad or left stick move, down soft drop, up or
R2 hard drop, A/B rotate, Y 180, X/L1/R1/L2 hold, Start or View pause; B on
its menu goes back to the remote. Holding View and Menu still leaves the app.

## Left out

The OS does these, or a Deck has none: IR (an imported profile's IR keys show
as unavailable), the Wi-Fi manager, over-the-air updates, Bluetooth pairing,
sleep and screen-off, the button learner, the bridge's remote.bt, remote.map,
remote.update, remote.coredump, remote.wifi.
