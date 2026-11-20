"""Loading knobs.toml, the one file every design knob lives in."""

import pathlib
import tomllib

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "knobs.toml"


def load(path=None):
    path = pathlib.Path(path or DEFAULT).resolve()
    with open(path, "rb") as f:
        knobs = tomllib.load(f)
    knobs["_path"] = str(path)
    return knobs
