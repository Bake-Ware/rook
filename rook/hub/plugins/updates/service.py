"""The update service: what is installed, what is available, and applying it.

Owned by the hub (worker ``rook``), driven by Manage > Updates and the
``updates.*`` caps. State lives in the updates directory
(rook/hub/update_feed.py ``updates_dir``):

* ``state.json`` - in-flight hub update and worker rollout, acknowledgements,
  what auto mode already tried, which releases were announced;
* ``history.jsonl`` - one line per hub update, rollout, rollback, APK publish;
* ``summary.json`` - the public summary the dashboard reads (pending breaking
  floors for the Workers page, the newest release for the desktop check);
* ``github/<owner>__<repo>/`` - release lists and verified downloads;
* ``worker/<build>/`` and ``feed.json`` - the worker feed (update_feed.py);
* ``apk/`` - the APK published from a release; ``hub/`` - hub release
  manifests made from release sdists; ``logs/`` - update process output.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ....core import compat
from ...release_meta import (below_floor, cut_off, floors_of, raised_against, version_lt,
                             version_tuple)
from ...update_feed import WorkerFeed, member_kind, published_apk
from . import hubself
from .github import ReleaseError, ReleaseFeed, verify_attestation
from .rollout import RolloutEngine, new_rollout, online

log = logging.getLogger("rook.hub.plugins.updates.service")

MODES = ("off", "notify", "auto")
HUB_APPLY_TIMEOUT = 20 * 60.0      # after this, an unconfirmed hub update has failed
HISTORY_MAX = 500


class Refused(Exception):
    """An operator request that cannot go ahead as asked (shown to them)."""

    def __init__(self, message: str, **data: Any) -> None:
        super().__init__(message)
        self.data = data


def current_floors() -> dict:
    return {"min_worker_build": compat.MIN_WORKER_BUILD,
            "min_android_app": compat.MIN_ANDROID_APP, "min_hub_build": compat.MIN_HUB_BUILD}


def in_window(window: str, now: datetime.datetime | None = None) -> bool:
    """``"02:00-05:00"`` (hub local time, may wrap midnight); "" = any time."""
    window = (window or "").strip()
    if not window:
        return True
    try:
        a, b = (x.strip() for x in window.split("-", 1))
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
    except ValueError:
        return False
    now = now or datetime.datetime.now()
    t, start, end = now.hour * 60 + now.minute, ah * 60 + am, bh * 60 + bm
    return start <= t < end if start <= end else (t >= start or t < end)


class UpdateService:
    def __init__(self, root: Path, settings: Callable[[], dict], client: Callable[[], Any],
                 notify: Callable[[str], Any] | None = None,
                 bundle_paths: Callable[[], tuple[Path, Path] | None] | None = None,
                 signer: Callable[[dict], dict] | None = None,
                 hub_signing_key: Callable[[], Any] | None = None) -> None:
        self.root = Path(root)
        self._settings = settings
        self._client = client
        self._notify = notify
        self._bundle_paths = bundle_paths
        self._signer = signer
        self._hub_key = hub_signing_key
        self.feed = WorkerFeed(self.root)
        self._feeds: dict[str, ReleaseFeed] = {}
        self._rollout_task: asyncio.Task | None = None
        self._engine: RolloutEngine | None = None
        self._lock = asyncio.Lock()
        self._fw_cache: dict[str, tuple[float, dict]] = {}
        self.state = self._load_state()

    # -- persistence ------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self) -> None:
        hubself.json_dump(self.root / "state.json", self.state)

    def record(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "history.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > HISTORY_MAX * 2:
                path.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def history(self, n: int = 50) -> list[dict]:
        try:
            lines = (self.root / "history.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return list(reversed(out))

    # -- settings ---------------------------------------------------------------
    def s(self, name: str, default: Any = None) -> Any:
        v = (self._settings() or {}).get(name)
        return default if v is None else v

    @property
    def mode(self) -> str:
        m = str(self.s("mode", "notify"))
        return m if m in MODES else "notify"

    @property
    def prerelease(self) -> bool:
        return self.s("channel", "stable") == "prerelease"

    def release_feed(self, repo: str) -> ReleaseFeed:
        api, token = str(self.s("api_base", "") or ""), str(self.s("token", "") or "")
        key = f"{repo}|{api}"
        f = self._feeds.get(key)
        if f is None or f.token != token:
            f = ReleaseFeed(repo, self.root / "github", api=api or None, token=token)
            self._feeds[key] = f
        return f

    @property
    def rook(self) -> ReleaseFeed:
        return self.release_feed(str(self.s("repo", "Bake-Ware/rook")))

    @property
    def relay(self) -> ReleaseFeed:
        return self.release_feed(str(self.s("relay_repo", "Bake-Ware/telesthete")))

    # -- checking -----------------------------------------------------------------
    async def check(self, force: bool = False) -> dict:
        """Poll the Rook and relay releases (when due, or now with ``force``)
        and read the metadata of every release newer than this hub."""
        interval = max(1.0, float(self.s("check_hours", 6))) * 3600
        res = await self.rook.poll(interval, force=force)
        await self.relay.poll(interval, force=force)
        hub = hubself.current_hub()
        for rel in self.rook.releases(self.prerelease)[:6]:
            if self._newer(rel, hub) or rel["tag"].lstrip("v") == hub["version"]:
                await self.rook.meta(rel)
        self.write_summary()
        await self._announce_new()
        return res

    @staticmethod
    def _newer(rel: dict, hub: dict) -> bool:
        meta = rel.get("meta") or {}
        if meta.get("build") and hub.get("build"):
            return int(meta["build"]) > int(hub["build"])
        if not hub.get("version"):
            return True
        return version_tuple(hub["version"]) < version_tuple(rel["tag"])

    def latest(self) -> dict | None:
        rels = self.rook.releases(self.prerelease)
        return rels[0] if rels else None

    async def _announce_new(self) -> None:
        if self.mode == "off" or not self.s("notify_chat", True) or self._notify is None:
            return
        latest, hub = self.latest(), hubself.current_hub()
        if latest is None or not self._newer(latest, hub):
            return
        seen = self.state.setdefault("announced", {})
        if latest["tag"] in seen:
            return
        seen[latest["tag"]] = time.time()
        self.save()
        meta = latest.get("meta") or {}
        text = (f"Rook {latest['version']} is available (this hub runs "
                f"{hub['version'] or hub['name'] or 'an older build'}). "
                + ("It is a BREAKING update: some devices must be updated first. "
                   if meta.get("breaking") else "")
                + "Open Manage > Updates on the dashboard to review it.")
        try:
            r = await self._notify(text)
            ok = not isinstance(r, dict) or bool(r.get("ok", True))
            self.record({"kind": "notify", "event": "announced", "tag": latest["tag"],
                         "result": "ok" if ok else "skipped",
                         "detail": "posted to chat" if ok else
                         f"not posted to chat ({(r or {}).get('error') or 'no channel'}); "
                         "shown on Manage > Updates"})
        except Exception:
            log.debug("could not announce %s", latest["tag"], exc_info=True)

    # -- the band as the update service sees it ----------------------------------
    def roster(self) -> dict:
        c = self._client()
        return dict(getattr(c, "workers", {}) or {}) if c is not None else {}

    def members(self) -> list[dict]:
        now = time.time()
        newest = self.feed.newest_build()
        released = self.feed.released_build()
        apk = self.app_available()
        fw = self._firmware_latest()
        floors = current_floors()
        pending, pending_v = None, ""
        latest = self.latest()
        if latest and (latest.get("meta") or {}).get("breaking") \
                and self._newer(latest, hubself.current_hub()):
            pending, pending_v = floors_of(latest["meta"]), latest["version"]
        out = []
        for wid, w in self.roster().items():
            m = member_kind({**w, "worker_id": wid})
            if m["kind"] == "hub":
                continue
            facts = w.get("facts") if isinstance(w.get("facts"), dict) else {}
            m["os"] = (m.get("platform") if m["kind"] == "app"
                       else "firmware" if m["kind"] == "firmware"
                       else str(facts.get("os") or "unknown").lower())
            m["online"] = online(w, now)
            m["can_push"] = m["kind"] == "worker" and "worker.apply" in (w.get("caps") or [])
            m["note"] = below_floor(m, floors)
            why = below_floor(m, pending) if pending else ""
            m["pending_note"] = f"stops working with {pending_v}: {why}" if why else ""
            upd = (w.get("hb") or {}).get("worker")
            m["rolled_back"] = upd.get("update") if isinstance(upd, dict) else ""
            if m["kind"] == "worker":
                b = m.get("build")
                m["target"] = newest
                m["state"] = ("unknown" if not isinstance(b, int) or b <= 0 else
                              "current" if b >= newest else "behind")
                if not m["can_push"] and m["state"] == "behind":
                    m["state"] = "cannot"
                m["released"] = released
            elif m["kind"] == "app":
                if m.get("platform") == "android" and apk:
                    m["target"] = apk.get("version_name")
                    m["state"] = ("behind" if version_lt(m.get("app_version"), apk.get("version_name"))
                                  else "current")
                    m["can_notify"] = "device.update" in (w.get("caps") or [])
                else:
                    m["target"], m["state"] = None, "unknown"
            elif m["kind"] == "firmware":
                latest = fw.get("esp32")
                m["target"] = latest
                m["state"] = ("behind" if latest and version_lt(m.get("fw_version"), latest)
                              else "current" if latest else "unknown")
            out.append(m)
            if "dongle.status" in (w.get("caps") or []):
                d = self._fw_cache.get(wid)
                info = d[1] if d else {}
                ver = str(info.get("version") or "")
                out.append({"worker_id": f"{wid}:rp2040", "host": wid,
                            "name": f"RP2040 dongle on {m['name']}", "band": m.get("band"),
                            "kind": "firmware", "firmware": "rp2040", "os": "firmware",
                            "fw_version": ver, "online": m["online"], "can_push": False,
                            "target": fw.get("rp2040"),
                            "state": ("unknown" if not ver or not fw.get("rp2040") else
                                      "behind" if version_lt(ver, fw.get("rp2040")) else "current"),
                            "note": "", "build": None})
        out.sort(key=lambda m: (m["kind"], (m.get("name") or "").lower()))
        return out

    async def refresh_firmware(self) -> int:
        """Ask hosts with an RP2040 dongle for its firmware version (read-only
        ``dongle.status``; cached 10 minutes)."""
        c, n = self._client(), 0
        if c is None:
            return 0
        now = time.time()
        for wid, w in self.roster().items():
            if "dongle.status" not in (w.get("caps") or []) or not online(w, now):
                continue
            cached = self._fw_cache.get(wid)
            if cached and now - cached[0] < 600:
                continue
            try:
                reply = await c.call(cap="dongle.status", args={}, target=wid, timeout=6.0,
                                     identity="system:rook-hub-updates")
                res = reply.get("result") if reply.get("ok") else {}
                self._fw_cache[wid] = (now, res if isinstance(res, dict) else {})
                n += 1
            except Exception:
                self._fw_cache[wid] = (now, {})
        return n

    def _firmware_latest(self) -> dict:
        for rel in self.rook.releases(self.prerelease):
            fw = (rel.get("meta") or {}).get("firmware")
            if fw:
                return fw
        return {}

    def app_available(self) -> dict | None:
        """The APK version phones can update to from this hub (published by
        this service, else the operator's rook/remote/rook-worker-apk.json)."""
        pub = published_apk(self.root)
        if pub:
            return {**pub[1], "source": "release"}
        legacy = Path(__file__).resolve().parents[3] / "remote" / "rook-worker-apk.json"
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version_name"):
                return {**data, "source": "hub"}
        except (OSError, ValueError):
            pass
        return None

    # -- breaking analysis --------------------------------------------------------
    def analyse(self, rel: dict, members: list[dict] | None = None) -> dict:
        meta = rel.get("meta") or {}
        members = self.members() if members is None else members
        if not meta:
            return {"known": False, "breaking": False, "reasons": [], "cut_off": [],
                    "note": rel.get("meta_error") or "this release carries no update metadata"}
        reasons = list(meta.get("breaking_reasons") or [])
        for r in raised_against(meta, current_floors()):
            if r not in reasons:
                reasons.append(r)
        floors = floors_of(meta)
        newest = self.feed.newest_build()
        apk = self.app_available() or {}
        out = []
        for m in cut_off(members, floors):
            fix, how = "manual", ""
            if m["kind"] == "worker" and m.get("can_push") and newest >= (floors["min_worker_build"] or 0):
                fix, how = "update_first", f"update it now to build {newest} (the hub has it)"
            elif m["kind"] == "worker":
                fix, how = "manual", ("reinstall it with the one-line install command"
                                      if not m.get("can_push") else
                                      "it gets the new build after the hub update; until then it "
                                      "may stop working")
            elif m["kind"] == "app" and m.get("platform") == "android":
                if apk.get("version_name") and not version_lt(apk["version_name"],
                                                              floors.get("min_android_app") or "0"):
                    fix, how = "app", (f"install Rook app {apk['version_name']} on it "
                                       "(Manage > Updates > Phones and tablets)")
                else:
                    fix, how = "app", "install a newer Rook app on it by hand: the app does not update itself"
            out.append({"worker_id": m["worker_id"], "name": m["name"], "kind": m["kind"],
                        "why": m["why"], "fix": fix, "how": how, "online": m.get("online")})
        breaking = bool(meta.get("breaking") or reasons or out)
        ack = (self.state.get("acks") or {}).get(rel["tag"])
        return {"known": True, "breaking": breaking, "reasons": reasons, "cut_off": out,
                "floors": floors, "acked": ack, "note": ""}

    # -- the overview the wizard renders ----------------------------------------------
    def overview(self) -> dict:
        hub = hubself.current_hub()
        kind = hubself.detect(str(self.s("repo", "Bake-Ware/rook")))
        members = self.members()
        rels = []
        for rel in self.rook.releases(self.prerelease)[:8]:
            a = self.analyse(rel, members)
            rels.append({k: rel.get(k) for k in ("tag", "version", "title", "notes", "prerelease",
                                                 "published_at", "html_url", "meta", "meta_error")}
                        | {"newer": self._newer(rel, hub), "analysis": a,
                           "can_apply": bool(kind.get("can_apply")),
                           "apply_note": hubself.describe_apply(kind, rel.get("meta") or {},
                                                                rel["tag"]),
                           "has_apk": "rook-worker.apk" in rel.get("assets", {})})
        latest = next((r for r in rels if r["newer"]), None)
        relay_latest = self.relay.releases(self.prerelease)
        st = self.rook.state
        hub_apply = self.state.get("hub_apply")
        if hub_apply:
            hub_apply = {**hub_apply, "log": hubself.log_tail(Path(hub_apply.get("log_path", "")), 15)}
        return {
            "mode": self.mode, "channel": "prerelease" if self.prerelease else "stable",
            "window": str(self.s("window", "") or ""),
            "in_window": in_window(str(self.s("window", "") or "")),
            "check": {"repo": self.rook.repo, "checked_at": st.get("checked_at"),
                      "ok_at": st.get("ok_at"), "error": st.get("error"),
                      "next_at": st.get("next_at"), "relay_repo": self.relay.repo},
            "hub": {"current": hub, "install": kind, "latest": latest["tag"] if latest else None,
                    "update_available": latest is not None,
                    "history": (hubself.deploy_history(kind["config"])
                                if kind.get("kind") == "rook-hub" else [])},
            "relay": {"pinned": hub.get("relay"),
                      "latest": relay_latest[0]["tag"] if relay_latest else None},
            "floors": current_floors(),
            "releases": rels,
            "feed": {"released": self.feed.released_build(), "newest": self.feed.newest_build(),
                     "builds": self.feed.builds()},
            "members": members,
            "app": self.app_available(),
            "firmware": self._firmware_latest(),
            "rollout": self.state.get("rollout"),
            "hub_apply": hub_apply,
            "history": self.history(40),
        }

    def write_summary(self) -> None:
        latest = self.latest()
        pending = None
        hub = hubself.current_hub()
        if latest and self._newer(latest, hub) and (latest.get("meta") or {}).get("breaking"):
            pending = {**floors_of(latest["meta"]), "version": latest["version"]}
        hubself.json_dump(self.root / "summary.json", {
            "updated_at": time.time(),
            "latest": ({k: latest.get(k) for k in ("tag", "version", "html_url", "published_at")}
                       | {"assets": {n: a["url"] for n, a in latest.get("assets", {}).items()}}
                       if latest else None),
            "pending_floors": pending,
        })

    # -- hub self-update -------------------------------------------------------------
    def _find(self, tag: str) -> dict:
        rel = self.rook.find(tag)
        if rel is None or (rel.get("prerelease") and not self.prerelease):
            raise Refused(f"{tag} is not an available release on the {('prerelease' if self.prerelease else 'stable')} channel")
        return rel

    def acknowledge(self, tag: str, actor: str) -> dict:
        rel = self._find(tag)
        a = self.analyse(rel)
        ack = {"by": actor, "at": time.time(), "cut_off": [m["name"] for m in a["cut_off"]]}
        self.state.setdefault("acks", {})[tag] = ack
        self.save()
        self.record({"kind": "hub", "event": "acknowledged", "tag": tag, "by": actor,
                     "detail": f"accepted that {len(ack['cut_off'])} device(s) may stop working: "
                               + ", ".join(ack["cut_off"])})
        return ack

    async def apply_hub(self, tag: str, actor: str) -> dict:
        async with self._lock:
            if self.state.get("hub_apply"):
                raise Refused("a hub update is already in progress")
            rel = self._find(tag)
            await self.rook.meta(rel)
            a = self.analyse(rel)
            ack = (self.state.get("acks") or {}).get(tag)
            if not a["known"] and not ack:
                raise Refused("This release has no update metadata, so what it changes for your "
                              "devices is unknown. Acknowledge it to go ahead.", needs_ack=True,
                              cut_off=[])
            if a["cut_off"] and not (ack and set(ack.get("cut_off", [])) >= {m["name"] for m in a["cut_off"]}):
                raise Refused(f"{len(a['cut_off'])} device(s) would stop working after this update. "
                              "Update them first, or acknowledge it.", needs_ack=True,
                              cut_off=a["cut_off"])
            kind = hubself.detect(self.rook.repo)
            if not kind.get("can_apply"):
                raise Refused(kind.get("why") or "this hub cannot update itself",
                              manual=kind.get("manual", "").replace("{tag}", tag)
                              .replace("{version}", rel["version"]))
            meta = rel.get("meta") or {}
            hub = hubself.current_hub()
            op = {"id": time.strftime("%Y%m%dT%H%M%S"), "tag": tag, "version": rel["version"],
                  "target_build": meta.get("build") or 0, "target_name": meta.get("name") or "",
                  "from_version": hub["version"], "from_build": hub["build"],
                  "from_tag": ("v" + hub["version"]) if hub["version"] else "",
                  "kind": kind["kind"], "by": actor, "started_at": time.time(),
                  "log_path": str(self.root / "logs" / f"hub-{tag}.log")}
            log_path = Path(op["log_path"])
            if kind["kind"] == "rook-hub":
                if not meta.get("name") or not meta.get("build"):
                    raise Refused("this release has no build name, so `rook hub deploy` cannot "
                                  "place it; update by hand")
                sdist = await self.rook.fetch(rel, f"rook-{meta.get('version') or rel['version']}.tar.gz")
                await self._attest(sdist)
                tarball = hubself.repack_sdist(sdist, meta["name"], self.root / "hub")
                key = self._hub_key() if self._hub_key else None
                manifest = hubself.hub_release_manifest(tarball, meta, key)
                mpath = self.root / "hub" / f"rook-hub-{meta['name']}.json"
                hubself.json_dump(mpath, manifest)
                argv, env = hubself.rook_hub_argv(kind["config"], "deploy", str(mpath), "--yes")
                op["launch"] = hubself.launch(argv, unit=f"rook-hub-update-{op['id']}",
                                              log_path=log_path, env=env,
                                              user_scope=kind.get("scope") == "user")
            elif kind["kind"] == "installer":
                script = await self.rook.fetch(rel, "install-hub.sh")
                await self._attest(script)
                env = {"ROOK_REPO": self.rook.repo}
                gh = os.environ.get("ROOK_GITHUB", "")
                if gh:
                    env["ROOK_GITHUB"] = gh
                op["launch"] = hubself.launch(hubself.installer_argv(script, kind, tag),
                                              unit=f"rook-hub-update-{op['id']}",
                                              log_path=log_path, env=env, user_scope=True)
            else:
                raise Refused(kind.get("why") or "this hub cannot update itself")
            self.state["hub_apply"] = op
            self.save()
            self.record({"kind": "hub", "event": "started", "tag": tag, "by": actor,
                         "from": hub["version"] or hub["name"], "to": rel["version"],
                         "detail": hubself.describe_apply(kind, meta, tag)})
            return op

    async def _attest(self, path: Path) -> None:
        if not self.s("verify_attestations", False):
            return
        res = await verify_attestation(path, self.rook.repo)
        if res.get("checked") and not res.get("ok"):
            raise Refused(f"{path.name}: build provenance check failed ({res.get('detail')})")

    def check_hub_apply(self) -> None:
        """After a restart: did the hub update land?"""
        op = self.state.get("hub_apply")
        if not op:
            return
        hub = hubself.current_hub()
        landed = ((op.get("target_build") and hub["build"] >= op["target_build"])
                  or (hub["version"] and hub["version"] == op.get("version")))
        if landed:
            self.state["hub_apply"] = None
            self.save()
            self.record({"kind": "hub", "event": "finished", "result": "ok", "tag": op["tag"],
                         "by": op.get("by"), "from": op.get("from_version"), "to": hub["version"],
                         "detail": f"the hub runs {hub['name'] or hub['version']}"})
            return
        if time.time() - float(op.get("started_at") or 0) > HUB_APPLY_TIMEOUT:
            tail = hubself.log_tail(Path(op.get("log_path", "")), 8)
            self.state["hub_apply"] = None
            self.save()
            self.record({"kind": "hub", "event": "finished", "result": "failed", "tag": op["tag"],
                         "by": op.get("by"), "from": op.get("from_version"),
                         "to": op.get("version"),
                         "detail": "the hub is still on the old release (the update failed or "
                                   "was rolled back)" + (f": {tail.splitlines()[-1]}" if tail else "")})

    async def rollback_hub(self, actor: str) -> dict:
        kind = hubself.detect(self.rook.repo)
        if kind.get("kind") == "rook-hub" and kind.get("can_apply"):
            argv, env = hubself.rook_hub_argv(kind["config"], "rollback", "--yes")
            log_path = self.root / "logs" / f"hub-rollback-{int(time.time())}.log"
            info = hubself.launch(argv, unit=f"rook-hub-rollback-{int(time.time())}",
                                  log_path=log_path, env=env, user_scope=kind.get("scope") == "user")
            self.record({"kind": "hub", "event": "rollback", "by": actor, "result": "started",
                         "detail": "rook hub rollback: back to the previous release"})
            return {"ok": True, "launch": info}
        if kind.get("kind") == "installer" and kind.get("can_apply"):
            prev = next((e for e in self.history(200) if e.get("kind") == "hub"
                         and e.get("event") == "finished" and e.get("result") == "ok"), None)
            frm = (prev or {}).get("from")
            if not frm:
                raise Refused("no earlier release is recorded to go back to")
            return await self.apply_hub_version(f"v{frm}", actor, rollback=True)
        raise Refused(kind.get("why") or "this hub cannot roll itself back",
                      manual="rook hub rollback --yes" if kind.get("kind") == "rook-hub" else "")

    async def apply_hub_version(self, tag: str, actor: str, rollback: bool = False) -> dict:
        rel = self.rook.find(tag)
        if rel is None:
            raise Refused(f"{tag} is not a known release")
        self.state.setdefault("acks", {}).setdefault(tag, {"by": actor, "at": time.time(),
                                                           "cut_off": [], "rollback": rollback})
        return await self.apply_hub(tag, actor)

    # -- workers --------------------------------------------------------------------
    def resolve(self, selection: dict) -> list[dict]:
        """Selection -> pyz worker members: ``{"mode": "all"}``, ``{"mode":
        "band", "value": label}``, ``{"mode": "os", "value": "linux"}`` or
        ``{"mode": "ids", "value": [worker_id, ...]}``."""
        mode = (selection or {}).get("mode", "all")
        value = (selection or {}).get("value")
        ws = [m for m in self.members() if m["kind"] == "worker"]
        if mode == "all":
            return ws
        if mode == "band":
            return [m for m in ws if m.get("band") == value]
        if mode == "os":
            return [m for m in ws if m.get("os") == str(value or "").lower()]
        if mode == "ids":
            ids = set(value or [])
            return [m for m in ws if m["worker_id"] in ids]
        raise Refused(f"unknown selection {mode!r}")

    def pick_canaries(self, targets: list[dict], wanted: list[str] | None = None) -> list[str]:
        ids = {t["worker_id"] for t in targets}
        if wanted:
            got = [w for w in wanted if w in ids]
            if got:
                return got
        names = [n.strip().lower() for n in str(self.s("canary", "") or "").split(",") if n.strip()]
        got = [t["worker_id"] for t in targets if t["name"].lower() in names]
        if got:
            return got
        build = self.feed.newest_build()
        live = [t for t in targets if t.get("online") and t.get("can_push")
                and isinstance(t.get("build"), int) and t["build"] < build]
        return [sorted(live, key=lambda t: t["name"].lower())[0]["worker_id"]] if live else []

    async def start_rollout(self, selection: dict, actor: str, build: int | None = None,
                            canaries: list[str] | None = None) -> dict:
        async with self._lock:
            cur = self.state.get("rollout")
            if cur and cur.get("status") == "running":
                raise Refused("a rollout is already running")
            build = int(build or self.feed.newest_build())
            manifest = self.feed.manifest_for(build)
            if not manifest or not self.feed.pyz_for(build):
                raise Refused(f"worker build {build} is not on this hub")
            targets = [t for t in self.resolve(selection)
                       if not (isinstance(t.get("build"), int) and t["build"] >= build)]
            if not targets:
                raise Refused("every selected worker is already on this build")
            can = self.pick_canaries(targets, canaries)
            ro = new_rollout(build, targets, can, by=actor,
                             release_after=(selection or {}).get("mode", "all") == "all",
                             gate_secs=float(self.s("gate_secs", 300)),
                             settle_secs=float(self.s("settle_secs", 30)),
                             batch=int(self.s("batch", 2)),
                             note=f"{len(targets)} worker(s), canary: "
                                  + (", ".join(t["name"] for t in targets if t["worker_id"] in can)
                                     or "none"))
            self.state["rollout"] = ro
            self.save()
            self.record({"kind": "workers", "event": "started", "build": build, "by": actor,
                         "rollout": ro["id"], "detail": ro["note"]})
            self._spawn(ro, manifest)
            return ro

    def _spawn(self, ro: dict, manifest: dict) -> None:
        c = self._client()
        if c is None:
            raise Refused("the hub is not connected to the band")
        self._engine = RolloutEngine(c, manifest, ro, self.save)
        self._rollout_task = asyncio.create_task(self._run_rollout(self._engine))

    async def _run_rollout(self, engine: RolloutEngine) -> None:
        ro = await engine.run()
        done = [t["name"] for t in ro["targets"] if t["status"] == "done"]
        if ro["status"] == "done" and ro.get("release_after"):
            try:
                self.feed.release(ro["build"], by=ro.get("by", "system"),
                                  note=f"rollout {ro['id']}")
            except LookupError:
                pass
        self.record({"kind": "workers", "event": "finished", "build": ro["build"],
                     "rollout": ro["id"], "result": ro["status"], "by": ro.get("by"),
                     "detail": (ro.get("reason") or "") + (f"; updated: {', '.join(done)}"
                                                           if done else "")})

    def cancel_rollout(self, actor: str) -> dict:
        ro = self.state.get("rollout")
        if not ro or ro.get("status") != "running" or self._engine is None:
            raise Refused("no rollout is running")
        self._engine.cancelled = True
        self.record({"kind": "workers", "event": "cancel", "rollout": ro["id"], "by": actor})
        return ro

    def release_build(self, build: int, actor: str) -> dict:
        try:
            out = self.feed.release(int(build), by=actor, note="released by the operator")
        except LookupError as e:
            raise Refused(str(e)) from None
        self.record({"kind": "workers", "event": "released", "build": int(build), "by": actor,
                     "detail": "every worker that checks in now moves to this build"})
        return out

    # -- apps ---------------------------------------------------------------------------
    async def publish_apk(self, tag: str, actor: str, notify: bool = False) -> dict:
        rel = self._find(tag)
        apk = await self.rook.fetch(rel, "rook-worker.apk")
        man_path = await self.rook.fetch(rel, "rook-worker-apk.json", 64 * 1024)
        man = json.loads(man_path.read_text(encoding="utf-8"))
        from .github import sha256_of
        if (not isinstance(man, dict) or man.get("package") != "systems.bake.rook"
                or man.get("sha256") != sha256_of(apk) or man.get("size") != apk.stat().st_size
                or type(man.get("version_code")) is not int):
            raise Refused("the release's APK and its manifest do not match; not published")
        dest = self.root / "apk"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(apk, dest / "rook-worker.apk.new")
        os.replace(dest / "rook-worker.apk.new", dest / "rook-worker.apk")
        hubself.json_dump(dest / "rook-worker-apk.json", man)
        self.record({"kind": "app", "event": "published", "tag": tag, "by": actor,
                     "detail": f"Rook app {man.get('version_name')} ({man.get('version_code')}) "
                               "is served at /apk"})
        out = {"ok": True, "version_name": man.get("version_name")}
        if notify:
            out["notified"] = await self.notify_apps(actor)
        return out

    async def notify_apps(self, actor: str, ids: list[str] | None = None) -> dict:
        """Tell Android apps an update is waiting (``device.update`` queues the
        app's own check; the app installs only with Android's consent)."""
        c = self._client()
        sent, skipped = [], []
        for m in self.members():
            if m["kind"] != "app" or m.get("platform") != "android":
                continue
            if ids and m["worker_id"] not in ids:
                continue
            if not m.get("can_notify") or not m.get("online") or m.get("state") != "behind":
                skipped.append(m["name"])
                continue
            try:
                r = await c.call(cap="device.update", args={}, target=m["worker_id"],
                                 timeout=15.0, identity=actor)
                (sent if r.get("ok") else skipped).append(m["name"])
            except Exception:
                skipped.append(m["name"])
        self.record({"kind": "app", "event": "notified", "by": actor,
                     "detail": f"asked {len(sent)} app(s) to update"
                               + (f"; not reached: {', '.join(skipped)}" if skipped else "")})
        return {"sent": sent, "skipped": skipped}

    # -- background ---------------------------------------------------------------------
    def sync_feed(self) -> None:
        """Keep the hub's current worker bundle in the feed, and start the
        feed at it the first time (so turning the service on changes nothing
        about what workers get today)."""
        paths = self._bundle_paths() if self._bundle_paths else None
        if paths:
            manifest, pyz = paths
            try:
                self.feed.snapshot(manifest, pyz, sign=self._signer)
            except Exception:
                log.exception("could not keep the worker bundle in the update feed")
        if not self.feed.released_build() and self.feed.newest_build():
            self.feed.release(self.feed.newest_build(), by="system",
                              note="the bundle this hub had when updates started")
        cur = self.state.get("rollout") or {}
        self.feed.prune(protect=(int(cur.get("build") or 0),))

    async def tick(self) -> None:
        self.sync_feed()
        self.check_hub_apply()
        ro = self.state.get("rollout")
        if ro and ro.get("status") == "running" and (self._rollout_task is None
                                                     or self._rollout_task.done()):
            # The hub restarted mid-rollout: say so instead of resuming blind.
            ro["status"], ro["reason"] = "stopped", "the hub restarted during the rollout"
            for t in ro["targets"]:
                if t["status"] in ("pending", "pushing", "waiting"):
                    t["status"], t["detail"] = "cancelled", "the hub restarted"
            self.save()
            self.record({"kind": "workers", "event": "finished", "result": "stopped",
                         "rollout": ro["id"], "build": ro.get("build"), "detail": ro["reason"]})
        if self.mode != "off":
            try:
                await self.check()
            except Exception:
                log.exception("release check failed")
        else:
            self.write_summary()
        if self.mode == "auto":
            await self.auto()

    async def auto(self, now: datetime.datetime | None = None) -> str:
        """Auto mode: inside the maintenance window, apply a newer non-breaking
        release to the hub, then roll the newer worker build out (canary
        first). Never a breaking release, never twice the same target."""
        if not in_window(str(self.s("window", "") or ""), now):
            return "outside the maintenance window"
        if self.state.get("hub_apply") or (self.state.get("rollout") or {}).get("status") == "running":
            return "busy"
        tried = self.state.setdefault("auto_tried", {})
        hub = hubself.current_hub()
        latest = self.latest()
        if latest and self._newer(latest, hub) and f"hub:{latest['tag']}" not in tried:
            a = self.analyse(latest)
            tried[f"hub:{latest['tag']}"] = time.time()
            self.save()
            if not a["known"] or a["breaking"]:
                self.record({"kind": "hub", "event": "auto-skipped", "tag": latest["tag"],
                             "detail": "breaking or without metadata: waits for the operator"})
                return "breaking: waiting for the operator"
            try:
                await self.apply_hub(latest["tag"], "auto")
                return "hub update started"
            except Refused as e:
                self.record({"kind": "hub", "event": "auto-skipped", "tag": latest["tag"],
                             "detail": str(e)})
                return str(e)
        newest, released = self.feed.newest_build(), self.feed.released_build()
        if newest > released and f"workers:{newest}" not in tried:
            tried[f"workers:{newest}"] = time.time()
            self.save()
            try:
                await self.start_rollout({"mode": "all"}, "auto", build=newest)
                return "worker rollout started"
            except Refused as e:
                self.record({"kind": "workers", "event": "auto-skipped", "build": newest,
                             "detail": str(e)})
                return str(e)
        return "nothing to do"

    async def stop(self) -> None:
        t = self._rollout_task
        if t is not None and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)


__all__ = ["MODES", "Refused", "UpdateService", "current_floors", "in_window", "ReleaseError"]
