"""The bundled user manual: Markdown pages with front matter, plus a manifest.

The pages live in the repository at ``docs/manual/`` and ship in the wheel
as ``rook/_manual/`` (pyproject force-include, like the agent skill). Each
page starts with front matter::

    ---
    id: getting-started
    title: Getting started
    order: 2
    features:
      - features/the-band
      - cli/hub
    since: 0.1.0
    deprecated: false
    ---

Stdlib only: ``tools/manual_check.py`` and the hub plugin both use it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]          # .../rook
MANIFEST = "manifest.json"
SKIP = {"README.md"}                  # contributor notes, not a manual page
PAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
VERSION = re.compile(r"^\d+(\.\d+)*([a-z]+\d*)?$")
#: Knowledge pages: the folder page and each page's slug.
ROOT_SLUG = "rook-manual"
SLUG_PREFIX = "manual-"
#: The knowledge store caps a body at 20 000 characters; leave room.
MAX_BODY = 18000


class ManualError(ValueError):
    pass


@dataclass
class Page:
    id: str
    title: str
    body: str                       # Markdown without the front matter
    file: str                       # file name inside the manual directory
    order: int = 1000
    features: list = field(default_factory=list)
    since: str = ""
    deprecated: bool = False
    preview: bool = False           # a feature not in a release yet (front matter preview: true)

    @property
    def slug(self) -> str:
        return SLUG_PREFIX + self.id

    @property
    def hash(self) -> str:
        """What a reader sees: title and body (front matter edits that change
        neither, such as a feature mapping, are not a manual change)."""
        return content_hash(self.title, self.body)

    def meta(self) -> dict:
        return {"id": self.id, "title": self.title, "file": self.file, "order": self.order,
                "features": list(self.features), "since": self.since,
                "deprecated": self.deprecated, "preview": self.preview, "slug": self.slug,
                "sha256": self.hash}


def content_hash(title: str, body: str) -> str:
    return hashlib.sha256((title.strip() + "\n\n" + body.strip()).encode("utf-8")).hexdigest()


def _scalar(value: str):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return v


def parse_front_matter(text: str) -> tuple[dict, str]:
    """``(meta, body)``. A small YAML subset: ``key: value``, ``key: [a, b]``
    and ``key:`` followed by ``  - item`` lines."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ManualError("no front matter (the file must start with ---)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ManualError("front matter is not closed with ---")
    meta: dict = {}
    key = None
    for n, line in enumerate(text[4:end].split("\n"), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item:
            if key is None or not isinstance(meta.get(key), list):
                raise ManualError(f"front matter line {n}: a list item outside a list")
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([a-z_]+):(.*)$", line)
        if not kv:
            raise ManualError(f"front matter line {n}: expected key: value")
        key, value = kv.group(1), kv.group(2)
        meta[key] = _scalar(value) if value.strip() else []
    return meta, text[end + 5:].lstrip("\n")


def parse_page(path: Path) -> Page:
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    missing = [k for k in ("id", "title", "features", "since", "deprecated") if k not in meta]
    if missing:
        raise ManualError(f"{path.name}: front matter lacks {', '.join(missing)}")
    pid, title = str(meta["id"]), str(meta["title"])
    if not PAGE_ID.match(pid):
        raise ManualError(f"{path.name}: id must be lowercase letters, digits and dashes")
    if path.stem != pid:
        raise ManualError(f"{path.name}: the file must be named {pid}.md")
    features = meta["features"]
    if not isinstance(features, list) or not all(isinstance(f, str) and f for f in features):
        raise ManualError(f"{path.name}: features must be a list of feature ids")
    if not isinstance(meta["deprecated"], bool):
        raise ManualError(f"{path.name}: deprecated must be true or false")
    if not isinstance(meta.get("preview", False), bool):
        raise ManualError(f"{path.name}: preview must be true or false")
    since = str(meta["since"])
    if not VERSION.match(since):
        raise ManualError(f"{path.name}: since must be a version such as 0.1.0")
    if not body.strip():
        raise ManualError(f"{path.name}: the page is empty")
    if len(body) > MAX_BODY:
        raise ManualError(f"{path.name}: {len(body)} characters; split it (max {MAX_BODY})")
    order = meta.get("order", 1000)
    return Page(id=pid, title=title, body=body.rstrip() + "\n", file=path.name,
                order=order if isinstance(order, int) else 1000, features=list(features),
                since=since, deprecated=meta["deprecated"], preview=meta.get("preview", False))


def find_dir() -> Path | None:
    """Where the bundled manual is: ``ROOK_MANUAL_DIR``, the wheel's
    ``rook/_manual``, then the checkout's ``docs/manual``."""
    env = os.environ.get("ROOK_MANUAL_DIR")
    cands = ([Path(env)] if env else []) + [_PKG_ROOT / "_manual",
                                             _PKG_ROOT.parent / "docs" / "manual"]
    for cand in cands:
        if cand.is_dir() and any(cand.glob("*.md")):
            return cand
    return None


@dataclass
class Bundle:
    directory: Path | None
    pages: list           # Page, in reading order
    errors: list          # "file: problem" for pages that did not parse

    @property
    def hash(self) -> str:
        """One hash for the whole manual: page ids and content, in order."""
        h = hashlib.sha256()
        for p in self.pages:
            h.update(f"{p.id}\0{p.hash}\0{int(p.deprecated)}{int(p.preview)}\n".encode())
        return h.hexdigest()

    def page(self, pid: str) -> Page | None:
        return next((p for p in self.pages if p.id == pid), None)

    def manifest(self) -> dict:
        features: dict = {}
        for p in self.pages:
            for f in p.features:
                features.setdefault(f, []).append(p.id)
        return {"format": "rook-manual/1", "manual_sha256": self.hash,
                "pages": [p.meta() for p in self.pages],
                "features": {k: features[k] for k in sorted(features)}}


def load(directory: Path | None = None) -> Bundle:
    directory = directory or find_dir()
    if directory is None:
        return Bundle(None, [], ["the manual was not found (docs/manual or rook/_manual)"])
    pages, errors = [], []
    for path in sorted(Path(directory).glob("*.md")):
        if path.name in SKIP:
            continue
        try:
            pages.append(parse_page(path))
        except (ManualError, OSError, UnicodeDecodeError) as e:
            errors.append(f"{path.name}: {e}" if not str(e).startswith(path.name) else str(e))
    ids = [p.id for p in pages]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"page id {dup!r} is used twice")
    pages.sort(key=lambda p: (p.order, p.id))
    return Bundle(Path(directory), pages, errors)


def read_manifest(directory: Path) -> dict | None:
    path = Path(directory) / MANIFEST
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(bundle: Bundle) -> Path:
    path = bundle.directory / MANIFEST
    path.write_text(json.dumps(bundle.manifest(), indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path
