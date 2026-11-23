"""The remote's key names, as the firmware has them (src/prof.cpp KEYS), and
the Deck's buttons they come from by default."""

from __future__ import annotations

import re

# The named keys, in the firmware's order (prof.cpp), and its aliases.
KEYS = ["power", "vol_up", "vol_down", "mute", "input", "up", "down", "left", "right", "ok", "back", "home",
        "menu", "play", "pause", "rewind", "forward", "ch_up", "ch_down", "info", "exit", "stop", "next", "prev",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
ALIASES = {"bt_menu": "menu", "settings": "menu", "select": "ok", "center": "ok", "enter": "ok",
           "volume_up": "vol_up", "volume_down": "vol_down"}
MAX_KEY = 15
_CUSTOM = re.compile(r"^[a-z0-9_]{1,%d}$" % MAX_KEY)

# Held, they repeat (as the firmware repeats IR codes for these).
REPEATS = {"vol_up", "vol_down", "up", "down", "left", "right", "ch_up", "ch_down", "rewind", "forward"}


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def valid_key(name: str) -> bool:
    """A named key, or any other name a remote reports ([a-z0-9_], 1..15)."""
    return canonical(name) in KEYS or bool(_CUSTOM.match(name or ""))


# The Deck's buttons (SDL game controller names, as Steam Input's virtual pad
# reports them) to key names, while the remote screen has the buttons. The
# View button opens the menu, as Guide does on the bridge; it is not a key.
DEFAULT_MAP = {
    "dpup": "up", "dpdown": "down", "dpleft": "left", "dpright": "right",
    "a": "ok", "b": "back", "x": "menu", "y": "home",
    "leftshoulder": "vol_down", "rightshoulder": "vol_up",
    "lefttrigger": "rewind", "righttrigger": "forward",
    "leftstick": "mute", "rightstick": "info",
    "start": "play",
}
BUTTON_LABELS = {"dpup": "D-pad up", "dpdown": "D-pad down", "dpleft": "D-pad left", "dpright": "D-pad right",
                 "a": "A", "b": "B", "x": "X", "y": "Y", "leftshoulder": "L1", "rightshoulder": "R1",
                 "lefttrigger": "L2", "righttrigger": "R2", "leftstick": "L3", "rightstick": "R3",
                 "start": "Menu", "back": "View"}
