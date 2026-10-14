"""Fakes for the update service tests: a GitHub releases server and a band.

:class:`FakeGitHub` serves ``/repos/<owner>/<repo>/releases`` (with ETags and
304s) and the assets, so no test talks to the real GitHub. :class:`FakeBand`
is a band client whose workers update themselves when pushed ``worker.apply``,
or misbehave on request.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import time
from typing import Any

from aiohttp import web


def release_json(version: str, build: int, *, min_worker_build: int = 142,
                 min_android_app: str = "0.4.1", min_hub_build: int = 230,
                 breaking: bool = False, reasons: list | None = None,
                 firmware: dict | None = None, channel: str | None = None) -> bytes:
    pre = any(x in version for x in ("rc", "a", "b"))
    return json.dumps({
        "schema": 1, "typ": "rook-release", "tag": "v" + version, "version": version,
        "build": build, "name": f"{build}.lumpy.moose", "commit": "abc1234",
        "channel": channel or ("prerelease" if pre else "stable"), "breaking": breaking,
        "breaking_reasons": reasons or [], "min_worker_build": min_worker_build,
        "min_android_app": min_android_app, "min_hub_build": min_hub_build, "relay": "v0.3.0",
        "android_app": {"version_name": "0.4.15", "version_code": 19},
        "firmware": firmware or {"esp32": "0.6.7", "rp2040": "0.1.0"},
    }).encode()


def sdist(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in ((f"rook-{version}/pyproject.toml", b"[project]\nname='rook'\n"),
                           (f"rook-{version}/rook/__init__.py", f"__version__ = '{version}'\n".encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    def __init__(self) -> None:
        self.releases: dict[str, list[dict]] = {}
        self.files: dict[tuple, bytes] = {}
        self.requests: list[tuple[str, str]] = []   # (path, If-None-Match)
        self.fail: tuple[int, dict] | None = None
        self.base = ""
        self.app = web.Application()
        self.app.router.add_get("/repos/{owner}/{repo}/releases", self._list)
        self.app.router.add_get("/dl/{owner}/{repo}/{tag}/{name}", self._asset)

    def add_release(self, repo: str, tag: str, assets: dict[str, bytes], *, prerelease=False,
                    draft=False, body="", sums=True, tamper: dict | None = None) -> dict:
        assets = dict(assets)
        if sums:
            assets["SHA256SUMS"] = "".join(
                f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in assets.items()).encode()
        for n, d in assets.items():
            self.files[(repo, tag, n)] = (tamper or {}).get(n, d)
        rel = {"tag_name": tag, "name": f"Rook {tag.lstrip('v')}", "body": body,
               "prerelease": prerelease, "draft": draft,
               "published_at": "2026-10-14T12:00:00Z",
               "html_url": f"https://github.example.com/{repo}/releases/tag/{tag}",
               "assets": [{"name": n, "size": len(d),
                           "browser_download_url": f"{{base}}/dl/{repo}/{tag}/{n}"}
                          for n, d in assets.items()]}
        self.releases.setdefault(repo, []).insert(0, rel)
        return rel

    def _payload(self, repo: str) -> bytes:
        out = json.loads(json.dumps(self.releases.get(repo, [])))
        for r in out:
            for a in r["assets"]:
                a["browser_download_url"] = a["browser_download_url"].replace("{base}", self.base)
        return json.dumps(out).encode()

    async def _list(self, request: web.Request) -> web.Response:
        repo = f"{request.match_info['owner']}/{request.match_info['repo']}"
        self.requests.append((repo, request.headers.get("If-None-Match", "")))
        if self.fail:
            status, headers = self.fail
            return web.Response(status=status, headers=headers, text="nope")
        body = self._payload(repo)
        etag = '"' + hashlib.sha1(body).hexdigest() + '"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(body=body, content_type="application/json", headers={"ETag": etag})

    async def _asset(self, request: web.Request) -> web.Response:
        m = request.match_info
        data = self.files.get((f"{m['owner']}/{m['repo']}", m["tag"], m["name"]))
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="application/octet-stream")


class FakeBand:
    """A band client: ``workers`` like the real roster, ``call`` answering the
    caps the update service uses."""

    def __init__(self) -> None:
        self.workers: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.behaviour: dict[str, str] = {}   # wid -> ok | never | rollback | refuse | held | mute
        self.delay = 0.05

    def add(self, wid: str, name: str, build: int | None = 500, *, band: str = "aaaa1111",
            os: str = "linux", caps=("worker.apply", "worker.status"), app: dict | None = None,
            plugins=("info", "shell"), version: str | None = None, roles=()) -> dict:
        w = {"worker_id": wid, "name": name, "build": build, "band": band,
             "version": version or (f"{build}.lumpy.moose" if build else None),
             "caps": list(caps), "plugins": list(plugins), "facts": {"os": os},
             "app_release": app or {}, "hb": {}, "roles": list(roles), "last_seen": time.time()}
        self.workers[wid] = w
        return w

    async def call(self, cap: str, args: dict | None = None, target: str | None = None,
                   timeout: float = 15.0, identity: str | None = None, principal: Any = None) -> dict:
        self.calls.append((cap, target, args or {}))
        w = self.workers[target]
        mode = self.behaviour.get(target, "ok")
        if cap == "worker.apply":
            if mode == "refuse":
                return {"ok": True, "result": {"ok": False, "error": "manifest signature invalid"}}
            if mode == "held":
                return {"ok": True, "result": {"ok": True, "action": "held", "build": w["build"]}}
            target_build = args["manifest"]["build"]
            asyncio.get_event_loop().call_later(self.delay, self._land, target, target_build, mode)
            return {"ok": True, "result": {"ok": True, "action": "updated", "restarting": True,
                                           "to_build": target_build}}
        if cap == "worker.status":
            if mode == "mute":
                return {"ok": False, "error": "timeout"}
            return {"ok": True, "result": {"build": w["build"]}}
        if cap == "device.update":
            return {"ok": True, "result": {"queued": True}}
        if cap == "dongle.status":
            return {"ok": True, "result": {"device": "rook-rp2040-zero", "version": "0.0.9"}}
        return {"ok": False, "error": f"unknown cap {cap}"}

    def _land(self, wid: str, build: int, mode: str) -> None:
        w = self.workers[wid]
        w["last_seen"] = time.time()
        if mode == "never":
            return
        if mode == "rollback":
            w["hb"] = {"worker": {"update": "rolled back: no band round trip",
                                  "bad_build": build}}
            return
        w["build"], w["version"] = build, f"{build}.lumpy.moose"
