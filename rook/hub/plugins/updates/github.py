"""GitHub releases for the update service: polling, verification, cache.

* :class:`ReleaseFeed` polls ``GET <api>/repos/<repo>/releases`` with the last
  ``ETag`` (a 304 costs no rate limit) and backs off exponentially on errors,
  honouring ``Retry-After`` and ``X-RateLimit-Reset``. Drafts are never
  listed; prereleases only when the channel allows them.
* :meth:`ReleaseFeed.fetch` downloads one asset into the cache, bounded in
  size and time, and keeps it only when its sha256 matches the release's
  ``SHA256SUMS``. ``release.json`` is read the same way.

Trust: ``SHA256SUMS`` comes from the same release page as the files, so it
proves the download is the file the release published (no corruption, no
proxy rewriting), not who published it. The release's build provenance
attestations prove that; checking them needs Sigstore's trust root, so it
is optional (:func:`verify_attestation`, with the ``gh`` CLI). Worker
bundles are then re-signed by this hub's own update key as before.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ...release_meta import parse_release_json, version_tuple

log = logging.getLogger("rook.hub.plugins.updates.github")

API = "https://api.github.com"
UA = "rook-hub-updates"
MAX_ASSET_BYTES = 400 * 1024 * 1024
SMALL_ASSET_BYTES = 1024 * 1024        # release.json, SHA256SUMS
BACKOFF_BASE = 15 * 60.0
BACKOFF_MAX = 24 * 3600.0
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")


class ReleaseError(Exception):
    pass


def _tag_ok(tag: str) -> bool:
    return bool(re.fullmatch(r"v?[0-9][0-9A-Za-z.\-+]{0,60}", tag or ""))


def clean_release(raw: dict) -> dict | None:
    """The fields we use from one GitHub release object (None for drafts
    and anything malformed). Text is kept as data only."""
    if not isinstance(raw, dict) or raw.get("draft"):
        return None
    tag = str(raw.get("tag_name") or "")
    if not _tag_ok(tag):
        return None
    assets = {}
    for a in raw.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if _SAFE_NAME.match(name) and url.startswith(("https://", "http://")):
            size = a.get("size")
            assets[name] = {"url": url, "size": size if isinstance(size, int) else None}
    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "title": str(raw.get("name") or tag)[:200],
        "notes": str(raw.get("body") or "")[:20000],
        "prerelease": bool(raw.get("prerelease")),
        "published_at": str(raw.get("published_at") or "")[:40],
        "html_url": str(raw.get("html_url") or "")[:500],
        "assets": assets,
    }


def parse_sums(text: str) -> dict[str, str]:
    """``SHA256SUMS`` lines (``<hex>  <name>`` or ``<hex> *<name>``)."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[1].lstrip("*").lstrip("./")] = parts[0].lower()
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _retry_after(headers: Any, now: float) -> float | None:
    ra = headers.get("Retry-After") if headers is not None else None
    if ra:
        try:
            return max(0.0, float(ra))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(ra).timestamp() - now)
            except (TypeError, ValueError):
                pass
    if headers is not None and headers.get("X-RateLimit-Remaining") == "0":
        try:
            return max(0.0, float(headers.get("X-RateLimit-Reset")) - now)
        except (TypeError, ValueError):
            pass
    return None


