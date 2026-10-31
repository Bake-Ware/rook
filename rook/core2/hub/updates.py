"""Core updates, hub side (docs/castling-updates.md): the builtin ``update``
plugin. An owner rolls a signed core release out to the band's workers,
canaries first, one batch at a time; a worker that doesn't come back healthy
on the new core stops the rollout (and has already rolled itself back).

    update.rollout  {version?, source?, canary?, workers?, batch?, gate?}   returns once it started
    update.status   {}                            releases, the rollout, each worker's core
    update.history  {limit?}                      what happened, newest first

``update.rollout`` signs the release it needs (the code this hub runs, or a
checkout on the hub machine) unless the hub already holds that version, so
going back band-wide is a rollout of an older held version. Each worker gets
``worker.update`` with the release's Drop descriptor, called through
dispatch and the journal as the person who started the rollout; the worker
pulls, verifies, smoke-checks and restarts into it on trial. The hub then
waits up to ``gate`` seconds for the worker to report that release confirmed
(``worker.status``). With the change plugin installed the rollout leaves one
change record when it ends; core never needs it. Plugins keep updating
through distribution (``POST /plugins/push``); the hub updates itself by its
deploy, not here.

Files: ``<data>/releases/<version>.tar.gz`` with ``<version>.json`` beside
it (the newest ``KEEP`` are kept), ``<data>/updates/history.jsonl`` and
``<data>/updates/rollout.json`` (the current or last rollout).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import urllib.request
from pathlib import Path

from rook.kit.ctx import Caller as KitCaller, KitError

from rook.core2 import release
from rook.core2.builtin import UPDATE
from rook.core2.transport.conn import BULK_MAX

log = logging.getLogger("rook.hub.updates")

KEEP = 3                     # releases kept on the hub
GATE = 300                   # seconds a worker has to come back confirmed on the new core
BATCH = 2                    # workers updated at once after the canaries
POLL = 2.0                   # seconds between status checks while a worker restarts
HISTORY_MAX = 1 << 20        # history.jsonl rotates past this


class Updates:
    def __init__(self, hub):
        self.hub = hub
        self.releases = Path(hub.data) / "releases"
        self.folder = Path(hub.data) / "updates"
        self.task: asyncio.Task | None = None
        self.rollout_state: dict | None = self._read(self.folder / "rollout.json")
        if (self.rollout_state or {}).get("status") == "running":      # the hub stopped mid-rollout
            self.rollout_state["status"] = "interrupted"
            self.rollout_state["ended"] = round(time.time(), 3)
            self._save_rollout()
            self._history({"event": "rollout.interrupted", "rollout": self.rollout_state.get("id"),
                           "version": self.rollout_state.get("version")})

    # -- files -------------------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return got if isinstance(got, dict) else None

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _save_rollout(self) -> None:
        self._write(self.folder / "rollout.json", self.rollout_state)

    def _history(self, entry: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.folder / "history.jsonl"
        try:
            if path.exists() and path.stat().st_size > HISTORY_MAX:
                path.replace(path.with_name("history.jsonl.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": round(time.time(), 3), **entry}, default=str) + "\n")
        except OSError as e:
            log.warning("update history not written", extra={"fields": {"error": str(e)}})
        log.info(entry.get("event", "update"), extra={"fields": {k: v for k, v in entry.items() if k != "event"}})

    def _held(self) -> list[dict]:
        out = []
        for meta in sorted(self.releases.glob("*.json")) if self.releases.is_dir() else ():
            m = self._read(meta)
            if m and (self.releases / f"{m.get('version')}.tar.gz").is_file():
                out.append(m)
        return sorted(out, key=lambda m: m.get("at") or 0, reverse=True)

    def _tarball(self, version: str) -> Path:
        if not (isinstance(version, str) and release.SEMVER.fullmatch(version)):
            raise KitError(f"no release {version!r}")
        path = self.releases / f"{version}.tar.gz"
        if not path.is_file():
            raise KitError(f"no release {version} on this hub")
        return path

    # -- caps --------------------------------------------------------------------

    @staticmethod
    def _owner(ctx) -> None:
        if not ctx.caller.owner:
            raise KitError("denied: only an owner updates the band's core")

    async def _prepare(self, ctx, version: str | None = None, source: str | None = None) -> dict:
        """Sign a release of ``source`` (a folder holding ``rook/``; default
        the code this hub runs). The same code again is the same release."""
        import rook
        src = Path(source).expanduser() if source else Path(rook.__file__).resolve().parent.parent
        try:
            version, _, hashes, _ = await asyncio.to_thread(release.plan, src, version)
        except release.ReleaseError as e:
            raise KitError(f"refused: {e}") from None
        digest = release.digest(hashes)
        meta_path = self.releases / f"{version}.json"
        held = self._read(meta_path)
        if held and (self.releases / f"{version}.tar.gz").is_file():
            if held.get("digest") != digest:
                raise KitError(f"refused: release {version} exists with other code; pick a new version")
            return {**held, "new": False}
        self.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = self.releases / f"{version}.tar.gz"
        try:
            sig = await asyncio.to_thread(release.build, src, dest, signer=self.hub.op, hub=self.hub.hub_id,
                                          version=version)
        except (release.ReleaseError, OSError) as e:
            raise KitError(f"refused: {e}") from None
        if release.digest(sig["files"]) != digest:       # the code changed between plan and build
            dest.unlink(missing_ok=True)
            raise KitError("refused: the source changed while the release was built; try again")
        meta = {"version": version, "digest": digest, "files": len(sig["files"]), "size": dest.stat().st_size,
                "sha256": release.sha256_file(dest), "core": sig["core"], "requires": sig["requires"],
                "source": str(src.resolve()), "at": round(time.time(), 3), "by": ctx.caller.person}
        self._write(meta_path, meta)
        self._history({"event": "prepared", "version": version, "by": ctx.caller.person,
                       "agent": ctx.caller.agent, "size": meta["size"]})
        self._prune()
        return {**meta, "new": True}

    def _prune(self) -> None:
        busy = (self.rollout_state or {}).get("version") if self.task and not self.task.done() else None
        for m in self._held()[KEEP:]:
            if m.get("version") != busy:
                (self.releases / f"{m['version']}.tar.gz").unlink(missing_ok=True)
                (self.releases / f"{m['version']}.json").unlink(missing_ok=True)

    def _workers(self, version: str) -> list[dict]:
        """Every known worker with its core and whether a rollout can take it."""
        hub, out = self.hub, []
        for k in sorted(hub.known.values(), key=lambda k: k.name):
            w = hub.dispatcher.workers.get(k.id)
            row = {"name": k.name, "id": k.id, "version": k.version, "online": k.online}
            if not k.online:
                row["skip"] = "offline"
            elif w is None or "worker.update" not in w.caps:
                row["skip"] = "can't update its core over the band (an app's worker, or an older core)"
            elif k.version == version:
                row["skip"] = f"already on {version}"
            out.append(row)
        return out

    def _holds(self, version: str | None) -> bool:
        return bool(version) and any(m.get("version") == version for m in self._held())

    async def rollout(self, ctx, version: str | None = None, source: str | None = None,
                      canary: list | None = None, workers: list | None = None,
                      batch: int = BATCH, gate: int = GATE) -> dict:
        """Start updating the band's workers to ``version``: the canaries
        (default the first worker in line) one by one, then the rest
        ``batch`` at a time. The first worker that fails stops it. Without
        ``version`` the code this hub runs is signed first; with ``source``
        that checkout is (as ``version``, or named by its digest)."""
        self._owner(ctx)
        if self.task is not None and not self.task.done():
            raise KitError(f"a rollout to {self.rollout_state.get('version')} is running; wait for it")
        made = None
        if source or not version:
            made = await self._prepare(ctx, version, source)
            version = made["version"]
        elif not self._holds(version):
            raise KitError(f"no release {version} on this hub: leave version blank to release the code it "
                           "runs, or give a source to sign as it")
        path = self._tarball(version)
        rows = self._workers(version)
        names = {r["name"] for r in rows}
        for n in [*(canary or ()), *(workers or ())]:
            if n not in names:
                raise KitError(f"unknown worker: {n}")
        wanted = [r for r in rows if not workers or r["name"] in workers]
        ready = [r for r in wanted if "skip" not in r]
        if not ready:
            raise KitError("no worker to update: " + "; ".join(f"{r['name']}: {r['skip']}" for r in wanted)
                           if wanted else "no worker to update")
        canaries = [r for r in ready if r["name"] in (canary or ())] or ready[:1]
        rest = [r for r in ready if r not in canaries]
        rid = secrets.token_hex(6)
        self.rollout_state = {
            "id": rid, "version": version, "status": "running", "by": ctx.caller.person,
            "agent": ctx.caller.agent, "started": round(time.time(), 3), "batch": batch, "gate": gate,
            "workers": [{"name": r["name"], "id": r["id"], "from": r["version"],
                         "canary": r in canaries, "state": "waiting"} for r in canaries + rest]
                       + [{"name": r["name"], "id": r["id"], "from": r["version"], "state": "skipped",
                           "reason": r["skip"]} for r in wanted if "skip" in r],
        }
        self._save_rollout()
        self._history({"event": "rollout.started", "rollout": rid, "version": version,
                       "by": ctx.caller.person, "agent": ctx.caller.agent,
                       "canary": [r["name"] for r in canaries], "rest": [r["name"] for r in rest]})
        who = KitCaller(person=ctx.caller.person, agent=ctx.caller.agent,
                        why=f"Update the core of the band's workers to {version} (rollout {rid}).")
        self.task = asyncio.get_running_loop().create_task(self._run(path, who))
        return {"rollout": rid, "version": version, "signed": bool(made and made["new"]),
                "canary": [r["name"] for r in canaries], "rest": [r["name"] for r in rest],
                "skipped": {r["name"]: r["skip"] for r in wanted if "skip" in r}}

    async def status(self, ctx) -> dict:
        ro = self.rollout_state
        version = (ro or {}).get("version") or ""
        return {"releases": self._held(), "rollout": ro,
                "workers": [{k: v for k, v in r.items() if k != "skip"} for r in self._workers(version)]}

    async def history(self, ctx, limit: int = 50) -> dict:
        lines = []
        for name in ("history.jsonl.1", "history.jsonl"):
            try:
                lines += (self.folder / name).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return {"entries": out}

    # -- the rollout ---------------------------------------------------------------

    async def _run(self, path: Path, who: KitCaller) -> None:
        ro = self.rollout_state
        queue = [w for w in ro["workers"] if w["state"] == "waiting"]
        canaries, rest = [w for w in queue if w.get("canary")], [w for w in queue if not w.get("canary")]
        try:
            blob = await asyncio.to_thread(path.read_bytes)
            ok = True
            for w in canaries:
                ok = await self._one(w, blob, who)
                if not ok:
                    break
            # Drop serves at most BULK_MAX per band at once (oldest offers go first): never
            # offer more copies at a time than fit, or one is gone before its worker pulls it.
            step = max(1, min(int(ro.get("batch") or BATCH), BULK_MAX // max(1, len(blob))))
            for i in range(0, len(rest), step):
                if not ok:
                    break
                ok = all(await asyncio.gather(*(self._one(w, blob, who) for w in rest[i:i + step])))
            status = "done" if ok else "failed"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as e:          # a bug must still end the rollout on record
            log.exception("rollout failed")
            ro["error"] = f"{type(e).__name__}: {e}"
            status = "failed"
        finally:
            for w in ro["workers"]:
                if w["state"] in ("waiting", "updating"):
                    w["state"], w["reason"] = "not started", w.get("reason") or "the rollout stopped"
            ro["status"], ro["ended"] = status, round(time.time(), 3)
            self._save_rollout()
            updated = [w["name"] for w in ro["workers"] if w["state"] == "updated"]
            failed = {w["name"]: w.get("reason") for w in ro["workers"] if w["state"] == "failed"}
            self._history({"event": f"rollout.{status}", "rollout": ro["id"], "version": ro["version"],
                           "updated": updated, "failed": failed})
            if status != "interrupted" and (updated or failed):
                ro["change"] = await self._record(ro, who, status, updated, failed)
                self._save_rollout()

    async def _record(self, ro: dict, who: KitCaller, status: str, updated: list, failed: dict) -> str | None:
        """The rollout's one change record (when the change plugin is installed)."""
        froms = sorted({w.get("from") or "the installed core" for w in ro["workers"] if w["name"] in updated
                        or w["name"] in failed})
        try:
            return await self.hub.note_change(who, {
                "what": f"Update the core of {len(updated) + len(failed)} worker(s) to {ro['version']} "
                        f"(rollout {ro['id']})",
                "why": who.why, "category": "update", "workers": [*updated, *failed], "caps": ["worker.update"],
                "risk": "Each worker restarts on the new core; a core that doesn't serve its home hub is rolled "
                        "back by the worker itself.",
                "testing": f"Each worker smoke-checks the release, then must report it confirmed within "
                           f"{ro.get('gate', GATE)}s; the first that doesn't stops the rollout.",
                "rollback": f"update.rollout of the version they had ({', '.join(froms)}), or worker.update "
                            "{version} on one worker.",
                "steps": "Canaries one by one, then the rest a batch at a time: the hub offers the release over "
                         "Drop and calls worker.update on each.",
                "outcome": (f"{status}: updated {', '.join(updated) or 'none'}"
                            + "".join(f"; {n} failed: {r}" for n, r in failed.items()))[:1000]})
        except Exception as e:          # the record is best effort; the history and journal have it
            log.warning("rollout change record not written", extra={"fields": {"error": str(e)}})
            return None

    async def _one(self, w: dict, blob: bytes, who: KitCaller) -> bool:
        """Update one worker and wait for it to come back confirmed."""
        hub, ro = self.hub, self.rollout_state
        version, gate = ro["version"], float(ro.get("gate", GATE))
        w["state"], w["at"] = "updating", round(time.time(), 3)
        self._save_rollout()
        k = hub.known.get(w["id"])
        if k is None:
            return self._done(w, False, "no longer known to the hub")
        try:
            desc = hub.transport.offer(k.band, blob, to=w["id"])
        except Exception as e:
            return self._done(w, False, f"release not offered: {e}")
        reply = await hub.call_as(who, "worker.update", {"bundle": desc}, target=w["id"])
        if reply.get("held"):
            return self._done(w, False, f"worker.update waits for a person (hold {reply.get('hold')}); once "
                                        "it is answered the worker updates, and a new rollout skips it")
        if not reply.get("ok"):
            return self._done(w, False, str(reply.get("error") or "worker.update failed"))
        deadline = time.monotonic() + gate
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL)
            st = await hub.call_as(who, "worker.status", {}, target=w["id"])
            rel = (st.get("result") or {}).get("release") if st.get("ok") else None
            if not isinstance(rel, dict):
                continue                      # still restarting
            failed = rel.get("failed") or {}
            if failed.get("version") == version and rel.get("running") != version:
                return self._done(w, False, f"rolled back on the worker: {failed.get('reason')}")
            if rel.get("running") == version and not rel.get("trial"):
                return self._done(w, True)
        return self._done(w, False, f"not back on {version} within {gate:g}s")

    def _done(self, w: dict, ok: bool, reason: str | None = None) -> bool:
        w["state"] = "updated" if ok else "failed"
        if reason:
            w["reason"] = reason
        w["ended"] = round(time.time(), 3)
        self._save_rollout()
        self._history({"event": "worker.updated" if ok else "worker.failed", "rollout": self.rollout_state["id"],
                       "worker": w["name"], "version": self.rollout_state["version"],
                       **({"reason": reason} if reason else {})})
        return ok

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


