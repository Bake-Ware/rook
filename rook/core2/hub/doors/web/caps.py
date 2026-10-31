"""The hub's builtin caps behind the dashboard (castling-dashboard): pages,
hub, approval, journal and setting, declared in ``builtin/<plugin>/plugin.yaml``
and run in-process on the hub node like any hub cap (dispatch, rights,
journal). :func:`install` registers them at hub start.

Pages are a content kind: a plugin ships them in its manifest (``pages:``),
and agents make them with ``pages.create``. An agent-made page lives in
``<data>/pages/<name>/`` as ``page.html``, ``meta.json`` and ``NOTE.md`` (why
it exists and its history: the provenance note shown beside it). Archiving
is reversible and kept in ``<data>/pages.json``.

Components are the canned tier (manifest ``components:``): small cap-backed
scripts a package ships, which any agent (a weak local model too) arranges
into a page by giving ``layout`` instead of ``html``. Each person's start
page (or the hub's, set by an agent or job acting alone) is kept in
``pages.json`` too. With the workbench installed, each page an agent makes
is built under a task linked to the page (castling-dashboard, provenance).
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field
from html import escape as html_escape
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
MAX_LAYOUT = 50
EVERYONE = "*"                       # the hub's own start page (pages.json)


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
    nav: bool = True             # in the sidebar; every page opens by its address either way
    layout: list | None = None   # arranged from components instead of its own html

    def view(self) -> dict:
        return {"page": self.id, "title": self.title, "caps": list(self.caps), "events": list(self.events),
                "sizes": list(self.sizes), "section": self.section, "made": self.made is not None,
                "nav": self.nav}


@dataclass
class Component:
    id: str                  # <plugin>/<name>
    title: str
    doc: str
    file: Path
    props: dict
    caps: list

    def view(self) -> dict:
        return {"component": self.id, "title": self.title, "doc": self.doc, "props": self.props,
                "caps": list(self.caps)}


class Pages:
    """Every page on the hub site: the hub plugins' manifests plus the pages
    agents made, and which of them are archived."""

    def __init__(self, hub):
        self.hub = hub
        self.folder = Path(hub.data) / "pages"
        self._state_path = Path(hub.data) / "pages.json"
        state = _read_json(self._state_path)
        self.archived: dict = state.get("archived", {})
        self.starts: dict = state.get("start", {})          # person:<name> or * -> page

    def all(self) -> dict[str, Page]:
        out = {}
        for p in list(self.hub.plugins.plugins.values()):
            for name, spec in (p.manifest.get("pages") or {}).items():
                if p.folder is None:
                    continue
                out[f"{p.name}/{name}"] = Page(
                    f"{p.name}/{name}", spec.get("title") or name, Path(p.folder) / spec["file"],
                    list(spec.get("caps") or ()), list(spec.get("events") or ()), list(spec.get("sizes") or ()),
                    (spec.get("nav") or {}).get("section") or p.manifest.get("title") or p.name,
                    nav="nav" in spec)
        made = sorted(self.folder.glob("*/meta.json")) if self.folder.is_dir() else ()
        components = self.components() if made else {}
        for meta_path in made:
            m = _read_json(meta_path)
            name = meta_path.parent.name
            if not NAME.fullmatch(name) or not m:
                continue
            pid = f"{MADE}/{name}"
            layout = m.get("layout") if isinstance(m.get("layout"), list) else None
            caps = list(m.get("caps") or ())
            for item in layout or ():          # a layout may call what its components call, as they are now
                c = components.get(item.get("component"))
                caps += [x for x in (c.caps if c else ()) if x not in caps]
            out[pid] = Page(pid, m.get("title") or name, meta_path.parent / "page.html", caps,
                            list(m.get("events") or ()), ["page"], m.get("section") or "Pages",
                            {k: m.get(k) for k in ("authors", "created", "task")}, layout=layout)
        return out

    def components(self) -> dict[str, Component]:
        out = {}
        for p in list(self.hub.plugins.plugins.values()):
            for name, spec in (p.manifest.get("components") or {}).items():
                if p.folder is None:
                    continue
                cid = f"{p.name}/{name}"
                out[cid] = Component(cid, spec.get("title") or name, spec.get("doc") or "",
                                     Path(p.folder) / spec["file"], dict(spec.get("props") or {}),
                                     list(spec.get("caps") or ()))
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
        """The page's file, which must stay inside its own folder; a layout
        page is put together from its components."""
        if page.layout is not None:
            return self.arrange(page)
        return _inside(page.file, page.file.parent if page.made else _plugin_folder(self.hub, page.id), page.id)

    def arrange(self, page: Page) -> str:
        """A layout page's document: one card per item, then each component's
        script (``rook.component(fn)`` registers it), then the mount."""
        components = self.components()
        cards, scripts, used = [], [], []
        for i, item in enumerate(page.layout):
            c = components.get(item.get("component"))
            title = item.get("title") if isinstance(item.get("title"), str) else (c.title if c else "")
            wide = " wide" if item.get("wide") else ""
            cards.append(f'<section class="card part{wide}"><h3>{_esc(title)}</h3><div id="part-{i}"></div></section>')
            if c is not None and c.id not in used:
                used.append(c.id)
                try:
                    code = _inside(c.file, _plugin_folder(self.hub, c.id), c.id)
                except KitError as e:
                    code = f"throw new Error({json.dumps(e.message)});"
                scripts.append(f"<script>rook._as({_js(c.id)});\n{_script(code)}\n</script>")
        parts = [{"component": item.get("component"), "props": item.get("props") or {}} for item in page.layout]
        return ("<!doctype html><html><head><meta charset=utf-8>"
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{_esc(page.title)}</title></head><body>"
                f'<div class="layout">{"".join(cards)}</div>{"".join(scripts)}'
                f"<script>rook._mount({_js(parts)});</script></body></html>")

    def note(self, page: Page) -> str | None:
        path = page.file.parent / "NOTE.md"
        return path.read_text(encoding="utf-8") if page.made and path.is_file() else None

    def start(self, person: str | None) -> str | None:
        """The start page: the person's own, else the hub's; an archived or
        gone one doesn't count."""
        live = self.all()
        for who in (person, EVERYONE):
            pid = self.starts.get(who) if who else None
            if pid in live and pid not in self.archived:
                return pid
        return None

    def save_state(self) -> None:
        _write(self._state_path, json.dumps({"archived": self.archived, "start": self.starts}, indent=1))


