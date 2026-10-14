"""Dashboard themes: the ``rook-theme/1`` format (docs/web/themes.md).

A theme is a small JSON file of named colour tokens. The web dashboard turns
it into CSS custom properties on ``:root`` (``--bg``, ``--accent``, ...);
the desktop app is meant to read the same files. Bundled themes live in
rook/web/themes/. This module loads and checks them and renders the CSS the
dashboard's default stylesheet (theme.css) carries for ``rook-dark``; the
browser does the same in rook/web/themes.js, with the same rules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FORMAT = "rook-theme/1"
DEFAULT = "rook-dark"
THEME_DIR = Path(__file__).resolve().parents[3] / "web" / "themes"

#: Every colour token, in the order the CSS lists them. A theme that
#: ``extends`` another may leave any of them out.
TOKENS = (
    "bg", "sidebar", "panel", "panel2", "overlay", "hover", "chip", "group-bg", "banner-bg",
    "line", "line2", "line-soft", "corner", "fg", "fg-soft", "dim", "dim2",
    "accent", "accent-dim", "accent-hi", "accent-bg", "accent-bg-strong", "accent-line",
    "on-accent", "good", "good-fg", "good-bg", "good-line", "warn", "warn-bg", "warn-line",
    "bad", "bad-bg", "bad-line", "info", "shadow", "backdrop", "term-bg", "term-fg",
    "art-line", "art-ink", "art-hatch", "art-fill", "art-text", "art-text2",
    "scene-1", "scene-2", "scene-3",
)
FONTS = ("sans", "mono")
#: Older names the stylesheets still use, kept as aliases of a token.
ALIASES = {"green": "good", "blue": "info"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# A colour: #rgb[a] / #rrggbb[aa], or an rgb()/rgba()/hsl()/hsla() call with
# plain numbers. Nothing that could close the declaration or the rule.
COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}"
                      r"|(rgb|rgba|hsl|hsla)\([0-9.,%\s]{1,60}\))$")
FONT_RE = re.compile(r"^[\w\s,'\"-]{1,120}$")


class ThemeError(ValueError):
    pass


def check(doc: dict) -> dict:
    """The theme ``doc`` normalised, or :class:`ThemeError` saying why not."""
    if not isinstance(doc, dict):
        raise ThemeError("a theme is a JSON object")
    if doc.get("format") != FORMAT:
        raise ThemeError(f"format must be {FORMAT!r}")
    tid = doc.get("id")
    if not isinstance(tid, str) or not ID_RE.match(tid):
        raise ThemeError("id: lowercase letters, digits and dashes, up to 40")
    scheme = doc.get("scheme", "dark")
    if scheme not in ("dark", "light"):
        raise ThemeError("scheme is dark or light")
    tokens = doc.get("tokens")
    if not isinstance(tokens, dict):
        raise ThemeError("tokens must be an object")
    out = {}
    for key, value in tokens.items():
        if key in FONTS:
            if not isinstance(value, str) or not FONT_RE.match(value):
                raise ThemeError(f"tokens.{key}: a font-family list")
        elif key in TOKENS:
            if not isinstance(value, str) or not COLOR_RE.match(value.strip()):
                raise ThemeError(f"tokens.{key}: not a colour ({value!r})")
        else:
            continue  # a newer token this hub does not know: ignored
        out[key] = value.strip()
    extends = doc.get("extends")
    if extends is not None and (not isinstance(extends, str) or not ID_RE.match(extends)):
        raise ThemeError("extends: a theme id")
    if extends is None and tid == DEFAULT:
        missing = [k for k in TOKENS if k not in out]
        if missing:
            raise ThemeError(f"the default theme must set every token (missing {missing})")
    return {"format": FORMAT, "id": tid, "name": str(doc.get("name") or tid)[:60],
            "description": str(doc.get("description") or "")[:240], "scheme": scheme,
            "extends": extends, "tokens": out}


def load(directory: Path = THEME_DIR) -> dict[str, dict]:
    """``{id: theme}`` for every valid ``*.json`` theme in ``directory``."""
    themes = {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            theme = check(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise ThemeError(f"{path.name}: {e}") from None
        if theme["id"] != path.stem:
            raise ThemeError(f"{path.name}: id {theme['id']!r} does not match the file name")
        themes[theme["id"]] = theme
    return themes


def resolved(theme: dict, themes: dict[str, dict]) -> dict:
    """Every token of ``theme``, filled in from what it extends (default:
    ``rook-dark``)."""
    chain, seen = [theme], {theme["id"]}
    while True:
        parent = chain[-1].get("extends") or (DEFAULT if chain[-1]["id"] != DEFAULT else None)
        if not parent or parent in seen or parent not in themes:
            break
        seen.add(parent)
        chain.append(themes[parent])
    tokens = {}
    for t in reversed(chain):
        tokens.update(t["tokens"])
    return tokens


def css(theme: dict, themes: dict[str, dict]) -> str:
    """``:root{...}`` for ``theme``: one custom property per token."""
    tokens = resolved(theme, themes)
    decls = [f"color-scheme:{theme['scheme']}"]
    decls += [f"--{k}:{tokens[k]}" for k in TOKENS + FONTS if k in tokens]
    decls += [f"--{alias}:{tokens[k]}" for alias, k in ALIASES.items() if k in tokens]
    return ":root{" + ";".join(decls) + "}"


def summary(theme: dict, themes: dict[str, dict]) -> dict:
    """What a picker shows: id, name, description, scheme and a few swatches."""
    tokens = resolved(theme, themes)
    return {"id": theme["id"], "name": theme["name"], "description": theme["description"],
            "scheme": theme["scheme"],
            "swatches": [tokens.get(k) for k in ("bg", "panel", "fg", "accent", "good", "bad")]}
