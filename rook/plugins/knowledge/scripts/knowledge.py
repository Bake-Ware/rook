"""knowledge.get/create/update/link: the wiki verbs that need more than the
records kit gives (slugs, [[links]] both ways, sources with who and when)."""

import re
import time

WIKI = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,79})\]\]")
CITE = re.compile(r"\[\^([A-Za-z0-9_-]{1,32})\](?!:)")
DEFINED = re.compile(r"^\[\^([A-Za-z0-9_-]{1,32})\]:", re.M)
TRIES = 8
MAX_BACKLINKS = 200


def _page(ctx, ref):
    """A page by slug or id (archived ones too: their slugs stay taken), or None."""
    for archived in (False, True):
        rows = ctx.records.knowledge.list(where={"slug": ref}, archived=archived, limit=1)
        if rows:
            return rows[0]
    try:
        return ctx.records.knowledge.get(ref)
    except ctx.Error:
        return None


def _need(ctx, ref):
    p = _page(ctx, ref)
    if p is None:
        raise ctx.Error(f"no knowledge page {ref}")
    return p


def _stub(p):
    return {"id": p["id"], "slug": p["slug"], "title": p["title"]}


def _slug(ctx, title):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:72].strip("-") or "page"
    slug, n = base, 1
    while _page(ctx, slug) is not None:
        n += 1
        slug = f"{base}-{n}"
    return slug


def _stamp(ctx, sources):
    me, now = ctx.caller.actor, time.time()
    return [{k: v for k, v in s.items() if v is not None} | {"by": me, "when": now} for s in sources or ()]


def _merge(old, new):
    """New sources over old: same key (else same kind and ref) replaces."""
    def ident(s):
        return ("key", s["key"]) if s.get("key") else ("ref", s["kind"], s.get("ref"))
    fresh = {ident(s) for s in new}
    return [s for s in old or () if ident(s) not in fresh] + list(new)


def _check_cites(ctx, body, sources):
    keys = {s["key"] for s in sources if s.get("key")} | set(DEFINED.findall(body or ""))
    missing = sorted(set(CITE.findall(body or "")) - keys)
    if missing:
        raise ctx.Error("bad args: the body cites " + ", ".join(f"[^{k}]" for k in missing)
                        + " with no source; add sources entries with those keys")


def _parent(ctx, ref):
    return _need(ctx, ref)["id"] if ref else None


def get(ctx, id):
    p = _need(ctx, id)
    linked = list(dict.fromkeys(WIKI.findall(p["body"] or "")))
    pages = {s: _page(ctx, s) for s in linked}
    out = {**p,
           "links_out": [_stub(x) for x in pages.values() if x is not None],
           "backlinks": _backlinks(ctx, p),
           "children": [_stub(c) for c in ctx.records.knowledge.list(where={"parent": p["id"]}, limit=200)]}
    missing = [s for s, x in pages.items() if x is None]
    if missing:
        out["missing"] = missing
    return out


def _backlinks(ctx, p):
    """Pages whose body links [[slug]]: full-text search finds candidates
    (a slug's words), the pattern confirms them."""
    words = p["slug"].replace("-", " ")
    mark = f"[[{p['slug']}]]"
    hits = ctx.records.knowledge.search(query=words, limit=MAX_BACKLINKS)
    return [_stub(h) for h in hits if h["id"] != p["id"] and mark in (h["body"] or "")]


def create(ctx, title, body="", slug=None, kind="observation", parent=None, tags=None, sources=None):
    srcs = _stamp(ctx, sources)
    _check_cites(ctx, body, srcs)
    return ctx.records.knowledge.create(
        title=title, body=body, slug=slug or _slug(ctx, title), kind=kind,
        parent=_parent(ctx, parent), tags=tags or [], sources=srcs)


def update(ctx, id, revision, sources=None, parent=None, **fields):
    p = _need(ctx, id)
    changes = {k: v for k, v in fields.items() if v is not None}
    if parent is not None:
        changes["parent"] = up = _parent(ctx, parent)
        for _ in range(100):              # walk up: a page can't sit under itself
            if up is None:
                break
            if up == p["id"]:
                raise ctx.Error("bad args: a page can't sit under itself")
            up = ctx.records.knowledge.get(up)["parent"]
    srcs = _merge(p["sources"], _stamp(ctx, sources)) if sources else p["sources"] or []
    if sources:
        changes["sources"] = srcs
    _check_cites(ctx, changes.get("body", p["body"]), srcs)
    return ctx.records.knowledge.update(p["id"], revision=revision, **changes)


def link(ctx, id, kind, ref=None, key=None, quote=None, note=None, remove=False):
    if not (ref or key or quote):
        raise ctx.Error("bad args: a link needs a ref, key or quote")
    for attempt in range(TRIES):
        p = _need(ctx, id)
        old = p["sources"] or []
        if remove:
            keep = [s for s in old if not ((s.get("key") == key) if key else
                                           (s["kind"], s.get("ref")) == (kind, ref))]
            if len(keep) == len(old):
                raise ctx.Error(f"page {p['slug']} has no such source")
            if key and key in CITE.findall(p["body"] or "") and key not in DEFINED.findall(p["body"] or ""):
                raise ctx.Error(f"the body still cites [^{key}]; change the body first")
            srcs = keep
        else:
            srcs = _merge(old, _stamp(ctx, [{"key": key, "kind": kind, "ref": ref,
                                              "quote": quote, "note": note}]))
        try:
            p = ctx.records.knowledge.update(p["id"], revision=p["revision"], sources=srcs)
            return {"id": p["id"], "slug": p["slug"], "revision": p["revision"], "sources": p["sources"]}
        except ctx.Error as e:
            if not e.retry or attempt == TRIES - 1:
                raise
