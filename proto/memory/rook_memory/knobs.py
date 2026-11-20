"""Loading knobs.toml, the one file every design knob lives in."""

import pathlib
import tomllib

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "knobs.toml"


def load(path=None):
    with open(path or DEFAULT, "rb") as f:
        return tomllib.load(f)
