"""Plugin marketplace catalogs (preview): parse, validate and fetch them.

Installing from a catalog is not implemented yet (``plugin.marketplace.install``
says so); the format, the hub setting that lists catalogs and the Marketplace
tab are here so catalogs can be written and reviewed now. See
docs/design/plugin-marketplace.md for the format and the security model.

Third-party plugins already load today without a catalog: ``pip install`` a
package that declares a ``rook.plugins`` entry point into the hub's or a
worker's environment and restart it (rook.core.plugin.iter_entry_points).

Stdlib only.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("rook.hub.plugin_catalog")

FORMAT = "rook-plugin-catalog/1"
EXAMPLE_PATH = Path(__file__).with_name("plugin_catalog_example.json")
MAX_BYTES = 1_000_000
FETCH_TIMEOUT = 8.0
CACHE_SECONDS = 300.0

NOT_IMPLEMENTED = ("Installing plugins from a catalog is not implemented yet. For now, "
                   "pip install the plugin's package (it declares a 'rook.plugins' entry "
                   "point) into the hub or worker environment and restart it; see "
                   "docs/design/plugin-marketplace.md.")

_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")          # reverse-DNS-ish: com.example.weather
_NS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d+(\.\d+){0,3}([.-]?[a-z0-9]+)*$", re.I)
_PIP_REQ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?\s*(==|>=|~=)\s*[\w.*+-]+$")


def _text(d: dict, key: str, limit: int, errors: list, required: bool = True) -> str:
    v = d.get(key)
    if v is None or v == "":
        if required:
            errors.append(f"missing {key}")
        return ""
    if not isinstance(v, str):
        errors.append(f"{key} must be a string")
        return ""
    v = v.strip()
    if len(v) > limit:
        errors.append(f"{key} longer than {limit} characters")
        return v[:limit]
    return v


def _url(d: dict, key: str, errors: list, required: bool = False) -> str:
    v = _text(d, key, 500, errors, required)
    if v and not v.startswith("https://"):
        errors.append(f"{key} must be an https:// URL")
        return ""
    return v


def parse_entry(raw: Any) -> tuple[dict | None, list[str]]:
    """One catalog entry, validated and normalised; ``(None, errors)`` when it
    cannot be shown at all."""
    from ..core.authz import TIERS
    from ..core.plugin import CORE_API_VERSION, core_api_compatible
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["entry is not an object"]
    e: dict[str, Any] = {}
    e["id"] = _text(raw, "id", 120, errors)
    if e["id"] and not _ID.match(e["id"]):
        errors.append("id must look like com.example.plugin (lower case, dotted)")
    e["name"] = _text(raw, "name", 80, errors)
    e["version"] = _text(raw, "version", 40, errors)
    if e["version"] and not _VERSION.match(e["version"]):
        errors.append("version is not a version")
    e["description"] = _text(raw, "description", 2000, errors)
    e["author"] = _text(raw, "author", 120, errors)
    e["homepage"] = _url(raw, "homepage", errors)
    e["license"] = _text(raw, "license", 60, errors, required=False)
    e["namespace"] = _text(raw, "namespace", 32, errors)
    if e["namespace"] and not _NS.match(e["namespace"]):
        errors.append("namespace must be lower case letters, digits, - or _")
    placement = raw.get("placement", "not is_hub")
    if not isinstance(placement, str) or not placement.strip():
        errors.append("placement must be a placement expression")
        placement = ""
    else:
        try:
            from ..core.facts import compile_placement
            compile_placement(placement)
        except Exception as err:
            errors.append(f"placement: {err}")
    e["placement"] = placement
    caps = []
    for c in raw.get("caps") or []:
        if not isinstance(c, dict) or not isinstance(c.get("cap"), str):
            errors.append("each cap needs a 'cap' name")
            continue
        risk = c.get("risk")
        if risk not in TIERS:
            errors.append(f"{c['cap']}: risk must be one of {', '.join(TIERS)}")
            risk = "exec"   # undeclared counts as exec, as in core
        if e["namespace"] and not (c["cap"] == e["namespace"]
                                   or c["cap"].startswith(e["namespace"] + ".")):
            errors.append(f"{c['cap']} is outside namespace {e['namespace']}")
        caps.append({"cap": c["cap"][:120], "risk": risk,
                     "description": str(c.get("description") or "")[:300]})
    if not caps:
        errors.append("caps: list at least one capability")
    e["caps"] = caps
    tiers = sorted({c["risk"] for c in caps}, key=TIERS.index)
    e["risk"] = {"tiers": tiers, "top": tiers[-1] if tiers else None}
    pkg = raw.get("package")
    if not isinstance(pkg, dict):
        errors.append("missing package")
        pkg = {}
    kind = pkg.get("kind")
    package: dict[str, Any] = {"kind": kind}
    if kind == "pip":
        req = _text(pkg, "requirement", 200, errors)
        if req and not _PIP_REQ.match(req):
            errors.append("package.requirement must pin a version (name==1.2.3)")
        package["requirement"] = req
    elif kind == "wheel":
        package["url"] = _url(pkg, "url", errors, required=True)
        if package["url"] and not package["url"].endswith(".whl"):
            errors.append("package.url must name a .whl file")
    else:
        errors.append("package.kind must be 'pip' or 'wheel'")
    sha = pkg.get("sha256")
    if kind == "wheel" or sha is not None:
        if not isinstance(sha, str) or not _SHA256.match(sha):
            errors.append("package.sha256 must be 64 lower-case hex characters")
        else:
            package["sha256"] = sha
    e["package"] = package
    sig = raw.get("signature")
    e["signature"] = sig if isinstance(sig, dict) else None   # placeholder: not verified yet
    e["entry_point"] = str(raw.get("entry_point") or "")[:200]
    min_api = raw.get("min_core_api") or "1.0"
    e["min_core_api"] = str(min_api)
    e["compatible"] = core_api_compatible(">=" + str(min_api) + ",<" + str(
        int(CORE_API_VERSION.split(".")[0]) + 1))
    if not e["id"] or not e["name"]:
        return None, errors
    e["problems"] = errors
    return e, errors


def parse_catalog(data: Any, source: str = "") -> dict:
    """``{"name", "source", "example", "plugins": [...], "errors": [...]}``.
    Bad entries are dropped with a reason; a document that is not a catalog
    raises ``ValueError``."""
    if not isinstance(data, dict):
        raise ValueError("a catalog is a JSON object")
    if data.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} document (format={data.get('format')!r})")
    plugins, errors, seen = [], [], set()
    for i, raw in enumerate(data.get("plugins") or []):
        entry, errs = parse_entry(raw)
        label = (raw.get("id") if isinstance(raw, dict) else None) or f"entry {i + 1}"
        if entry is None:
            errors.append(f"{label}: " + "; ".join(errs))
            continue
        if entry["id"] in seen:
            errors.append(f"{label}: duplicate id")
            continue
        seen.add(entry["id"])
        if errs:
            errors.append(f"{label}: " + "; ".join(errs))
        plugins.append(entry)
    return {"name": str(data.get("name") or source or "catalog")[:120], "source": source,
            "example": bool(data.get("example")), "plugins": plugins, "errors": errors}


def example_catalog() -> dict:
    return parse_catalog(json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")),
                         source="bundled example")


def fetch(url: str, timeout: float = FETCH_TIMEOUT) -> dict:
    """Download and parse one catalog (https only, at most 1 MB, no redirects
    to another scheme)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("catalog URLs must be https://")
    from urllib.request import Request, urlopen
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "rook-hub-catalog/1"})
    with urlopen(req, timeout=timeout) as resp:   # noqa: S310 - https enforced above
        if not str(resp.geturl()).startswith("https://"):
            raise ValueError("catalog redirected away from https")
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("catalog larger than 1 MB")
    return parse_catalog(json.loads(raw.decode("utf-8")), source=url)


class CatalogCache:
    """Catalogs by URL, refetched after :data:`CACHE_SECONDS`. With no URL
    configured the bundled example catalog is shown, marked as such."""

    def __init__(self, fetcher=fetch) -> None:
        self._fetch = fetcher
        self._cache: dict[str, tuple[float, dict]] = {}

    def load(self, urls: list[str], refresh: bool = False) -> list[dict]:
        if not urls:
            return [example_catalog()]
        out = []
        now = time.time()
        for url in urls:
            hit = self._cache.get(url)
            if hit and not refresh and now - hit[0] < CACHE_SECONDS:
                out.append(hit[1])
                continue
            try:
                cat = self._fetch(url)
            except Exception as err:
                log.warning("plugin catalog %s: %s", url, err)
                cat = {"name": url, "source": url, "example": False, "plugins": [],
                       "errors": [f"could not load: {type(err).__name__}: {err}"]}
            self._cache[url] = (now, cat)
            out.append(cat)
        return out
