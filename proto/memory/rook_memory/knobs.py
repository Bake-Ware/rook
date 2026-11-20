"""Loading knobs.toml, the one file every design knob lives in."""

import pathlib
import tomllib

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "knobs.toml"


def load(path=None, overrides=()):
    path = pathlib.Path(path or DEFAULT).resolve()
    with open(path, "rb") as f:
        knobs = tomllib.load(f)
    knobs["_path"] = str(path)
    for o in overrides:
        set_knob(knobs, o)
    knobs["_overrides"] = list(overrides)
    return knobs


def set_knob(knobs, text):
    """`section.key=value` (or `models.name.key=value`); the value is read as TOML."""
    key, _, value = text.partition("=")
    *path, last = key.strip().split(".")
    where = knobs
    for p in path:
        where = where.setdefault(p, {})
    try:
        where[last] = tomllib.loads(f"v = {value.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        where[last] = value.strip()


def search(knobs):
    """The search knobs with the chosen model's own settings folded in."""
    s = dict(knobs["search"])
    s.update(knobs.get("models", {}).get(s["model"], {}))
    s.setdefault("repo", s["model"])
    return s
