"""The worker update feed the hub serves, owned by the update service.

Without the update service a hub serves whatever worker bundle it has (its
auto-built one, or an operator-built one in rook/remote): every worker that
polls ``/band-worker.json``, and every worker the push loop reaches, moves
to it as soon as it changes. That is how a hub update used to roll the whole
fleet at once.

The update service (rook/hub/plugins/updates) keeps a copy of every bundle
the hub has had, by build, under ``<updates dir>/worker/<build>/``, and a
pointer to the *released* build (``feed.json``). The dashboard then serves
the released build's manifest at ``/band-worker.json`` and pushes only it;
a newer bundle goes to the workers the operator picks (a staged rollout) and
becomes the released build when the rollout reaches everyone. Each snapshot's
manifest is re-signed with this hub's update key with its download URL
pointing at ``/updates/worker/<build>/band-worker.pyz``, so a worker always
downloads exactly the bundle its manifest names; the signature and sha256
are still what the worker checks.

New installs (``/band-worker.pyz``) keep getting the newest bundle.

Stdlib only at import: the dashboard imports this in either hub layout.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("rook.hub.update_feed")

PYZ = "band-worker.pyz"
MANIFEST = "band-worker.json"
SNAPSHOT_PATH = "/updates/worker"     # + /<build>/band-worker.pyz
KEEP_SNAPSHOTS = 6


def updates_dir() -> Path:
    """Where the update service keeps its state, downloads and feed
    (``$ROOK_UPDATES_DIR``, else ``$ROOK_DATA_DIR/updates``, else
    ``~/.local/share/rook/updates``). The dashboard and the MCP side compute
    the same path, in one process or two."""
    env = os.environ.get("ROOK_UPDATES_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    from ..paths import data_path
    return Path(data_path("updates", str(Path.home() / ".local" / "share" / "rook" / "updates")))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def snapshot_url(original_url: str, build: int) -> str:
    """``https://hub/band-worker.pyz`` -> ``https://hub/updates/worker/<b>/band-worker.pyz``."""
    base = original_url.rsplit("/", 1)[0] if "/" in original_url else ""
    return f"{base}{SNAPSHOT_PATH}/{int(build)}/{PYZ}"


class WorkerFeed:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else updates_dir()

    @property
    def dir(self) -> Path:
        return self.root / "worker"

    # -- reading (dashboard side) ------------------------------------------
    def pointer(self) -> dict:
        data = _read_json(self.root / "feed.json")
        return data if isinstance(data, dict) else {}

    def released_build(self) -> int:
        b = self.pointer().get("released")
        return b if isinstance(b, int) and b > 0 else 0

    def manifest_for(self, build: int) -> dict | None:
        data = _read_json(self.dir / str(int(build)) / MANIFEST)
        return data if isinstance(data, dict) and data.get("build") == int(build) else None

    def pyz_for(self, build: Any) -> Path | None:
        try:
            b = int(build)
        except (TypeError, ValueError):
            return None
        if b <= 0:
            return None
        p = self.dir / str(b) / PYZ
        return p if p.is_file() else None

    def released_manifest(self) -> dict | None:
        """The manifest to serve and push, or None when the feed is not in
        charge (no update service, or nothing released yet): the caller then
        serves the bundle as before."""
        b = self.released_build()
        if not b:
            return None
        m = self.manifest_for(b)
        return m if m and self.pyz_for(b) else None

    def builds(self) -> list[int]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.iterdir():
            if p.name.isdigit() and (p / PYZ).is_file() and (p / MANIFEST).is_file():
                out.append(int(p.name))
        return sorted(out)

    def newest_build(self) -> int:
        b = self.builds()
        return b[-1] if b else 0

    # -- writing (update service side) -------------------------------------
    def snapshot(self, manifest_path: Path, pyz_path: Path,
                 sign: Callable[[dict], dict] | None = None) -> int:
        """Copy a signed bundle into the feed under its build, re-signed with
        its snapshot URL. Returns the build (0: unsigned or unreadable, not
        copied). An existing snapshot of the same build is kept."""
        manifest = _read_json(Path(manifest_path))
        if not isinstance(manifest, dict) or not manifest.get("sig"):
            return 0
        build = manifest.get("build")
        if not isinstance(build, int) or build <= 0:
            return 0
        dest = self.dir / str(build)
        if (dest / MANIFEST).is_file() and (dest / PYZ).is_file():
            return build
        from rook.worker._update_verify import sha256_file
        if sha256_file(pyz_path) != manifest.get("sha256"):
            # The bundle is mid-rebuild (pyz swapped, manifest not yet): next time.
            return 0
        if sign is None:
            from rook.remote.update_keys import sign_manifest as sign
        new = {k: v for k, v in manifest.items() if k != "sig"}
        if manifest.get("url"):
            new["url"] = snapshot_url(str(manifest["url"]), build)
        else:
            new["filename"] = f"{SNAPSHOT_PATH.lstrip('/')}/{build}/{PYZ}"
        new["snapshot_of"] = manifest.get("filename", PYZ)
        signed = sign(new)
        if not signed.get("sig"):
            return 0
        tmp = self.dir / f".{build}.tmp{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        shutil.copy2(pyz_path, tmp / PYZ)
        _write_json(tmp / MANIFEST, signed)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        log.info("worker feed: kept build %s", build)
        return build

    def release(self, build: int, by: str = "system", note: str = "") -> dict:
        if not self.manifest_for(build) or not self.pyz_for(build):
            raise LookupError(f"worker build {build} is not in the feed")
        prev = self.released_build()
        data = {"released": int(build), "previous": prev, "at": int(time.time()),
                "by": by, "note": note}
        _write_json(self.root / "feed.json", data)
        return data

    def prune(self, keep: int = KEEP_SNAPSHOTS, protect: tuple = ()) -> list[int]:
        keep_set = set(self.builds()[-keep:]) | {self.released_build(), *protect}
        gone = []
        for b in self.builds():
            if b not in keep_set:
                shutil.rmtree(self.dir / str(b), ignore_errors=True)
                gone.append(b)
        return gone


