"""Templating for config actions.

``{{args.x}}``, ``{{settings.x}}``, ``{{secret:NAME}}``, ``{{appliance.x}}``,
``{{worker.os}}``, ``{{tmp}}``, ``{{plugin.dir}}``, ``{{plugin.data}}``. No
expressions. A value is escaped by where it lands, so it can only fill its own
slot:

- argv: each item is one literal argument; a whole-item value that is a list
  becomes several arguments.
- url: a value at the very start must be an absolute URL (it is the base); in
  the host part it must be a host[:port]; anywhere else it is percent-encoded
  as one component.
- path: a value at the very start may be any path (it is the base); after that
  it is one path segment (no separators, not ``.`` or ``..``).
- header, line: no CR, LF or NUL.
- raw (stdin, body, file content): inserted as is.

A string that is exactly one template keeps the value's type (an int stays an
int in a JSON body). A value that is unset raises ``Missing``: argv groups,
map keys and list items that hit it are dropped; in text (stdin, body, send,
file content) an unset arg renders empty; anywhere else the action fails.

A list arg declared with ``join: ","`` renders as one joined string wherever
it becomes text (argv, URLs, headers, text); a JSON body keeps the list.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .errors import ActionError, Secret
from .parse import dig

REF = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_UNSET = object()
_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+)?(:\d{0,5})?")
_ABS_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


class Missing(Exception):
    """A template referenced a value that isn't set."""

    def __init__(self, ref: str):
        super().__init__(ref)
        self.ref = ref


class Scope:
    """What templates can see during one call, plus the secret values it
    handed out (for redaction)."""

    def __init__(self, *, args: Mapping, settings: Mapping, appliance: Mapping | None,
                 worker: Mapping, secrets: Mapping[str, str], tmp: str | None,
                 plugin_dir: str | None, plugin_data: str | None,
                 joins: Mapping[str, str] | None = None):
        self.args = args or {}
        self.joins = joins or {}
        self.settings = settings or {}
        self.appliance = appliance or {}
        self.worker = worker or {}
        self.secrets = secrets
        self.tmp = tmp
        self.plugin = {"dir": plugin_dir, "data": plugin_data}
        self.hidden: set[str] = {v for v in secrets.values() if v}
        for m in (self.settings, self.appliance):
            self.hidden.update(v for v in m.values() if isinstance(v, Secret) and v)

    def lookup(self, ref: str) -> Any:
        if ref.startswith("secret:"):
            name = ref[7:].strip()
            if self.secrets.get(name) is None:
                raise ActionError(f"secret {name} is not in the vault")
            return self.secrets[name]
        head, _, rest = ref.partition(".")
        if ref == "tmp":
            value = self.tmp
        elif head == "plugin" and rest in ("dir", "data"):
            value = self.plugin[rest]
        elif head in ("args", "settings", "appliance", "worker") and rest:
            value = dig(getattr(self, head), rest, _UNSET)
        else:
            raise ActionError("unknown template variable {{%s}}" % ref)
        if value is _UNSET or value is None:
            raise Missing(ref)
        return value

    def value(self, ref: str, *, empty: bool = False, keep_list: bool = False) -> Any:
        """``lookup`` for rendering: an unset arg is "" when ``empty``; a
        joined list arg is its joined text unless ``keep_list``."""
        try:
            found = self.lookup(ref)
        except Missing:
            if empty and ref.startswith("args."):
                return ""
            raise
        sep = self.joins.get(ref[5:]) if ref.startswith("args.") else None
        if sep is not None and isinstance(found, list) and not keep_list:
            return sep.join(text(v) for v in found)
        return found

    def redact(self, text: str) -> str:
        for s in sorted(self.hidden, key=len, reverse=True):
            text = text.replace(s, "***")
        return text


