"""Server-rendered pages kept as static files under ``rook/web``.

A page is plain HTML with ``{{name}}`` slots. ``render`` fills them in one
pass, so a value that itself contains ``{{...}}`` is never expanded again.
Values are HTML-escaped unless the slot is written ``{{name|raw}}``; raw slots
are for markup the server built itself, never for request or stored text.
"""
import html
import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
_SLOT = re.compile(r"\{\{([a-z_]+)(\|raw)?\}\}")


def render(name: str, **values) -> str:
    text = (WEB_DIR / name).read_text(encoding="utf-8")

    def fill(match):
        key, raw = match.group(1), match.group(2)
        if key not in values:
            raise KeyError(f"{name}: no value for slot {key!r}")
        value = "" if values[key] is None else str(values[key])
        return value if raw else html.escape(value, quote=True)
    return _SLOT.sub(fill, text)
