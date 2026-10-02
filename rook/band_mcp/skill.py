"""The Rook agent skill: locate it, package it, serve it, install it.

The generic skill lives in the repo at ``skills/rook/`` (SKILL.md +
references/). Wheels carry a copy at ``rook/_skill/rook/`` (see pyproject).

Site overlay: an operator can append site notes (host roles, standing rules)
without committing them. The hub reads, at request time, either

- ``ROOK_SKILL_SITE_PAGE``: a knowledge page slug or id (needs ROOK_KNOWLEDGE=1), or
- ``ROOK_SKILL_SITE_FILE``: a Markdown file on the hub,

plus, when the persona plugin has a default persona assigned, a short
"Persona" section (docs/design/persona.md), and serves it as
``references/site.md``. The overlay is only included for callers holding a valid token (MCP clients always do; the HTTP download needs
``Authorization: Bearer <token>``). Anonymous downloads get the generic skill.

Served as:
- MCP resources ``rook://skill/rook`` (SKILL.md) and
  ``rook://skill/rook/references/<name>.md``;
- HTTP ``GET /skill/rook.skill`` (zip, root folder ``rook/``) and
  ``GET /skill/rook/<path>`` (single file).
"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Callable

log = logging.getLogger("rook.band_mcp.skill")

SKILL_NAME = "rook"
SITE_FILE = "references/site.md"
_PKG_ROOT = Path(__file__).resolve().parent.parent  # .../rook


def skill_dir() -> Path:
    """Directory holding SKILL.md: the wheel copy, else the source checkout."""
    for cand in (_PKG_ROOT / "_skill" / SKILL_NAME,
                 _PKG_ROOT.parent / "skills" / SKILL_NAME):
        if (cand / "SKILL.md").is_file():
            return cand
    raise FileNotFoundError("Rook skill not found (expected skills/rook/SKILL.md)")


def base_files() -> dict[str, str]:
    """The generic skill: {relative path: text}, sorted, POSIX separators."""
    root = skill_dir()
    out = {}
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel == SITE_FILE:  # a stray local overlay never ships as generic
            continue
        out[rel] = p.read_text(encoding="utf-8")
    return out


def site_notes(knowledge=None, persona=None) -> str | None:
    """Operator site notes from config, plus the band's persona note when the
    persona plugin has a default assigned (``persona`` is that plugin), or
    None. Never raises."""
    notes = _configured_notes(knowledge)
    note = None
    if persona is not None:
        try:
            note = persona.site_note() or None
        except Exception as e:  # noqa: BLE001
            log.warning("persona note for the skill overlay unavailable: %s", e)
    if notes and note:
        return notes.rstrip("\n") + "\n\n" + note
    return notes or note


def _configured_notes(knowledge=None) -> str | None:
    slug = os.environ.get("ROOK_SKILL_SITE_PAGE", "").strip()
    path = os.environ.get("ROOK_SKILL_SITE_FILE", "").strip()
    try:
        if slug and knowledge is not None:
            band = knowledge._band_for(None, slug)
            rec = knowledge.store.get(band, slug)
            body = (rec.get("body") or "").strip()
            if body:
                return f"# {rec.get('title') or 'Site notes'}\n\n{body}\n"
        if path:
            text = Path(path).read_text(encoding="utf-8").strip()
            if text:
                return text + "\n"
    except Exception as e:  # noqa: BLE001 — a broken overlay must not break the skill
        log.warning("skill site overlay unavailable: %s", e)
    return None


def files(site: str | None = None) -> dict[str, str]:
    out = base_files()
    if site:
        out[SITE_FILE] = site
    return out


def package(file_map: dict[str, str]) -> bytes:
    """Claude skill format: a zip whose single root folder is ``rook/``.
    Deterministic (fixed timestamps) so identical content gives identical bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, text in sorted(file_map.items()):
            info = zipfile.ZipInfo(f"{SKILL_NAME}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def install(file_map: dict[str, str], dest: Path) -> list[Path]:
    """Write the skill into ``dest`` (the ``rook/`` folder itself), replacing
    files this skill owns and removing a stale site.md. Returns written paths."""
    dest = Path(dest).expanduser()
    written = []
    for rel, text in sorted(file_map.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    if SITE_FILE not in file_map:
        (dest / SITE_FILE).unlink(missing_ok=True)
    return written


def _resource_uri(rel: str) -> str:
    return f"rook://skill/{SKILL_NAME}" if rel == "SKILL.md" else f"rook://skill/{SKILL_NAME}/{rel}"


def register(mcp, store=None, knowledge_getter: Callable[[], object] | None = None,
             persona_getter: Callable[[], object] | None = None) -> None:
    """Register the skill as MCP resources and HTTP routes on ``mcp``.

    ``store`` is the TokenStore that vouches for HTTP bearer tokens (overlay
    access); ``knowledge_getter`` returns the KnowledgeService or None.
    """
    kget = knowledge_getter or (lambda: None)
    pget = persona_getter or (lambda: None)
    try:
        generic = base_files()
    except FileNotFoundError:
        log.warning("Rook skill files missing; skill resources not served")
        return

    def _reader(rel: str):
        def _read() -> str:
            return base_files()[rel]
        return _read

    for rel in generic:
        _read = _reader(rel)
        mcp.resource(_resource_uri(rel), name=f"rook-skill:{rel}",
                     description=f"Rook agent skill: {rel}",
                     mime_type="text/markdown")(_read)

    def _site() -> str:
        return site_notes(kget(), pget()) or "No site notes configured on this hub.\n"
    mcp.resource(_resource_uri(SITE_FILE), name=f"rook-skill:{SITE_FILE}",
                 description="Operator-maintained site notes for this band (may be empty)",
                 mime_type="text/markdown")(_site)

    from starlette.responses import PlainTextResponse, Response

    def _authed(request) -> bool:
        got = request.headers.get("authorization", "")
        if not got.lower().startswith("bearer ") or store is None:
            return False
        try:
            return store.verify_bearer(got[7:].strip()) is not None
        except Exception:  # noqa: BLE001
            return False

    def _current(request) -> dict[str, str]:
        return files(site_notes(kget(), pget()) if _authed(request) else None)

    @mcp.custom_route("/skill/rook.skill", methods=["GET"])
    async def _download(request):
        return Response(package(_current(request)), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="rook.skill"',
                                 "Cache-Control": "no-store"})

    @mcp.custom_route("/skill/rook/{path:path}", methods=["GET"])
    async def _one(request):
        text = _current(request).get(request.path_params["path"])
        if text is None:
            return PlainTextResponse("not found", 404)
        return PlainTextResponse(text, media_type="text/markdown",
                                 headers={"Cache-Control": "no-store"})

