"""Keep the knowledge base's copy of the user manual in step with the bundle.

The copy is a contents page ("Rook manual", slug ``rook-manual``, or
``rook-manual-2``... when someone else's page has that slug) with one child
page per manual page (slug ``manual-<id>``), in the primary band, written by
the system actor ``system:rook-manual``, tagged ``rook-manual`` and
``rook-<version>`` and verified against the bundled file (one ``file``
evidence link), so the pages do not crowd the operator's review queue. They
carry ``attrs.verified_from``: the knowledge store marks such a page
unverified when anyone else changes its text.

What was written is recorded in ``state.json`` in the plugin's data
directory: per page (and for the contents page, ``root_entry``) the bundled
content hash it came from (``source``) and a fingerprint of what the store
holds after the write (``written``: title, body and tags). Comparing the
bundle with ``source`` says what the update changes; comparing the knowledge
page with ``written`` says whether someone edited it since (tags included),
and such pages, and pages whose slug someone else holds, are only written
when the caller lists them in ``overwrite``. A user's own tags are kept.

Nothing here runs unless the knowledge plugin is loaded: the manual itself
is always readable from the bundle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import bundle as manual_bundle

log = logging.getLogger("rook.hub.plugins.manual")

ACTOR = {"id": "system:rook-manual", "kind": "system", "label": "Rook manual"}
ROOT_TITLE = "Rook manual"
#: The contents page's id in diffs and ``overwrite`` (not a valid page id).
ROOT_ID = "_root"
ROOT_LABEL = "Contents page (Rook manual)"
TAG = "rook-manual"
VERSION_TAG = re.compile(r"^rook-\d")
VERIFIED_FROM = {"actor": ACTOR["id"], "label": "the Rook manual"}
#: Pages written per apply call and kept in the event list.
HISTORY_MAX = 20


def _now() -> float:
    return time.time()


def fingerprint(r: dict) -> str:
    """What counts as an edit of a knowledge page: title, body and tags."""
    tags = sorted(str(t) for t in (r.get("attrs") or {}).get("tags") or [])
    return hashlib.sha256("\0".join([r["title"].strip(), r["body"].strip(), *tags])
                          .encode("utf-8")).hexdigest()


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
        return fingerprint(r) != entry.get("written")

    @staticmethod
    def _root_entry(imported: dict) -> dict | None:
        entry = imported.get("root_entry")
        if entry is None and imported.get("root"):       # state.json from before root_entry
            entry = {"record": imported["root"], "slug": imported.get("slug")}
        return entry

    def _root_body(self, b: manual_bundle.Bundle) -> str:
        lines = ["The Rook user manual: how every feature works, in plain language. "
                 "Rook keeps this copy up to date when it is updated (you are asked first).",
                 "", "## Pages", ""]
        for p in b.pages:
            note = " (deprecated)" if p.deprecated else " (preview)" if p.preview else ""
            lines.append(f"- [[{p.slug}]] {p.title}{note}")
        return "\n".join(lines)

    def _root_source(self, b: manual_bundle.Bundle) -> str:
        return manual_bundle.content_hash(ROOT_TITLE, self._root_body(b))

    @staticmethod
    def _band(service, imported: dict) -> str | None:
        band = imported.get("band")
        if band is None and service is not None:
            try:
                band = service.band(None)
            except ValueError:
                band = None
        return band

    # -- the diff ---------------------------------------------------------------
    def diff(self, service=None) -> dict:
        """Pages ``added``, ``changed`` and ``removed`` relative to the
        imported copy (everything is ``added`` when nothing was imported).
        ``local_edit`` marks pages someone changed in the knowledge base (or,
        for an added page, a page of someone else's that has its slug). The
        contents page shows as ``_root`` when its list of pages changed."""
        b = self.bundle()
        imported = self.state().get("imported") or {}
        pages = imported.get("pages") or {}
        band = self._band(service, imported)
        known = service is not None and bool(band)
        out: dict = {"added": [], "changed": [], "removed": [], "unchanged": 0}
        for p in b.pages:
            entry = pages.get(p.id)
            item = {"id": p.id, "title": p.title, "deprecated": p.deprecated, "preview": p.preview}
            if entry is None:
                taken = self._record(service, band, p.slug) if known else None
                out["added"].append({**item, "local_edit": taken is not None})
            elif entry.get("source") != p.hash:
                out["changed"].append({**item, "was_deprecated": bool(entry.get("deprecated")),
                                       "local_edit": known and self._edited(service, band, entry)})
            else:
                out["unchanged"] += 1
        root = self._root_entry(imported)
        if root is not None and root.get("source") != self._root_source(b):
            out["changed"].append({"id": ROOT_ID, "title": ROOT_LABEL, "deprecated": False,
                                   "preview": False, "was_deprecated": False,
                                   "local_edit": known and self._edited(service, band, root)})
        live = {p.id for p in b.pages}
        for pid, entry in sorted(pages.items()):
            if pid not in live:
                out["removed"].append({"id": pid, "title": entry.get("title", pid),
                                       "local_edit": known and self._edited(service, band, entry)})
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

    def _attrs(self, page: manual_bundle.Page | None = None, existing: dict | None = None) -> dict:
        """Our attrs; an existing page's own tags (not ours) are kept."""
        tags = [TAG, "rook-" + self.version] + (["preview"] if page is not None and page.preview else [])
        if existing is not None:
            tags += [t for t in existing["attrs"].get("tags") or []
                     if t not in tags and t != TAG and not VERSION_TAG.match(str(t)) and t != "preview"]
        attrs: dict = {"knowledge_kind": "procedure" if page else "summary", "tags": tags,
                       "verified_from": dict(VERIFIED_FROM)}
        attrs["manual"] = ({"page": page.id, "version": self.version, "since": page.since,
                            "deprecated": page.deprecated, "preview": page.preview, "sha256": page.hash}
                           if page else {"version": self.version})
        return attrs

    async def _verify(self, service, band: str, rid: str, page_file: str, req: str) -> dict:
        """Link the bundled file as evidence (once) and mark the page verified."""
        ref = "docs/manual/" + page_file
        with service.store.db(False) as db:
            linked = any(l["kind"] == "file" and l["ref"] == ref and l["relation"] == "evidence"
                         for l in service.store._live_links(db, rid))
        if not linked:
            await self._write(service, "link", band, rid,
                              {"kind": "file", "ref": ref, "relation": "evidence",
                               "note": "the Rook user manual, bundled with Rook"}, req + ":evidence")
        r = self._record(service, band, rid)
        if r["attrs"].get("verification") == "verified":
            return r
        return await self._write(service, "update", band, rid,
                                 {"revision": r["revision"],
                                  "patch": {"attrs": {"verification": "verified"}}}, req + ":verify")

    async def _upsert(self, service, band: str, parent: str | None, title: str, body: str,
                      slug: str, page: manual_bundle.Page | None, file: str, req: str,
                      existing: dict | None) -> dict:
        attrs = self._attrs(page, existing)
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
        r = await self._verify(service, band, r["id"], file, req)
        return self._record(service, band, r["id"])

    def _entry(self, r: dict, source: str, **extra: Any) -> dict:
        return {"record": r["id"], "slug": r["slug"], "source": source, "written": fingerprint(r),
                "revision": r["revision"], **extra}

    def _free_root_slug(self, service, band: str) -> str:
        """``rook-manual``, or the first free ``rook-manual-N``: a page of
        someone else's with our slug is never adopted."""
        slug, n = manual_bundle.ROOT_SLUG, 2
        while self._record(service, band, slug) is not None:
            slug, n = f"{manual_bundle.ROOT_SLUG}-{n}", n + 1
        return slug

    async def _apply_root(self, service, band: str, b: manual_bundle.Bundle, imported: dict,
                          overwrite: set, stamp: str, kept: list) -> tuple[dict | None, dict]:
        """(contents page record or None, its root_entry)."""
        entry = self._root_entry(imported)
        source = self._root_source(b)
        body = self._root_body(b)
        rec = self._record(service, band, entry.get("record")) if entry else None
        if entry is None or rec is None:
            rec = await self._upsert(service, band, None, ROOT_TITLE, body, self._free_root_slug(service, band),
                                     None, manual_bundle.MANIFEST, stamp + ":root", None)
            return rec, self._entry(rec, source)
        if entry.get("source") == source:
            return rec, entry            # unchanged upstream (an operator's edit stays)
        if self._edited(service, band, entry) and ROOT_ID not in overwrite:
            kept.append({"id": ROOT_ID, "why": "edited in the knowledge base"})
            return rec, {**entry, "source": source}
        rec = await self._upsert(service, band, None, ROOT_TITLE, body, rec["slug"], None,
                                 manual_bundle.MANIFEST, stamp + ":root", rec)
        return rec, self._entry(rec, source)

    async def apply(self, service, *, overwrite: set | list | tuple = (), reason: str = "update",
                    who: str = "") -> dict:
        """Bring the knowledge copy up to the bundle. Pages edited in the
        knowledge base (and slugs someone else holds) are skipped unless their
        id is in ``overwrite`` (``_root`` for the contents page). Returns
        ``{written, archived, kept, errors}``."""
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

        root, root_entry = await self._apply_root(service, band, b, imported, overwrite, stamp, kept)
        parent = root["id"] if root is not None else None

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
                r = await self._upsert(service, band, parent, p.title, p.body, p.slug, p, p.file,
                                       f"{stamp}:{p.id}", existing)
                pages[p.id] = self._entry(r, p.hash, title=p.title, deprecated=p.deprecated)
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
                             "at": _now(), "band": band, "root": root_entry.get("record"),
                             "slug": root_entry.get("slug"), "root_entry": root_entry, "pages": pages}
        state.pop("declined", None)
        state.pop("snoozed_until", None)
        result = {"written": written, "archived": archived, "kept": kept, "errors": errors,
                  "root": root_entry.get("slug")}
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
        was imported (``service`` given), else the bundle's."""
        b = self.bundle()
        imported = self.state().get("imported") or {}
        entries = imported.get("pages") or {}
        band = imported.get("band")
        out = []
        for p in b.pages:
            row = {"id": p.id, "title": p.title, "deprecated": p.deprecated, "preview": p.preview,
                   "since": p.since, "source": "bundle"}
            entry = entries.get(p.id)
            if service is not None and band and entry:
                r = self._record(service, band, entry.get("record"))
                if r is not None and r["state"] == "active":
                    row.update(title=r["title"], source="knowledge", slug=r["slug"],
                               edited=fingerprint(r) != entry.get("written"))
            out.append(row)
        return {"pages": out, "version": self.version, "manual_sha256": b.hash,
                "knowledge": service is not None, "imported": bool(imported)}

    def page(self, pid: str, service=None, *, source: str = "auto") -> dict:
        """One page: from the knowledge copy (``source`` auto/knowledge, with
        ``service``) when it exists, else the bundled file."""
        b = self.bundle()
        p = b.page(pid)
        imported = self.state().get("imported") or {}
        entry = (imported.get("pages") or {}).get(pid)
        meta = {"deprecated": bool(p.deprecated) if p else False, "preview": bool(p.preview) if p else False,
                "since": p.since if p else "", "bundled": p is not None}
        if source != "bundle" and service is not None and entry and imported.get("band"):
            r = self._record(service, imported["band"], entry.get("record"))
            if r is not None and r["state"] == "active":
                return {"id": pid, "title": r["title"], "body": r["body"], "source": "knowledge",
                        "slug": r["slug"], "record": r["id"], "updated": r["updated"],
                        "edited": fingerprint(r) != entry.get("written"),
                        "verification": r["attrs"].get("verification"), **meta}
        if p is None:
            raise ValueError(f"No manual page {pid!r}")
        return {"id": p.id, "title": p.title, "body": p.body, "source": "bundle", "file": p.file, **meta}