# -- what the dashboard reads besides the feed ---------------------------------

def published_apk(root: Path | None = None) -> tuple[Path, dict] | None:
    """The APK the update service published (``apk/rook-worker.apk`` with its
    approved manifest), or None. The manifest's sha256 is the approval: the
    file was checked against the release's SHA256SUMS when it was published."""
    base = (Path(root) if root is not None else updates_dir()) / "apk"
    apk, manifest = base / "rook-worker.apk", _read_json(base / "rook-worker-apk.json")
    if not apk.is_file() or not isinstance(manifest, dict) or not manifest.get("sha256"):
        return None
    return apk, manifest


def summary(root: Path | None = None) -> dict:
    """The service's public summary (``summary.json``): newest releases and
    the floors of a pending breaking one, for the Workers page and the
    desktop update check. {} when the service has not run."""
    data = _read_json((Path(root) if root is not None else updates_dir()) / "summary.json")
    return data if isinstance(data, dict) else {}


def worker_note(row: dict, floors: dict | None = None, pending: dict | None = None) -> str:
    """A plain-language note for a Workers-page row that is (or will be) too
    old for this hub: "" for everyone else."""
    from .release_meta import below_floor
    from ..core import compat
    if floors is None:
        floors = {"min_worker_build": compat.MIN_WORKER_BUILD,
                  "min_android_app": compat.MIN_ANDROID_APP,
                  "min_hub_build": compat.MIN_HUB_BUILD}
    target = member_kind(row)
    why = below_floor(target, floors)
    if why:
        return f"Too old for this hub ({why}). Update it from Manage > Updates."
    if pending:
        why = below_floor(target, pending)
        if why:
            return (f"Will stop working after update {pending.get('version') or ''} "
                    f"({why}). Update it first: Manage > Updates.").replace("  ", " ")
    return ""


def member_kind(w: dict) -> dict:
    """Classify a roster entry for the update service: ``hub``, ``app``
    (Android/desktop app with a built-in worker), ``firmware`` (ESP32: no
    build, firmware caps) or ``worker`` (a pyz worker)."""
    app = w.get("app_release") if isinstance(w.get("app_release"), dict) else {}
    build = w.get("build") if isinstance(w.get("build"), int) else None
    plugins = set(w.get("plugins") or [])
    caps = w.get("caps") or []
    base = {"worker_id": w.get("worker_id"), "name": w.get("name") or "",
            "band": w.get("band"), "build": build, "version": w.get("version")}
    if "is_hub" in (w.get("roles") or ()):
        return {**base, "kind": "hub"}
    if app.get("platform"):
        return {**base, "kind": "app", "platform": str(app.get("platform")),
                "app_version": str(app.get("version") or ""), "app_code": app.get("code")}
    if build is None and plugins and plugins <= {"info", "kvm", "bthid", "serial"} \
            and any(str(c).startswith(("kvm.", "bthid.", "serial.")) for c in caps):
        return {**base, "kind": "firmware", "firmware": "esp32",
                "fw_version": str(w.get("version") or "")}
    return {**base, "kind": "worker"}
