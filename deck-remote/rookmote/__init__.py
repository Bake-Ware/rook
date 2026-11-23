"""Rook remote for the Steam Deck: the firmware remote (firmware-remote/) as a
fullscreen app, driven by the Deck's buttons and touchscreen, minus IR.

The modules below the UI (keys, profiles, paths, bridge, lan) import nothing
outside the standard library: the worker plugin (rook/plugins/remote-deck)
uses them too, from the worker's own venv, which has no pygame.
"""

VERSION = "0.1.0"
