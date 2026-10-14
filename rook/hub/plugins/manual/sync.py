"""Keep the knowledge base's copy of the user manual in step with the bundle.

The copy is a folder page ``rook-manual`` ("Rook manual") with one child page
per manual page (slug ``manual-<id>``), in the primary band, written by the
system actor ``system:rook-manual``, tagged ``rook-manual`` and
``rook-<version>`` and verified against the bundled file (a ``file``
evidence link), so the pages do not crowd the operator's review queue.

What was written is recorded in ``state.json`` in the plugin's data
directory: per page the bundled content hash it came from (``source``) and
the hash of what the store holds after the write (``written``). Comparing
the bundle with ``source`` says what the update changes; comparing the
knowledge page with ``written`` says whether someone edited it since, and
such pages are only overwritten when the caller lists them in ``overwrite``.

Nothing here runs unless the knowledge plugin is loaded: the manual itself
is always readable from the bundle.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from . import bundle as manual_bundle

log = logging.getLogger("rook.hub.plugins.manual")

ACTOR = {"id": "system:rook-manual", "kind": "system", "label": "Rook manual"}
ROOT_TITLE = "Rook manual"
TAG = "rook-manual"
#: Pages written per apply call and kept in the event list.
HISTORY_MAX = 20


def _now() -> float:
    return time.time()


class ManualSync:
    """``load``: callable returning the current :class:`bundle.Bundle`;
    ``state_path``: where the import record lives; ``version``: the Rook
    version the bundled manual belongs to (``rook.__version__``); ``build``:
    the build or release label, for display."""

    def __init__(self, load: Callable[[], manual_bundle.Bundle], state_path: Path,
                 version: str, build: str = "") -> None:
        self._load = load
        self.state_path = Path(state_path)
        self.version = version
        self.build = build

    # -- state ---------------------------------------------------------------
    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("manual: unreadable %s; treating the manual as not imported", self.state_path)
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _event(self, state: dict, event: str, **data: Any) -> None:
        state.setdefault("history", []).append({"at": _now(), "event": event, **data})
        state["history"] = state["history"][-HISTORY_MAX:]

    def bundle(self) -> manual_bundle.Bundle:
        return self._load()

    # -- knowledge reads ----------------------------------------------------------
    @staticmethod
    def _record(service, band: str, rid: str | None) -> dict | None:
        if not rid:
            return None
        store = service.store
        with store.db(False) as db:
            row = db.execute("SELECT * FROM records WHERE band=? AND (id=? OR slug=?)",
                             (band, rid, rid)).fetchone()
        return store.record(row) if row is not None else None

    @staticmethod
    def _knowledge_count(service) -> int:
        with service.store.db(False) as db:
            return db.execute("SELECT count(*) FROM records").fetchone()[0]

    def _edited(self, service, band: str, entry: dict) -> bool:
        """Changed in the knowledge base since we wrote it (or archived/gone)."""
        r = self._record(service, band, entry.get("record"))
        if r is None or r["state"] != "active":
            return True
        return manual_bundle.content_hash(r["title"], r["body"]) != entry.get("written")

    # -- the diff ---------------------------------------------------------------
    def diff(self, service=None) -> dict:
        """Pages ``added``, ``changed`` and ``removed`` relative to the
        imported copy (everything is ``added`` when nothing was imported).
        ``local_edit`` marks pages someone changed in the knowledge base."""
        b = self.bundle()
        imported = self.state().get("imported") or {}
        pages = imported.get("pages") or {}
        band = imported.get("band")
        if band is None and service is not None:
            try:
                band = service.band(None)
            except ValueError:
                band = None
        out: dict = {"added": [], "changed": [], "removed": [], "unchanged": 0}
        for p in b.pages:
            entry = pages.get(p.id)
            if entry is None:
                taken = None
                if service is not None and band:
                    taken = self._record(service, band, p.slug)
                out["added"].append({"id": p.id, "title": p.title, "deprecated": p.deprecated,
                                     "local_edit": taken is not None})
            elif entry.get("source") != p.hash:
                out["changed"].append({"id": p.id, "title": p.title, "deprecated": p.deprecated,
                                       "was_deprecated": bool(entry.get("deprecated")),
                                       "local_edit": bool(service is not None and band
                                                          and self._edited(service, band, entry))})
            else:
                out["unchanged"] += 1
        live = {p.id for p in b.pages}
        for pid, entry in sorted(pages.items()):
            if pid not in live:
                out["removed"].append({"id": pid, "title": entry.get("title", pid),
                                       "local_edit": bool(service is not None and band
                                                          and self._edited(service, band, entry))})
        return out

    def status(self, service=None, *, prompt_enabled: bool = True) -> dict:
        """Everything the dashboard's banner and Help page need."""
        b = self.bundle()
        state = self.state()
        imported = state.get("imported")
        available = service is not None
        d = self.diff(service if available else None)
        pending = bool(d["added"] or d["changed"] or d["removed"])
        declined = (state.get("declined") or {}).get("sha256") == b.hash
        snoozed = float(state.get("snoozed_until") or 0) > _now()
        out = {
            "version": self.version, "build": self.build, "manual_sha256": b.hash,
            "pages": len(b.pages), "errors": b.errors,
            "knowledge": "on" if available else "off",
            "imported": ({k: imported.get(k) for k in ("version", "build", "sha256", "at", "band",
                                                        "root", "slug")}
                         if imported else None),
            "diff": {k: d[k] for k in ("added", "changed", "removed", "unchanged")},
            "pending": pending and available,
            "declined": declined, "snoozed_until": state.get("snoozed_until") or None,
            "history": state.get("history", [])[-5:],
        }
        # Ask only when there is a copy to update (or a non-empty knowledge
        # base the seed stayed out of), the knowledge base is on, and the
        # operator has not put this manual off.
        out["prompt"] = bool(prompt_enabled and available and pending and not declined and not snoozed)
        out["fresh"] = not imported
        return out

    # -- writes -----------------------------------------------------------------
    async def _write(self, service, action: str, band: str, rid, data: dict, request: str):
        return await service.dispatch(action, band, "knowledge" if action == "create" else None,
                                      rid, "", data, request, actor=ACTOR)

    def _attrs(self, page: manual_bundle.Page | None = None) -> dict:
        tags = [TAG, "rook-" + self.version]
        attrs: dict = {"knowledge_kind": "procedure" if page else "summary", "tags": tags}
        attrs["manual"] = ({"page": page.id, "version": self.version, "since": page.since,
                            "deprecated": page.deprecated, "sha256": page.hash}
                           if page else {"version": self.version})
        return attrs

    def _root_body(self, b: manual_bundle.Bundle) -> str:
        lines = ["The Rook user manual: how every feature works, in plain language. "
                 "Rook keeps this copy up to date when it is updated (you are asked first).",
                 "", f"Rook version: {self.version}" + (f" ({self.build})" if self.build else ""), "",
                 "## Pages", ""]
        for p in b.pages:
            lines.append(f"- [[{p.slug}]] {p.title}" + (" (deprecated)" if p.deprecated else ""))
        return "\n".join(lines)

    async def _verify(self, service, band: str, rid: str, page_file: str, req: str) -> dict:
        """Link the bundled file as evidence and mark the page verified."""
        await self._write(service, "link", band, rid,
                          {"kind": "file", "ref": "docs/manual/" + page_file, "relation": "evidence",
                           "note": f"Rook manual, Rook {self.version}"}, req + ":evidence")
        r = self._record(service, band, rid)
        if r["attrs"].get("verification") == "verified":
            return r
        return await self._write(service, "update", band, rid,
                                 {"revision": r["revision"],
                                  "patch": {"attrs": {"verification": "verified"}}}, req + ":verify")

    async def _upsert(self, service, band: str, parent: str | None, title: str, body: str,
                      slug: str, attrs: dict, file: str | None, req: str,
                      existing: dict | None) -> dict:
        if existing is None:
            data = {"title": title, "body": body, "slug": slug, "attrs": attrs}
            if parent:
                data["parent"] = parent
            r = await self._write(service, "create", band, None, data, req + ":create")
        else:
            patch: dict = {"title": title, "body": body, "attrs": attrs}
            if existing["state"] != "active":
                patch["state"] = "active"
            if parent and existing.get("parent") != parent:
                patch["parent"] = parent
            r = await self._write(service, "update", band, existing["id"],
                                  {"revision": existing["revision"], "patch": patch}, req + ":update")
        if file:
            r = await self._verify(service, band, r["id"], file, req)
        return self._record(service, band, r["id"])

    async def apply(self, service, *, overwrite: set | list | tuple = (), reason: str = "update",
                    who: str = "") -> dict:
        """Bring the knowledge copy up to the bundle. Pages edited in the
        knowledge base (and slugs someone else took) are skipped unless their
        id is in ``overwrite``. Returns ``{written, archived, kept, errors}``."""
        b = self.bundle()
        if not b.pages:
            raise ValueError("The bundled manual has no pages")
        overwrite = set(overwrite or ())
        state = self.state()
        imported = state.get("imported") or {}
        band = imported.get("band") or service.band(None)
        pages: dict = dict(imported.get("pages") or {})
        stamp = f"manual:{b.hash[:16]}:{int(_now() * 1000)}"
        written, archived, kept, errors = [], [], [], []

        root = self._record(service, band, imported.get("root")) or self._record(service, band,
                                                                                manual_bundle.ROOT_SLUG)
        if root is not None and root["kind"] != "knowledge":
            raise ValueError(f"The slug {manual_bundle.ROOT_SLUG!r} is taken by a {root['kind']}")
        root = await self._upsert(service, band, None, ROOT_TITLE, self._root_body(b),
                                  manual_bundle.ROOT_SLUG, self._attrs(), manual_bundle.MANIFEST,
                                  stamp + ":root", root)

        for p in b.pages:
            entry = pages.get(p.id)
            try:
                if entry is None:
                    taken = self._record(service, band, p.slug)
                    if taken is not None and p.id not in overwrite:
                        kept.append({"id": p.id, "why": "a page with this name already exists"})
                        continue
                    if taken is not None and taken["kind"] != "knowledge":
                        errors.append(f"{p.id}: the slug {p.slug} is taken by a {taken['kind']}")
                        continue
                    existing = taken
                else:
                    if entry.get("source") == p.hash:
                        continue        # unchanged upstream (a local edit stays as it is)
                    if self._edited(service, band, entry) and p.id not in overwrite:
                        kept.append({"id": p.id, "why": "edited in the knowledge base"})
                        pages[p.id] = {**entry, "source": p.hash, "title": p.title,
                                       "deprecated": p.deprecated, "kept_local": True}
                        continue
                    existing = self._record(service, band, entry.get("record"))
                r = await self._upsert(service, band, root["id"], p.title, p.body, p.slug,
                                       self._attrs(p), p.file, f"{stamp}:{p.id}", existing)
                pages[p.id] = {"record": r["id"], "source": p.hash, "title": p.title,
                               "written": manual_bundle.content_hash(r["title"], r["body"]),
                               "revision": r["revision"], "deprecated": p.deprecated}
                written.append(p.id)
            except (ValueError, KeyError, TypeError, PermissionError) as e:
                log.warning("manual: %s not written: %s", p.id, e)
                errors.append(f"{p.id}: {e}")

        live = {p.id for p in b.pages}
        for pid in sorted(set(pages) - live):
            entry = pages[pid]
            try:
                if self._edited(service, band, entry) and pid not in overwrite:
                    kept.append({"id": pid, "why": "edited in the knowledge base; no longer in the manual"})
                    pages.pop(pid)
                    continue
                r = self._record(service, band, entry.get("record"))
                if r is not None and r["state"] != "archived":
                    await self._write(service, "update", band, r["id"],
                                      {"revision": r["revision"], "patch": {"state": "archived"}},
                                      f"{stamp}:{pid}:archive")
                archived.append(pid)
                pages.pop(pid)
            except (ValueError, KeyError, TypeError, PermissionError) as e:
                errors.append(f"{pid}: {e}")

        state = self.state()      # re-read: a snooze may have landed meanwhile
        state["imported"] = {"version": self.version, "build": self.build, "sha256": b.hash,
                             "at": _now(), "band": band, "root": root["id"],
                             "slug": manual_bundle.ROOT_SLUG, "pages": pages}
        state.pop("declined", None)
        state.pop("snoozed_until", None)
        result = {"written": written, "archived": archived, "kept": kept, "errors": errors}
        self._event(state, reason, version=self.version, who=who, written=len(written),
                    archived=len(archived), kept=[k["id"] for k in kept], errors=len(errors))
        self._save(state)
        return result

    async def seed_if_fresh(self, service, *, auto_seed: bool = True) -> dict | None:
        """On a hub that never imported the manual: import it when the
        knowledge base is empty (a new hub). A knowledge base that already
        has pages is left alone; the dashboard asks instead."""
        if not auto_seed or self.state().get("imported"):
            return None
        if self._knowledge_count(service) > 0:
            return None
        return await self.apply(service, reason="seed", who=ACTOR["id"])

    def snooze(self, hours: float = 24.0, who: str = "") -> dict:
        state = self.state()
        state["snoozed_until"] = _now() + max(0.0, float(hours)) * 3600
        self._event(state, "snooze", hours=hours, who=who)
        self._save(state)
        return {"snoozed_until": state["snoozed_until"]}

    def decline(self, who: str = "") -> dict:
        b = self.bundle()
        state = self.state()
        state["declined"] = {"sha256": b.hash, "version": self.version, "at": _now()}
        self._event(state, "decline", version=self.version, who=who)
        self._save(state)
        return {"declined": state["declined"]}

    # -- reading ------------------------------------------------------------------
    def pages(self, service=None) -> dict:
        """The table of contents: the knowledge copy's titles where a page
        was imported, else the bundle's."""
        b = self.bundle()
        imported = self.state().get("imported") or {}
        entries = imported.get("pages") or {}
        band = imported.get("band")
        out = []
        for p in b.pages:
            row = {"id": p.id, "title": p.title, "deprecated": p.deprecated, "since": p.since,
                   "source": "bundle"}
            entry = entries.get(p.id)
            if service is not None and band and entry:
                r = self._record(service, band, entry.get("record"))
                if r is not None and r["state"] == "active":
                    row.update(title=r["title"], source="knowledge", slug=r["slug"],
                               edited=manual_bundle.content_hash(r["title"], r["body"]) != entry.get("written"))
            out.append(row)
        return {"pages": out, "version": self.version, "manual_sha256": b.hash,
                "knowledge": service is not None, "imported": bool(imported)}

    def page(self, pid: str, service=None, *, source: str = "auto") -> dict:
        """One page: from the knowledge copy (``source`` auto/knowledge) when
        it exists, else the bundled file."""
        b = self.bundle()
        p = b.page(pid)
        imported = self.state().get("imported") or {}
        entry = (imported.get("pages") or {}).get(pid)
        if source != "bundle" and service is not None and entry and imported.get("band"):
            r = self._record(service, imported["band"], entry.get("record"))
            if r is not None and r["state"] == "active":
                return {"id": pid, "title": r["title"], "body": r["body"], "source": "knowledge",
                        "slug": r["slug"], "record": r["id"], "updated": r["updated"],
                        "edited": manual_bundle.content_hash(r["title"], r["body"]) != entry.get("written"),
                        "deprecated": bool(p.deprecated) if p else False,
                        "since": p.since if p else "", "bundled": p is not None}
        if p is None:
            raise ValueError(f"No manual page {pid!r}")
        return {"id": p.id, "title": p.title, "body": p.body, "source": "bundle",
                "deprecated": p.deprecated, "since": p.since, "file": p.file, "bundled": True}
