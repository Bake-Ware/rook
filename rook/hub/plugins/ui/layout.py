"""The start page layout: per-account, stored in the ``ui.startpage`` setting.

    {"version": 1,
     "columns": [[{"id": "w1", "type": "bookmarks", "title": "Media",
                   "options": {"links": [{"label": "Jellyfin", "url": "https://..."}]}},
                  ...],
                 ...]}

Columns are ordered lists of widgets (1 to :data:`MAX_COLUMNS`). The
built-in widget types are checked here; a type a plugin adds (any other
dotted name) keeps its ``options`` as given, within the size limits, and
its own script renders it (docs/web/start-page.md).
"""

from __future__ import annotations

import json
import re

VERSION = 1
MAX_COLUMNS = 4
MAX_WIDGETS = 40
MAX_WIDGET_BYTES = 8 * 1024
MAX_LAYOUT_BYTES = 64 * 1024
MAX_LINKS = 60
MAX_GROUPS = 12

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}(\.[a-z0-9_-]{1,30}){0,2}$")
#: Band widgets read the hub's own data; they take no options but a limit.
BAND_TYPES = ("workers", "tasks", "rooms", "knowledge", "sessions")
BUILTIN = ("bookmarks", "feed", "note") + BAND_TYPES


class LayoutError(ValueError):
    pass


def default() -> dict:
    """What an account sees before it changes anything (normalised, so it
    compares equal to what :func:`check` returns)."""
    return check({"version": VERSION, "columns": [
        [{"id": "workers", "type": "workers", "title": "Workers"},
         {"id": "tasks", "type": "tasks", "title": "Open tasks", "options": {"limit": 6}}],
        [{"id": "rooms", "type": "rooms", "title": "Chat rooms", "options": {"limit": 6}},
         {"id": "knowledge", "type": "knowledge", "title": "Recently changed pages",
          "options": {"limit": 6}}],
        [{"id": "getting-around", "type": "bookmarks", "title": "Getting around",
          "options": {"groups": [{"name": "Rook", "links": [
              {"label": "Overview", "url": "#overview"},
              {"label": "Sessions", "url": "#sessions"},
              {"label": "Knowledge", "url": "#knowledge"},
              {"label": "Add a worker", "url": "#install"}]}]}},
         {"id": "note", "type": "note", "title": "Make it yours",
          "options": {"text": "Choose Edit page to add bookmarks, news feeds and band "
                              "widgets, and drag them between columns."}}],
    ]})


def _str(value, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise LayoutError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise LayoutError(f"{name} is required")
    if len(value) > limit:
        raise LayoutError(f"{name} is longer than {limit} characters")
    return value


def check_url(value, name: str) -> str:
    """A link the page may render: http(s), or a dashboard ``#view``."""
    url = _str(value, name, 2000, required=True)
    low = url.lower()
    if not (low.startswith(("http://", "https://")) or re.fullmatch(r"#[a-z][\w?=&%./-]{0,200}", url)):
        raise LayoutError(f"{name}: links must start with http://, https:// or # (a dashboard page)")
    return url


def _limit(options: dict, default: int = 6, top: int = 30) -> int:
    value = options.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise LayoutError(f"limit must be a whole number from 1 to {top}")
    return value


def _links(raw, where: str) -> list[dict]:
    if not isinstance(raw, list):
        raise LayoutError(f"{where}: links must be a list")
    out = []
    for i, link in enumerate(raw):
        if not isinstance(link, dict):
            raise LayoutError(f"{where}: link {i + 1} must be an object")
        item = {"label": _str(link.get("label"), f"{where} link label", 80, required=True),
                "url": check_url(link.get("url"), f"{where} link {i + 1}")}
        note = _str(link.get("note"), f"{where} link note", 160)
        if note:
            item["note"] = note
        out.append(item)
    return out


def _options(wtype: str, options) -> dict:
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise LayoutError("options must be an object")
    if wtype == "bookmarks":
        groups = options.get("groups")
        if groups is None and "links" in options:      # one unnamed group
            groups = [{"name": "", "links": options["links"]}]
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            raise LayoutError(f"a bookmarks widget has up to {MAX_GROUPS} groups")
        out, total = [], 0
        for g in groups:
            if not isinstance(g, dict):
                raise LayoutError("each bookmark group must be an object")
            name = _str(g.get("name"), "group name", 60)
            links = _links(g.get("links") or [], name or "bookmarks")
            total += len(links)
            out.append({"name": name, "links": links})
        if total > MAX_LINKS:
            raise LayoutError(f"a bookmarks widget holds up to {MAX_LINKS} links")
        return {"groups": out}
    if wtype == "feed":
        from .feeds import FeedError, check_url as feed_url
        url = _str(options.get("url"), "feed address", 2000, required=True)
        try:
            feed_url(url)
        except FeedError as e:
            raise LayoutError(str(e)) from None
        out = {"url": url, "limit": _limit(options, 8)}
        if options.get("summaries") is not None:
            out["summaries"] = bool(options.get("summaries"))
        return out
    if wtype == "note":
        return {"text": _str(options.get("text"), "note", 2000)}
    if wtype in BAND_TYPES:
        return {"limit": _limit(options)} if "limit" in options else {}
    return options  # a plugin's widget: its own script reads the options


def check(layout) -> dict:
    """The layout normalised, or :class:`LayoutError` with a reason a person
    can act on."""
    if not isinstance(layout, dict):
        raise LayoutError("the layout must be an object")
    if len(json.dumps(layout)) > MAX_LAYOUT_BYTES:
        raise LayoutError("the page is too large to save; remove some widgets")
    if layout.get("version", VERSION) != VERSION:
        raise LayoutError(f"unknown layout version {layout.get('version')!r}")
    columns = layout.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise LayoutError(f"the page has 1 to {MAX_COLUMNS} columns")
    seen, out, count = set(), [], 0
    for col in columns:
        if not isinstance(col, list):
            raise LayoutError("each column is a list of widgets")
        widgets = []
        for w in col:
            count += 1
            if count > MAX_WIDGETS:
                raise LayoutError(f"a page holds up to {MAX_WIDGETS} widgets")
            if not isinstance(w, dict):
                raise LayoutError("each widget must be an object")
            wid, wtype = w.get("id"), w.get("type")
            if not isinstance(wid, str) or not ID_RE.match(wid) or wid in seen:
                raise LayoutError("each widget needs a unique id (letters, digits, - and _)")
            if not isinstance(wtype, str) or not TYPE_RE.match(wtype):
                raise LayoutError(f"widget {wid}: unknown type")
            if wtype not in BUILTIN and "." not in wtype:
                raise LayoutError(f"widget {wid}: unknown type {wtype!r}")
            if len(json.dumps(w)) > MAX_WIDGET_BYTES:
                raise LayoutError(f"widget {wid} is too large")
            seen.add(wid)
            item = {"id": wid, "type": wtype, "title": _str(w.get("title"), "title", 80)}
            try:
                item["options"] = _options(wtype, w.get("options"))
            except LayoutError as e:
                raise LayoutError(f"{item['title'] or wtype}: {e}") from None
            if w.get("collapsed"):
                item["collapsed"] = True
            widgets.append(item)
        out.append(widgets)
    return {"version": VERSION, "columns": out}