def text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def render(value: Any, scope: Scope, pos: str = "raw", *, native: bool = False,
           empty: bool = False) -> Any:
    """Fill the templates in one string for position ``pos``. Non-strings
    pass through. ``native`` keeps a lone template's value type; ``empty``
    renders unset args as "" (text positions)."""
    if not isinstance(value, str):
        return value
    parts = REF.split(value)
    if len(parts) == 1:
        return value
    if native and len(parts) == 3 and parts[0] == parts[2] == "":
        found = scope.value(parts[1], empty=empty, keep_list=pos == "json")
        if isinstance(found, str):
            _check(found, pos, parts[1])
        return found
    out = parts[0]
    for i in range(1, len(parts), 2):
        found = text(scope.value(parts[i], empty=empty))
        out += _escape(found, pos, out, leading=(i == 1 and parts[0] == ""), ref=parts[i])
        out += parts[i + 1]
    return out


def render_str(value: Any, scope: Scope, pos: str = "raw") -> str:
    return text(render(value, scope, pos, native=True))


def render_text(value: Any, scope: Scope, pos: str = "raw") -> str:
    """Free text (stdin, body, a sent line, file content): unset args are ""."""
    return text(render(value, scope, pos, native=True, empty=True))


def render_argv(items: list, scope: Scope) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, list):
            try:
                out.extend(render_argv(item, scope))
            except Missing:
                continue   # an optional group with an unset value: dropped
            continue
        found = render(item, scope, "argv", native=True)
        for one in found if isinstance(found, list) else [found]:
            one = text(one)
            _check(one, "argv", "argv item")
            out.append(one)
    return out


def render_map(m: Mapping | None, scope: Scope, pos: str) -> dict[str, str]:
    """A flat map of strings (query, headers, form, env); keys whose value is
    unset are dropped."""
    out: dict[str, str] = {}
    for k, v in (m or {}).items():
        try:
            out[str(k)] = render_str(v, scope, pos)
        except Missing:
            continue
    return out


def render_tree(value: Any, scope: Scope) -> Any:
    """A JSON value: strings templated (lone templates keep their type), map
    keys and list items whose value is unset dropped."""
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            try:
                out[k] = render_tree(v, scope)
            except Missing:
                continue
        return out
    if isinstance(value, list):
        out_list = []
        for v in value:
            try:
                out_list.append(render_tree(v, scope))
            except Missing:
                continue
        return out_list
    return render(value, scope, "json", native=True)


def secret_names(value: Any) -> set[str]:
    """Every ``{{secret:NAME}}`` in a spec, so they can be fetched up front."""
    if isinstance(value, str):
        return {r[7:].strip() for r in REF.findall(value) if r.startswith("secret:")}
    if isinstance(value, Mapping):
        value = list(value.values())
    if isinstance(value, list):
        return set().union(*(secret_names(v) for v in value)) if value else set()
    return set()


_FORBIDDEN = {"raw": "", "json": "", "argv": "\0", "env": "\0", "path": "\0", "url": "\0",
              "header": "\r\n\0", "line": "\r\n\0"}


def _check(value: str, pos: str, ref: str) -> None:
    if any(c in value for c in _FORBIDDEN[pos]):
        raise ActionError("{{%s}} has a control character where it can't go" % (ref or "value"))


def _escape(value: str, pos: str, before: str, *, leading: bool, ref: str) -> str:
    _check(value, pos, ref)
    if pos == "url":
        if leading:
            if not _ABS_URL.fullmatch(value):
                raise ActionError("{{%s}} must be an absolute URL" % ref)
            return value
        scheme = before.find("://")
        if scheme >= 0 and not re.search(r"[/?#]", before[scheme + 3:]):
            if not _HOST.fullmatch(value):
                raise ActionError("{{%s}} is not a host name" % ref)
            return value
        return _dots(quote(value, safe=""))
    if pos == "path" and not leading:
        if any(c in value for c in "/\\") or value in (".", ".."):
            raise ActionError("{{%s}} must be a single file name" % ref)
    return value


def _dots(s: str) -> str:
    return s.replace(".", "%2E") if s in (".", "..") else s
