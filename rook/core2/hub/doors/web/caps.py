"""The hub's builtin caps behind the dashboard (castling-dashboard): pages,
hub, approval, journal and setting, declared in ``builtin/<plugin>/plugin.yaml``
and run in-process on the hub node like any hub cap (dispatch, rights,
journal). :func:`install` registers them at hub start.

Pages are a content kind: a plugin ships them in its manifest (``pages:``),
and agents make them with ``pages.create``. An agent-made page lives in
``<data>/pages/<name>/`` as ``page.html``, ``meta.json`` and ``NOTE.md`` (why
it exists and its history: the provenance note shown beside it). Archiving
is reversible and kept in ``<data>/pages.json``.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from rook.kit.ctx import KitError
from rook.kit.manifest import compile_fields
from rook.kit.manifest.types import setting_spec

from rook.core2 import CORE_VERSION
from rook.core2.hub.identity import Caller

BUILTIN = Path(__file__).with_name("builtin")
MADE = "pages"                       # agent-made pages are pages/<name>
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_HTML = 512 * 1024
MAX_NOTE = 64 * 1024


@dataclass
class Page:
    id: str                  # <plugin>/<name>
    title: str
    file: Path
    caps: list = field(default_factory=list)
    events: list = field(default_factory=list)
    sizes: list = field(default_factory=list)
    section: str = "Pages"
    made: dict | None = None     # agent-made: {authors: [{person, agent}], created, task}

    def view(self) -> dict:
        return {"page": self.id, "title": self.title, "caps": list(self.caps), "events": list(self.events),
                "sizes": list(self.sizes), "section": self.section, "made": self.made is not None}


class Pages:
    """Every page on the hub site: the hub plugins' manifests plus the pages
    agents made, and which of them are archived."""

    def __init__(self, hub):
        self.hub = hub
        self.folder = Path(hub.data) / "pages"
        self._state_path = Path(hub.data) / "pages.json"
        self.archived: dict = _read_json(self._state_path).get("archived", {})

    def all(self) -> dict[str, Page]:
        out = {}
        for p in list(self.hub.plugins.plugins.values()):
            for name, spec in (p.manifest.get("pages") or {}).items():
                if p.folder is None:
                    continue
                out[f"{p.name}/{name}"] = Page(
                    f"{p.name}/{name}", spec.get("title") or name, Path(p.folder) / spec["file"],
                    list(spec.get("caps") or ()), list(spec.get("events") or ()), list(spec.get("sizes") or ()),
                    (spec.get("nav") or {}).get("section") or p.manifest.get("title") or p.name)
        for meta_path in sorted(self.folder.glob("*/meta.json")) if self.folder.is_dir() else ():
            m = _read_json(meta_path)
            name = meta_path.parent.name
            if not NAME.fullmatch(name) or not m:
                continue
            pid = f"{MADE}/{name}"
            out[pid] = Page(pid, m.get("title") or name, meta_path.parent / "page.html", list(m.get("caps") or ()),
                            list(m.get("events") or ()), ["page"], m.get("section") or "Pages",
                            {k: m.get(k) for k in ("authors", "created", "task")})
        return out

    def get(self, pid: str) -> Page:
        page = self.all().get(pid) if isinstance(pid, str) else None
        if page is None:
            raise KitError(f"no page {pid}")
        return page

    def openable(self, pid: str) -> Page:
        page = self.get(pid)
        if pid in self.archived:
            raise KitError(f"{pid} is archived: restore it first")
        return page

    def html(self, page: Page) -> str:
        """The page's file, which must stay inside its own folder."""
        root = (page.file.parent if page.made else _plugin_folder(self.hub, page.id)).resolve()
        path = page.file.resolve()
        if root not in path.parents or not path.is_file():
            raise KitError(f"{page.id}: its file is missing")
        return path.read_text(encoding="utf-8")

    def note(self, page: Page) -> str | None:
        path = page.file.parent / "NOTE.md"
        return path.read_text(encoding="utf-8") if page.made and path.is_file() else None

    def save_state(self) -> None:
        _write(self._state_path, json.dumps({"archived": self.archived}, indent=1))


def _plugin_folder(hub, pid: str) -> Path:
    return Path(hub.plugins.plugins[pid.partition("/")[0]].folder)


# -- install --------------------------------------------------------------------

def install(hub) -> None:
    """Register the builtin plugins on the hub node and load stored settings."""
    hub.pages = Pages(hub)
    settings = Settings(hub)
    h = _Handlers(hub, settings)
    table = {
        "pages": {"pages.list": h.pages_list, "pages.view": h.pages_view, "pages.get": h.pages_get,
                  "pages.create": h.pages_create, "pages.update": h.pages_update,
                  "pages.archive": h.pages_archive, "pages.restore": h.pages_restore},
        "hub": {"hub.status": h.hub_status, "hub.workers": h.hub_workers},
        "approval": {"approval.list": h.approval_list, "approval.decide": h.approval_decide},
        "journal": {"journal.search": h.journal_search},
        "setting": {"setting.list": settings.list, "setting.set": settings.set},
    }
    for plugin, handlers in table.items():
        hub.plugins.add_builtin(BUILTIN / plugin, handlers)