class ReleaseFeed:
    """One GitHub repository's releases, cached under ``root``."""

    def __init__(self, repo: str, root: Path, api: str = API, token: str = "",
                 session_factory=None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise ValueError(f"not a GitHub owner/repo: {repo!r}")
        self.repo = repo
        self.api = (api or API).rstrip("/")
        self.token = token
        self.root = Path(root) / repo.replace("/", "__")
        self._session_factory = session_factory
        self.state = self._load()

    # -- state ----------------------------------------------------------------
    def _state_path(self) -> Path:
        return self.root / "releases.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"etag": "", "releases": [], "checked_at": 0, "ok_at": 0, "failures": 0,
                "next_at": 0, "error": ""}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_path())

    def releases(self, prerelease: bool = False) -> list[dict]:
        """Known releases, newest first; prereleases only when allowed."""
        out = [r for r in self.state.get("releases") or [] if prerelease or not r.get("prerelease")]
        return sorted(out, key=lambda r: version_tuple(r["tag"]), reverse=True)

    def find(self, tag: str) -> dict | None:
        return next((r for r in self.state.get("releases") or [] if r["tag"] == tag), None)

    # -- HTTP -----------------------------------------------------------------
    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {"User-Agent": UA, "Accept": accept}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300, sock_connect=20))

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) >= float(self.state.get("next_at") or 0)

    async def poll(self, interval: float, force: bool = False) -> dict:
        """Check for releases now (``force``) or when due. Returns
        ``{"changed", "error", "skipped"}``; the state keeps the rest."""
        now = time.time()
        if not force and not self.due(now):
            return {"changed": False, "skipped": True, "error": self.state.get("error", "")}
        url = f"{self.api}/repos/{self.repo}/releases?per_page=30"
        headers = self._headers()
        if self.state.get("etag") and self.state.get("releases"):
            headers["If-None-Match"] = self.state["etag"]
        self.state["checked_at"] = now
        changed = False
        try:
            async with self._session() as s, s.get(url, headers=headers) as r:
                if r.status == 304:
                    pass
                elif r.status == 200:
                    raw = await r.json(content_type=None)
                    if not isinstance(raw, list):
                        raise ReleaseError("GitHub answered with something other than a list")
                    rels = [c for c in (clean_release(x) for x in raw) if c]
                    old = {x["tag"]: x for x in self.state.get("releases") or []}
                    for rel in rels:   # keep what we learned about known tags
                        prev = old.get(rel["tag"]) or {}
                        for k in ("meta", "meta_error", "sums"):
                            if k in prev:
                                rel[k] = prev[k]
                    changed = [x["tag"] for x in rels] != [x["tag"] for x in old.values()]
                    self.state["releases"] = rels
                    self.state["etag"] = r.headers.get("ETag", "")
                else:
                    wait = _retry_after(r.headers, now)
                    raise ReleaseError(f"GitHub answered HTTP {r.status}"
                                       + (f"; retry in {int(wait)}s" if wait else ""),
                                       wait)
            self.state.update(ok_at=now, failures=0, error="", next_at=now + interval)
        except Exception as e:
            failures = int(self.state.get("failures") or 0) + 1
            wait = e.args[1] if isinstance(e, ReleaseError) and len(e.args) > 1 and e.args[1] else None
            delay = wait if wait is not None else min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (failures - 1))
            msg = e.args[0] if isinstance(e, ReleaseError) else f"{type(e).__name__}: {e}"
            self.state.update(failures=failures, error=str(msg)[:300],
                              next_at=now + max(delay, 60.0))
            log.info("release check for %s failed (%s); next try in %ds", self.repo, msg,
                     int(max(delay, 60.0)))
        self._save()
        return {"changed": bool(changed), "skipped": False, "error": self.state.get("error", "")}

    async def _download(self, url: str, dest: Path, limit: int, accept: str = "application/octet-stream") -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".part{os.getpid()}")
        got = 0
        try:
            async with self._session() as s, s.get(url, headers=self._headers(accept)) as r:
                if r.status != 200:
                    raise ReleaseError(f"download {dest.name}: HTTP {r.status}")
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        got += len(chunk)
                        if got > limit:
                            raise ReleaseError(f"{dest.name} is larger than {limit} bytes")
                        f.write(chunk)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _dir(self, tag: str) -> Path:
        if not _tag_ok(tag):
            raise ReleaseError(f"bad tag {tag!r}")
        return self.root / tag

    async def sums(self, rel: dict) -> dict[str, str]:
        if isinstance(rel.get("sums"), dict) and rel["sums"]:
            return rel["sums"]
        asset = rel["assets"].get("SHA256SUMS")
        if not asset:
            raise ReleaseError(f"{rel['tag']} has no SHA256SUMS: nothing can be verified")
        dest = self._dir(rel["tag"]) / "SHA256SUMS"
        await self._download(asset["url"], dest, SMALL_ASSET_BYTES)
        sums = parse_sums(dest.read_text(encoding="utf-8", errors="replace"))
        if not sums:
            raise ReleaseError(f"{rel['tag']}: SHA256SUMS lists no files")
        rel["sums"] = sums
        self._remember(rel)
        return sums

    def _remember(self, rel: dict) -> None:
        for i, r in enumerate(self.state.get("releases") or []):
            if r["tag"] == rel["tag"]:
                self.state["releases"][i] = rel
        self._save()

    async def fetch(self, rel: dict, name: str, limit: int = MAX_ASSET_BYTES) -> Path:
        """``name`` from release ``rel``, verified against its SHA256SUMS.
        A cached copy is re-hashed rather than trusted."""
        if not _SAFE_NAME.match(name) or name == "SHA256SUMS":
            raise ReleaseError(f"bad asset name {name!r}")
        asset = rel["assets"].get(name)
        if not asset:
            raise ReleaseError(f"{rel['tag']} has no {name}")
        want = (await self.sums(rel)).get(name)
        if not want:
            raise ReleaseError(f"{name} is not listed in {rel['tag']}'s SHA256SUMS")
        dest = self._dir(rel["tag"]) / name
        if dest.is_file() and sha256_of(dest) == want:
            return dest
        await self._download(asset["url"], dest, limit)
        got = sha256_of(dest)
        if got != want:
            dest.unlink(missing_ok=True)
            raise ReleaseError(f"{name}: sha256 {got[:16]}... does not match SHA256SUMS "
                               f"({want[:16]}...); download discarded")
        return dest

    async def meta(self, rel: dict) -> dict | None:
        """The release's verified release.json (None for a release from
        before release.json existed; the error is kept in ``meta_error``)."""
        if isinstance(rel.get("meta"), dict):
            return rel["meta"]
        if "release.json" not in rel["assets"]:
            rel["meta_error"] = "this release has no release.json (it predates update metadata)"
            self._remember(rel)
            return None
        try:
            path = await self.fetch(rel, "release.json", SMALL_ASSET_BYTES)
            meta = parse_release_json(json.loads(path.read_text(encoding="utf-8")))
        except (ReleaseError, ValueError, OSError) as e:
            rel["meta_error"] = str(e)[:300]
            self._remember(rel)
            return None
        rel["meta"], rel["meta_error"] = meta, ""
        self._remember(rel)
        return meta

    def prune(self, keep_tags: set[str]) -> None:
        if not self.root.is_dir():
            return
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in keep_tags:
                shutil.rmtree(p, ignore_errors=True)


async def verify_attestation(path: Path, repo: str, timeout: float = 120.0) -> dict:
    """Check a downloaded file's build provenance with ``gh attestation
    verify``. ``{"checked": False, ...}`` when ``gh`` is not installed: the
    Sigstore check needs its trust root, which this hub does not carry."""
    gh = shutil.which("gh")
    if not gh:
        return {"checked": False, "ok": None,
                "detail": "gh CLI not installed: provenance not checked (SHA256SUMS was)"}
    try:
        proc = await asyncio.create_subprocess_exec(
            gh, "attestation", "verify", str(path), "-R", repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        return {"checked": True, "ok": False, "detail": f"gh failed: {e}"}
    text = (out or b"").decode("utf-8", "replace").strip().splitlines()
    return {"checked": True, "ok": proc.returncode == 0, "detail": (text[-1] if text else "")[:300]}