def dock_agent(hub) -> str | None:
    """The agent in the site's chat dock: the ``pages.dock`` setting, when
    the agents package is installed."""
    name = hub.plugins.settings.get("pages", {}).get("dock", "house")
    return name if name and "agents" in hub.plugins.plugins else None


def _plugin_folder(hub, pid: str) -> Path:
    return Path(hub.plugins.plugins[pid.partition("/")[0]].folder)


def _inside(file: Path, folder: Path, what: str) -> str:
    root, path = folder.resolve(), file.resolve()
    if root not in path.parents or not path.is_file():
        raise KitError(f"{what}: its file is missing")
    return path.read_text(encoding="utf-8")


def _esc(text: str) -> str:
    return html_escape(text or "", quote=True)


def _js(value) -> str:
    """JSON safe inside a <script>."""
    return json.dumps(value).replace("<", "\\u003c")


def _script(code: str) -> str:
    return re.sub(r"</(script)", r"<\\/\1", code, flags=re.I)


# -- install --------------------------------------------------------------------

def install(hub) -> None:
    """Register the builtin plugins on the hub node and load stored settings."""
    hub.pages = Pages(hub)
    settings = Settings(hub)
    h = _Handlers(hub, settings)
    table = {
        "pages": {"pages.list": h.pages_list, "pages.view": h.pages_view, "pages.get": h.pages_get,
                  "pages.create": h.pages_create, "pages.update": h.pages_update,
                  "pages.archive": h.pages_archive, "pages.restore": h.pages_restore,
                  "pages.components": h.pages_components, "pages.site": h.pages_site,
                  "pages.start": h.pages_start},
        "hub": {"hub.status": h.hub_status, "hub.workers": h.hub_workers, "hub.manifests": h.hub_manifests,
                "hub.door": h.hub_door},
        "approval": {"approval.list": h.approval_list, "approval.decide": h.approval_decide,
                     "approval.ask": h.approval_ask, "approval.get": h.approval_get},
        "journal": {"journal.search": h.journal_search},
        "setting": {"setting.list": settings.list, "setting.set": settings.set},
        "llm": {"llm.providers": h.llm_providers, "llm.models": h.llm_models, "llm.status": h.llm_status},
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
            out.update(made_by=p.made, note=pages.note(p), layout=p.layout)
        else:
            plugin = self.hub.plugins.plugins[page.partition("/")[0]].manifest
            out["plugin"] = {"name": plugin["plugin"], "version": str(plugin["version"]),
                             "builtin": bool(plugin.get("builtin"))}
        return out

    async def pages_create(self, ctx, name: str, title: str, note: str, html: str | None = None,
                           layout: list | None = None, caps: list | None = None, events: list | None = None,
                           section: str | None = None, task: str | None = None) -> dict:
        if not NAME.fullmatch(name):
            raise KitError("bad args: name is lowercase letters, digits, - and _ (64 at most)")
        folder = self.hub.pages.folder / name
        if (folder / "meta.json").exists():
            raise KitError(f"{MADE}/{name} exists: change it with pages.update")
        if (html is None) == (layout is None):
            raise KitError("bad args: give the page's html or a layout of components (pages.components lists them)")
        _check_sizes(html or "", note)
        meta = {"title": title, "caps": _patterns(caps), "events": _patterns(events), "section": section or "Pages",
                "layout": self._layout(layout), "authors": [_author(ctx)], "created": time.time(), "task": task,
                "note": note, "history": [_history(ctx, "made the page")]}
        folder.mkdir(parents=True, exist_ok=True)
        _write(folder / "page.html", html or "")
        self._save(folder, meta)
        pid = f"{MADE}/{name}"
        out = {"page": pid}
        if self._workbench():
            out.update(await self._built_under(ctx, pid, meta))
            if out.get("task") != task:
                self._save(folder, {**meta, "task": out.get("task")})
        return out

    async def pages_update(self, ctx, page: str, change: str, title: str | None = None, html: str | None = None,
                           layout: list | None = None, note: str | None = None, caps: list | None = None,
                           events: list | None = None, section: str | None = None) -> dict:
        p = self.hub.pages.get(page)
        if not p.made:
            raise KitError(f"{page} comes with a plugin: change the plugin instead")
        if html is not None and layout is not None:
            raise KitError("bad args: give html or a layout, not both")
        folder = p.file.parent
        meta = _read_json(folder / "meta.json")
        _check_sizes(html or "", note or "")
        for key, value in (("title", title), ("note", note), ("section", section)):
            if value is not None:
                meta[key] = value
        for key, value in (("caps", caps), ("events", events)):
            if value is not None:
                meta[key] = _patterns(value)
        if layout is not None:
            meta["layout"] = self._layout(layout)
        meta.setdefault("history", []).append(_history(ctx, change))
        if _author(ctx) not in meta.setdefault("authors", []):
            meta["authors"].append(_author(ctx))       # the page never does more than anyone who wrote it
        if html is not None:
            meta["layout"] = None
            _write(folder / "page.html", html)
        self._save(folder, meta)
        out = {"page": page}
        if meta.get("task") and self._workbench():       # the build record hears every change
            out.update(await self._task_call(ctx, "task.comment", id=meta["task"],
                                             text=f"Changed the page {page}: {change.strip()}"))
        return out

    def _layout(self, layout: list | None) -> list | None:
        """A layout's items, checked against the components as they are now."""
        if layout is None:
            return None
        if not isinstance(layout, list) or not 0 < len(layout) <= MAX_LAYOUT:
            raise KitError(f"bad args: a layout is a list of 1 to {MAX_LAYOUT} components")
        components = self.hub.pages.components()
        out = []
        for i, item in enumerate(layout):
            if not isinstance(item, dict) or not isinstance(item.get("component"), str):
                raise KitError(f"bad args: layout[{i}] needs a component (pages.components lists them)")
            c = components.get(item["component"])
            if c is None:
                raise KitError(f"bad args: layout[{i}]: no component {item['component']} (pages.components lists them)")
            extra = set(item) - {"component", "props", "title", "wide"}
            if extra:
                raise KitError(f"bad args: layout[{i}]: unknown {', '.join(sorted(extra))}")
            props = item.get("props") or {}
            errors = list(Draft202012Validator(compile_fields(c.props)).iter_errors(props)) \
                if isinstance(props, dict) else ["props is not a map"]
            if errors:
                raise KitError(f"bad args: layout[{i}] props: {getattr(errors[0], 'message', errors[0])}")
            if not isinstance(item.get("title", ""), str) or not isinstance(item.get("wide", False), bool):
                raise KitError(f"bad args: layout[{i}]: title is text, wide is true or false")
            out.append({k: item[k] for k in ("component", "props", "title", "wide") if k in item})
        return out

    # -- the workbench: a page build is a task --------------------------------------

    def _workbench(self) -> bool:
        return "task" in self.hub.plugins.plugins

    async def _built_under(self, ctx, pid: str, meta: dict) -> dict:
        """Link the page to the task it was built under; with none given, make
        that task (the person reviews the page, then it closes)."""
        task = meta.get("task")
        if not task:
            made = await self._task_call(ctx, "task.create", title=f"Page: {meta['title']}", body=meta["note"],
                                         state="in_progress", assignee=ctx.caller.person or "",
                                         criteria=["The person has looked at the page and it shows what they asked for."])
            task = (made.pop("result", None) or {}).get("id")
            if not task:
                return {"task": None, **made}
        linked = await self._task_call(ctx, "task.link", id=task, kind="page", ref=pid, relation="produced",
                                       note=meta["title"])
        linked.pop("result", None)
        return {"task": task, **linked}

    async def _task_call(self, ctx, cap: str, **args) -> dict:
        """A workbench call as the caller. A refusal never undoes the page:
        it comes back as ``task_error``."""
        args = {k: v for k, v in args.items() if v not in ("", None)}
        reply = await self.hub.call_as(ctx.caller, cap, args)
        if reply.get("ok"):
            return {"result": reply.get("result")}
        return {"task_error": f"{cap}: {reply.get('error') or 'failed'}"}

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

    async def pages_components(self, ctx) -> dict:
        return {"components": [c.view() for _, c in sorted(self.hub.pages.components().items())]}

    async def pages_site(self, ctx) -> dict:
        """What the shell needs: the caller's start page and the agent in
        the dock (with the agents package)."""
        pages = self.hub.pages
        return {"person": ctx.caller.person, "start": pages.start(ctx.caller.person), "dock": dock_agent(self.hub),
                "workbench": self._workbench()}

    async def pages_start(self, ctx, page: str | None = None) -> dict:
        pages = self.hub.pages
        if page:
            pages.get(page)
        who = ctx.caller.person or EVERYONE       # an agent or a job alone sets the hub's own
        if page:
            pages.starts[who] = page
        else:
            pages.starts.pop(who, None)
        pages.save_state()
        return {"start": pages.start(ctx.caller.person), "for": who}

    # -- hub, approvals, journal --------------------------------------------------

    async def hub_status(self, ctx) -> dict:
        hub = self.hub
        out = {"hub": hub.hub_id, "node": hub.node, "band": hub.band, "core": CORE_VERSION,
               "doors": [d.name for d in hub.doors], "closed_doors": sorted(hub.dispatcher.closed_doors),
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

    async def hub_door(self, ctx, door: str, open: bool) -> dict:
        out = await self.hub.set_door(_who(ctx), door, open)
        if not out["ok"]:
            raise KitError(out["error"])
        return {"closed": out["closed"]}

    async def hub_workers(self, ctx) -> dict:
        return {"workers": (await self.hub.workers_view(_who(ctx)))["workers"]}

    async def hub_manifests(self, ctx, section: str | None = None) -> dict:
        """Hub plugins first, then what workers announce. ``folder`` (where the
        hub holds the plugin: loaded, or in its library) only for owners and
        the hub's own components, which run code from there (jobs)."""
        hub = self.hub
        found = {p.name: (p.manifest, p.folder) for p in hub.plugins.plugins.values()}
        for k in sorted(hub.known.values(), key=lambda k: k.name):
            for e in k.manifests:
                m = hub._norm.get(e.get("hash"))
                if m is not None and m["plugin"] not in found:
                    found[m["plugin"]] = (m, None)
        show_folder = ctx.caller.owner or (ctx.agent or "").startswith("system:")
        out = []
        for name, (m, folder) in sorted(found.items()):
            if section and not m.get(section):
                continue
            item = {"plugin": name, "version": str(m["version"])}
            item.update({section: m[section]} if section else {"manifest": m})
            folder = folder or (hub._library_folder(name) if show_folder else None)
            if folder and show_folder:
                item["folder"] = str(folder)
            out.append(item)
        return {"manifests": out}

    async def approval_list(self, ctx) -> dict:
        return {"holds": (await self.hub.holds_view(_who(ctx)))["holds"]}

    async def approval_decide(self, ctx, hold: str, decision: str | None = None, answer: str | None = None) -> dict:
        out = await self.hub.decide(_who(ctx), hold, decision, answer=answer)
        if not out.get("ok") and "id" not in out:      # unknown hold, not theirs to answer
            raise KitError(out["error"])
        return out          # a released call's own reply, ok or not

    async def approval_ask(self, ctx, question: str, options: list | None = None, wait: int = 0) -> dict:
        out = await self.hub.asks.ask(_who(ctx), ctx.caller.why, question, options, wait)
        if not out.pop("ok"):
            raise KitError(out["error"])
        return out

    async def approval_get(self, ctx, hold: str) -> dict:
        out = self.hub.asks.get(_who(ctx), hold)
        if not out.pop("ok"):
            raise KitError(out["error"])
        return out

    async def journal_search(self, ctx, limit: int = 50, **filters) -> dict:
        out = await self.hub.journal_view(_who(ctx), {**filters, "limit": limit})
        if not out.get("ok"):
            raise KitError(out["error"])
        return {"entries": out["entries"]}


    # -- llm: the llm.complete providers ------------------------------------------

    async def llm_providers(self, ctx) -> dict:
        contracts = self.hub.contracts
        caps = contracts.providers("llm.complete")
        chosen = contracts.choose("llm.complete") if caps else None
        return {"chosen": chosen and _plugin_of(chosen),
                "providers": [{"provider": _plugin_of(c), "cap": c, "chosen": c == chosen} for c in caps]}

    async def llm_models(self, ctx, provider: str | None = None) -> dict:
        plugin = self._llm_provider(provider)
        return {"provider": plugin, **await self._llm_call(ctx, plugin)}

    async def llm_status(self, ctx, provider: str | None = None) -> dict:
        plugins = [self._llm_provider(provider)] if provider else \
            [_plugin_of(c) for c in self.hub.contracts.providers("llm.complete")]
        out = []
        for plugin in plugins:
            t = time.monotonic()
            try:
                await self._llm_call(ctx, plugin)
                row = {"provider": plugin, "ok": True}
            except KitError as e:
                row = {"provider": plugin, "ok": False, "error": e.message}
            out.append({**row, "ms": round((time.monotonic() - t) * 1000)})
        return {"providers": out}

    def _llm_provider(self, provider: str | None) -> str:
        contracts = self.hub.contracts
        if not provider:
            return _plugin_of(contracts.choose("llm.complete"))
        if provider not in {_plugin_of(c) for c in contracts.providers("llm.complete")}:
            raise KitError(f"{provider} doesn't provide llm.complete (llm.providers lists them)")
        return provider

    async def _llm_call(self, ctx, plugin: str) -> dict:
        reply = await self.hub.call_as(ctx.caller, f"{plugin}.models", {})
        if not reply.get("ok"):
            raise KitError(str(reply.get("error") or "failed"))
        return reply.get("result") or {}


def _plugin_of(cap: str) -> str:
    return cap.partition(".")[0]


# -- settings -------------------------------------------------------------------

class Settings:
    """Hub-side settings (``where: hub``) of the hub's plugins, and each
    person's own (``where: person``: a notification handle, a preferred
    voice). Values set with ``setting.set`` are kept in ``<data>/settings.json``
    and ``<data>/person-settings.json`` and win over the config file; the
    plugins read them on their next call. This is also the hub plugins'
    view of people (``ctx.settings_for``, ``ctx.owners``), of the agents
    they run (``ctx.run_agent``, ``ctx.turn_token``) and of who provides a
    contract (``ctx.providers``)."""

    def __init__(self, hub):
        self.hub = hub
        self.path = Path(hub.data) / "settings.json"
        self.people_path = Path(hub.data) / "person-settings.json"
        self.base = copy.deepcopy(hub.cfg.settings)        # the config file's values
        self.stored: dict = _read_json(self.path)
        self.personal: dict = _read_json(self.people_path)  # person:<name> -> {plugin: {key: value}}
        for plugin, values in self.stored.items():
            hub.plugins.settings.setdefault(plugin, {}).update(values)
        hub.plugins.people = self

    # -- the people view hub plugins get --------------------------------------

    def owners(self) -> list:
        return [f"person:{u.name}" for u in self.hub.identities.users.values() if u.preset == "owner"]

    def settings(self, person: str) -> dict:
        return self.personal.get(person) or {}

    def providers(self, contract: str) -> list:
        """ctx.providers: a contract's provider caps, first installed first."""
        return self.hub.contracts.providers(contract)

    # Rook-run agents' identities and turn tokens (ctx.run_agent, ctx.turn_token).
    def run_agent(self, plugin: str, name: str, grants: list | None) -> None:
        self.hub.run_agent(plugin, name, grants)

    def turn_token(self, plugin: str, person: str | None, name: str, seconds: float, tainted: bool = False) -> str:
        return self.hub.turn_token(plugin, person, name, seconds, tainted)

    def end_turn_token(self, token: str) -> None:
        self.hub.end_turn_token(token)

    # -- caps -------------------------------------------------------------------

    def _specs(self, plugin: str | None = None, where: str = "hub"):
        for p in sorted(list(self.hub.plugins.plugins.values()), key=lambda p: p.name):
            if plugin and p.name != plugin:
                continue
            for key, spec in (p.manifest.get("settings") or {}).items():
                if spec.get("where") == where:
                    yield p.name, key, spec

    async def list(self, ctx, plugin: str | None = None, person: str | None = None) -> dict:
        out = []
        for name, key, spec in self._specs(plugin):
            values = self.hub.plugins.settings.get(name, {})
            source = "set" if key in self.stored.get(name, {}) else \
                "config" if key in self.base.get(name, {}) else "default"
            out.append({"plugin": name, "key": key, "type": spec.get("type"), "doc": spec.get("doc", ""),
                        "default": spec.get("default"), "secret": bool(spec.get("secret")),
                        "value": values.get(key, spec.get("default")), "source": source})
        who = self._person(ctx, person)
        if who and who != ctx.caller.person and not ctx.caller.owner:
            raise KitError("denied: only an owner reads someone else's settings")
        if who:
            mine = self.settings(who)
            for name, key, spec in self._specs(plugin, "person"):
                have = key in mine.get(name, {})
                out.append({"plugin": name, "key": key, "type": spec.get("type"), "doc": spec.get("doc", ""),
                            "default": spec.get("default"), "secret": bool(spec.get("secret")),
                            "value": mine[name][key] if have else spec.get("default"),
                            "source": "set" if have else "default", "where": "person", "person": who})
        return {"settings": out}

    async def set(self, ctx, plugin: str, key: str, value, person: str | None = None) -> dict:
        spec = next((s for p, k, s in self._specs(plugin, "person") if k == key), None)
        if spec is not None:
            return self._set_personal(ctx, plugin, key, value, spec, person)
        if person:
            raise KitError(f"{plugin}.{key} is not a per-person setting")
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
            _check_value(key, spec, value)
            self.stored.setdefault(plugin, {})[key] = value
            live[key] = copy.deepcopy(value)
        self.stored = {p: v for p, v in self.stored.items() if v}
        _write(self.path, json.dumps(self.stored, indent=1))
        return {"plugin": plugin, "key": key, "value": live.get(key, spec.get("default"))}

    def _set_personal(self, ctx, plugin: str, key: str, value, spec: dict, person: str | None) -> dict:
        who = self._person(ctx, person)
        if not who:
            raise KitError("a per-person setting needs a person: give person")
        if who != ctx.caller.person and not ctx.caller.owner:
            raise KitError("denied: only an owner changes someone else's settings")
        if spec.get("secret"):      # a vault name a person picks could name the band's own secrets
            raise KitError(f"{plugin}.{key} is a per-person secret: not supported yet (vault names are the band's, owners only)")
        mine = self.personal.setdefault(who, {}).setdefault(plugin, {})
        if value is None:
            mine.pop(key, None)
        else:
            _check_value(key, spec, value)
            mine[key] = value
        self.personal = {w: {p: v for p, v in ps.items() if v} for w, ps in self.personal.items()}
        self.personal = {w: ps for w, ps in self.personal.items() if ps}
        _write(self.people_path, json.dumps(self.personal, indent=1))
        return {"plugin": plugin, "key": key, "person": who, "value": mine.get(key, spec.get("default"))}

    def _person(self, ctx, person: str | None) -> str | None:
        """``person`` as ``person:<name>`` (a known one), else the caller."""
        if not person:
            return ctx.caller.person
        who = person if person.startswith("person:") else f"person:{person}"
        if self.hub.identities.user(who) is None:
            raise KitError(f"no person {person} on this hub")
        return who


def _check_value(key: str, spec: dict, value) -> None:
    errors = list(Draft202012Validator(compile_fields({key: setting_spec(spec)})).iter_errors({key: value}))
    if errors:
        raise KitError(f"bad value: {errors[0].message}")


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