def _who(ctx) -> Caller:
    return Caller(ctx.caller.person, ctx.caller.agent)


class _Handlers:
    def __init__(self, hub, settings):
        self.hub, self.settings = hub, settings

    # -- pages ------------------------------------------------------------------

    async def pages_list(self, ctx, archived: bool = False) -> dict:
        pages = self.hub.pages
        out = []
        for pid, page in sorted(pages.all().items(), key=lambda kv: (kv[1].section, kv[1].title)):
            if (pid in pages.archived) != archived:
                continue
            row = page.view()
            if archived:
                row["archived"] = pages.archived[pid]
            out.append(row)
        return {"pages": out}

    async def pages_view(self, ctx, page: str) -> dict:
        return self.hub.pages.openable(page).view()

    async def pages_get(self, ctx, page: str) -> dict:
        pages = self.hub.pages
        p = pages.get(page)
        out = {**p.view(), "archived": pages.archived.get(page)}
        if p.made:
            out.update(made_by=p.made, note=pages.note(p))
        else:
            plugin = self.hub.plugins.plugins[page.partition("/")[0]].manifest
            out["plugin"] = {"name": plugin["plugin"], "version": str(plugin["version"]),
                             "builtin": bool(plugin.get("builtin"))}
        return out

    async def pages_create(self, ctx, name: str, title: str, html: str, note: str, caps: list | None = None,
                           events: list | None = None, section: str | None = None, task: str | None = None) -> dict:
        if not NAME.fullmatch(name):
            raise KitError("bad args: name is lowercase letters, digits, - and _ (64 at most)")
        folder = self.hub.pages.folder / name
        if (folder / "meta.json").exists():
            raise KitError(f"{MADE}/{name} exists: change it with pages.update")
        _check_sizes(html, note)
        meta = {"title": title, "caps": _patterns(caps), "events": _patterns(events), "section": section or "Pages",
                "authors": [_author(ctx)], "created": time.time(), "task": task,
                "note": note, "history": [_history(ctx, "made the page")]}
        folder.mkdir(parents=True, exist_ok=True)
        _write(folder / "page.html", html)
        self._save(folder, meta)
        return {"page": f"{MADE}/{name}"}

    async def pages_update(self, ctx, page: str, change: str, title: str | None = None, html: str | None = None,
                           note: str | None = None, caps: list | None = None, events: list | None = None,
                           section: str | None = None) -> dict:
        p = self.hub.pages.get(page)
        if not p.made:
            raise KitError(f"{page} comes with a plugin: change the plugin instead")
        folder = p.file.parent
        meta = _read_json(folder / "meta.json")
        _check_sizes(html or "", note or "")
        for key, value in (("title", title), ("note", note), ("section", section)):
            if value is not None:
                meta[key] = value
        for key, value in (("caps", caps), ("events", events)):
            if value is not None:
                meta[key] = _patterns(value)
        meta.setdefault("history", []).append(_history(ctx, change))
        if _author(ctx) not in meta.setdefault("authors", []):
            meta["authors"].append(_author(ctx))       # the page never does more than anyone who wrote it
        if html is not None:
            _write(folder / "page.html", html)
        self._save(folder, meta)
        return {"page": page}

    def _save(self, folder: Path, meta: dict) -> None:
        _write(folder / "meta.json", json.dumps(meta, indent=1))
        lines = [f"# {meta['title']}", "", meta.get("note") or "", "", "## History", ""]
        lines += [f"- {h['at']}, {h['by']}: {h['change']}" for h in meta.get("history", [])]
        _write(folder / "NOTE.md", "\n".join(lines) + "\n")

    async def pages_archive(self, ctx, page: str, reason: str | None = None) -> dict:
        pages = self.hub.pages
        pages.get(page)
        pages.archived[page] = {"at": time.time(), "by": ctx.caller.actor, "reason": reason or ""}
        pages.save_state()
        return {"page": page, "archived": True}

    async def pages_restore(self, ctx, page: str) -> dict:
        pages = self.hub.pages
        pages.get(page)
        if pages.archived.pop(page, None) is not None:
            pages.save_state()
        return {"page": page, "archived": False}

    # -- hub, approvals, journal --------------------------------------------------

    async def hub_status(self, ctx) -> dict:
        hub = self.hub
        out = {"hub": hub.hub_id, "node": hub.node, "band": hub.band, "core": CORE_VERSION,
               "doors": [d.name for d in hub.doors],
               "plugins": [{"plugin": p.name, "version": str(p.manifest["version"]),
                            "title": p.manifest.get("title") or p.name, "builtin": bool(p.manifest.get("builtin")),
                            "runs_on": p.manifest.get("runs_on"), "caps": len(p.manifest.get("caps") or {}),
                            "pages": sorted(p.manifest.get("pages") or {})}
                           for p in sorted(hub.plugins.plugins.values(), key=lambda p: p.name)]}
        if ctx.caller.owner:
            out["people"] = [{"name": u.name, "preset": u.preset, "grants": list(u.grants)}
                             for u in sorted(hub.identities.users.values(), key=lambda u: u.name)]
            out["agents"] = sorted(hub.identities.agents)
        return out

    async def hub_workers(self, ctx) -> dict:
        return {"workers": (await self.hub.workers_view(_who(ctx)))["workers"]}

    async def approval_list(self, ctx) -> dict:
        return {"holds": (await self.hub.holds_view(_who(ctx)))["holds"]}

    async def approval_decide(self, ctx, hold: str, decision: str) -> dict:
        out = await self.hub.decide(_who(ctx), hold, decision)
        if not out.get("ok") and "id" not in out:      # unknown hold, not theirs to answer
            raise KitError(out["error"])
        return out          # a released call's own reply, ok or not

    async def journal_search(self, ctx, limit: int = 50, **filters) -> dict:
        out = await self.hub.journal_view(_who(ctx), {**filters, "limit": limit})
        if not out.get("ok"):
            raise KitError(out["error"])
        return {"entries": out["entries"]}