# -- the desktop app's update check (R10) -------------------------------------------------

RELEASES_API = "https://api.github.com/repos/Bake-Ware/rook/releases/latest"
DESKTOP_PREFIX = "rook-desktop-"
FEED_SECS = 6 * 3600          # GitHub is asked at most this often
FEED_TIMEOUT = 15
FEED_RETRY = 600            # after a failed fetch
_feed: dict = {"at": 0.0, "body": None}
_feed_lock = threading.Lock()


def _latest_release() -> dict:
    req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": "rook-hub"})
    with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as r:
        return json.loads(r.read(4 << 20))


def desktop_feed(fetch=_latest_release, now: float | None = None) -> dict:
    """``GET /updates/desktop.json``: the desktop app's files in Rook's latest
    GitHub release (the app picks its platform's and compares versions, and
    only takes links into Rook's releases). Cached; a failed fetch serves the
    last good answer, else an empty list."""
    now = time.time() if now is None else now
    with _feed_lock:
        if _feed["body"] is not None and now - _feed["at"] < FEED_SECS:
            return _feed["body"]
        try:
            rel = fetch()
            downloads = [{"name": a["name"], "url": a["browser_download_url"]} for a in rel.get("assets") or ()
                         if isinstance(a, dict) and str(a.get("name", "")).startswith(DESKTOP_PREFIX)
                         and isinstance(a.get("browser_download_url"), str)]
            body = {"ok": True, "release": str(rel.get("tag_name") or ""), "downloads": downloads}
        except Exception as e:       # offline, rate-limited, a bad reply: try again in a while
            log.info("desktop update feed not fetched", extra={"fields": {"error": str(e)[:200]}})
            _feed.update(at=now - FEED_SECS + FEED_RETRY,
                         body=_feed["body"] or {"ok": True, "release": "", "downloads": []})
            return _feed["body"]
        _feed.update(at=now, body=body)
        return body


def install(hub) -> Updates:
    """Register the builtin ``update`` plugin on the hub node."""
    up = Updates(hub)
    hub.plugins.add_builtin(UPDATE, {"update.rollout": up.rollout, "update.status": up.status,
                                       "update.history": up.history})
    return up
