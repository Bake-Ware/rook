"""The Games tab's games: Baketris (baketris.py, rules.py, sfx.py, music.py, audio.py) and the
remote's own small games, ported from firmware-remote/src (mini.py; g2048.py, ...)."""


class _Lazy(dict):
    """name -> its Screen class, imported when first opened."""

    def get(self, name, default=None):
        mod = {"2048": ("g2048", "Game2048")}.get(name)
        if not mod:
            return default
        import importlib
        return getattr(importlib.import_module(f"{__name__}.{mod[0]}"), mod[1])


MINI = _Lazy()