# -- settings -------------------------------------------------------------------

class Settings:
    """Hub-side settings (``where: hub``) of the hub's plugins. Values set
    with ``setting.set`` are kept in ``<data>/settings.json`` and win over the
    config file; the plugins read them on their next call."""

    def __init__(self, hub):
        self.hub = hub
        self.path = Path(hub.data) / "settings.json"
        self.base = copy.deepcopy(hub.cfg.settings)        # the config file's values
        self.stored: dict = _read_json(self.path)
        for plugin, values in self.stored.items():
            hub.plugins.settings.setdefault(plugin, {}).update(values)

    def _specs(self, plugin: str | None = None):
        for p in sorted(list(self.hub.plugins.plugins.values()), key=lambda p: p.name):
            if plugin and p.name != plugin:
                continue
            for key, spec in (p.manifest.get("settings") or {}).items():
                if spec.get("where") == "hub":
                    yield p.name, key, spec

    async def list(self, ctx, plugin: str | None = None) -> dict:
        out = []
        for name, key, spec in self._specs(plugin):
            values = self.hub.plugins.settings.get(name, {})
            source = "set" if key in self.stored.get(name, {}) else \
                "config" if key in self.base.get(name, {}) else "default"
            out.append({"plugin": name, "key": key, "type": spec.get("type"), "doc": spec.get("doc", ""),
                        "default": spec.get("default"), "secret": bool(spec.get("secret")),
                        "value": values.get(key, spec.get("default")), "source": source})
        return {"settings": out}

    async def set(self, ctx, plugin: str, key: str, value) -> dict:
        if not ctx.caller.owner:
            raise KitError("denied: only an owner changes settings")
        spec = next((s for p, k, s in self._specs(plugin) if k == key), None)
        if spec is None:
            raise KitError(f"no hub setting {plugin}.{key}")
        live = self.hub.plugins.settings.setdefault(plugin, {})
        if value is None:
            self.stored.get(plugin, {}).pop(key, None)
            if key in self.base.get(plugin, {}):
                live[key] = copy.deepcopy(self.base[plugin][key])
            else:
                live.pop(key, None)
        else:
            errors = list(Draft202012Validator(compile_fields({key: setting_spec(spec)})).iter_errors({key: value}))
            if errors:
                raise KitError(f"bad value: {errors[0].message}")
            self.stored.setdefault(plugin, {})[key] = value
            live[key] = copy.deepcopy(value)
        self.stored = {p: v for p, v in self.stored.items() if v}
        _write(self.path, json.dumps(self.stored, indent=1))
        return {"plugin": plugin, "key": key, "value": live.get(key, spec.get("default"))}


# -- helpers --------------------------------------------------------------------

def _patterns(items) -> list:
    if items is None:
        return []
    if not all(isinstance(i, str) and i and len(i) <= 200 for i in items) or len(items) > 100:
        raise KitError("bad args: caps and events are lists of patterns")
    return list(items)


def _check_sizes(html: str, note: str) -> None:
    if len(html.encode("utf-8")) > MAX_HTML:
        raise KitError(f"bad args: html is over {MAX_HTML // 1024} KiB")
    if len(note.encode("utf-8")) > MAX_NOTE:
        raise KitError(f"bad args: note is over {MAX_NOTE // 1024} KiB")


def _author(ctx) -> dict:
    return {"person": ctx.caller.person, "agent": ctx.caller.agent}


def _history(ctx, change: str) -> dict:
    c = ctx.caller
    by = f"{c.agent} for {c.person}" if c.agent and c.person else (c.agent or c.person or "?")
    return {"at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "by": by, "change": change.strip()}


def _read_json(path: Path) -> dict:
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
