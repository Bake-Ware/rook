"""The settings service: resolution, writes, history, worker delivery.

One object backs the ``settings.*`` caps on worker ``rook`` and the
dashboard's Settings area (through ``/settings/account-api`` on the MCP
port), so both show the same values and write the same history.

Precedence (docs/design/settings.md 3.3, as implemented here)::

    default < file (setup.json) < hub < band < worker < user < env / flag

with one change from the proposal: a stored value beats ``setup.json`` (the
file is a legacy source that a UI change replaces), and the environment beats
both. The band key is the exception: it lives in the enrollment database and
the environment only seeds the first band.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable

from ..core import settings as cs
from .settings_schema import Entry, Schema
from .settings_store import SCOPES, SettingsStore

log = logging.getLogger("rook.hub.settings")

#: Worker caps that mean "this worker understands typed settings": it can
#: resolve ``{{secret:…}}`` references at use and masks its config reads.
WORKER_SETTINGS_CAP = "worker.settings_report"


class SettingsError(ValueError):
    pass


def _env_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


class SettingsService:
    def __init__(self, store: SettingsStore, schema: Schema, *, vault: Any = None,
                 node: Any = None, enrollment: Any = None, process: str = "mcp",
                 environ: Any = None, setup_loader: Callable[[], dict] | None = None,
                 started_at: float | None = None) -> None:
        self.store = store
        self.schema = schema
        self.vault = vault
        self.node = node
        self.enrollment = enrollment
        self.process = process
        self.environ = environ if environ is not None else os.environ
        self._setup_loader = setup_loader
        self.started_at = started_at or time.time()
        self._jobs: dict[str, dict] = {}
        self._on_change: list[Callable[[Entry], None]] = []
        #: ``{key: (flag, value)}``: settings this process got on its command
        #: line; they lock the key like an environment variable.
        self.flags: dict[str, tuple] = {}

    # -- sources -----------------------------------------------------------
    def _setup(self) -> dict:
        if self._setup_loader is None:
            from ..remote import setup_store
            self._setup_loader = setup_store.load
        try:
            return self._setup_loader() or {}
        except Exception:
            return {}

    def _env_for(self, e: Entry) -> tuple | None:
        """``(variable, value)`` when the owning process's environment sets
        the key. The MCP reads its own environment; other hub processes
        report theirs at start (the store's runtime table)."""
        if e.owner == self.process or e.origin == "hub-plugin":
            if e.key in self.flags and self.flags[e.key][1] not in (None, ""):
                return self.flags[e.key]
            for var in e.env_names():
                if self.environ.get(var) is not None:
                    return var, self.environ[var]
            return None
        if e.owner in ("dashboard", "watchdog") or e.owner.startswith("service:"):
            rep = self.store.runtime(e.owner)
            got = (rep.get("env") or {}).get(e.key)
            if got:
                return got.get("env"), got.get("value")
        return None

    def _file_for(self, e: Entry) -> Any:
        if not e.file_key:
            return None
        v = self._setup().get(e.file_key)
        return v or None

    def _row_value(self, row: dict | None) -> Any:
        if row is None:
            return None
        if row.get("secret_ref"):
            return cs.make_ref(row["secret_ref"])
        return row.get("value")

    def _layers(self, e: Entry, ctx: dict) -> list[tuple]:
        out = []
        for scope in SCOPES:
            if not self.schema.applies_at(e, scope):
                continue
            target = "" if scope == "hub" else ctx.get(scope)
            if target is None:
                continue
            row = self.store.get(e.key, scope, target)
            if row is not None:
                out.append((scope, self._row_value(row),
                            {"target": target, "rev": row["rev"], "updated_by": row["updated_by"],
                             "updated_at": row["updated_at"]}))
        return out

    def _started(self, owner: str) -> float | None:
        if owner == self.process:
            return self.started_at
        rep = self.store.runtime(owner)
        return rep.get("started_at")

    # -- resolution --------------------------------------------------------
    def resolve(self, key: str, ctx: dict | None = None) -> dict:
        e = self.schema.get(key)
        if e is None:
            raise SettingsError(f"unknown setting {key!r}")
        return self._resolve(e, ctx or {})

    def _resolve(self, e: Entry, ctx: dict) -> dict:
        ctx = ctx or {}
        if e.managed == "enrollment":
            return self._resolve_band_key(e, ctx)
        env = self._env_for(e) if e.owner != "worker" else None
        file = self._file_for(e)
        r = cs.resolve(e.setting, e.key, self._layers(e, ctx), env=env, file=file)
        r["apply"] = e.setting.apply
        # A value changed after its owner process started, and read only at
        # start, is saved but not active yet.
        if e.setting.apply == "restart" and r["source"] in SCOPES and e.owner != "worker":
            started = self._started(e.owner)
            top = r["layers"][-1] if r["layers"] else {}
            if started and top.get("updated_at", 0) > started:
                r["pending"] = "restart"
        # The environment hides a stored or file value: say so (P1).
        if r["locked"]:
            hidden = [l for l in r["layers"] if l["source"] in SCOPES + ("file",)]
            if hidden:
                src = hidden[-1]["source"]
                what = {"file": "setup.json value", "hub": "Settings page value"}.get(
                    src, f"{src} value on the Settings page")
                var = r.get("env") or "?"
                where = (f"the {e.owner} command line" if var.startswith("-")
                         else f"the {e.owner} environment")
                r["conflict"] = {
                    "env": var, "hidden": src,
                    "note": f"{var} is set on {where} and wins over the {what}; "
                            f"remove it there to use the stored value."}
        return r

    def _resolve_band_key(self, e: Entry, ctx: dict) -> dict:
        out = {"key": e.key, "value": None, "source": "default", "locked": False,
               "layers": [], "invalid": [], "apply": e.setting.apply, "managed": "enrollment"}
        band_id = ctx.get("band")
        band = None
        if self.enrollment is not None and band_id:
            try:
                band = next((b for b in self.enrollment.bands(secrets_visible=True)
                             if b["id"] == band_id), None)
            except Exception:
                band = None
        if band is not None:
            out.update(value=cs.MASK if band.get("active") else None, source="enrollment",
                       fingerprint=cs.fingerprint(band.get("psk")),
                       epoch=band.get("epoch"), active=bool(band.get("active")))
        env = self._env_for(e)
        if env:
            out["env"] = env[0]
            env_fp = cs.fingerprint(env[1]) if env[1] not in (None, cs.MASK) else None
            if band is not None and env_fp and env_fp != out.get("fingerprint"):
                out["conflict"] = {"env": env[0], "note": (
                    f"{env[0]} holds a different key; it only seeds the first band and is "
                    "ignored now. Remove it from the environment.")}
        return out

    # -- pages -------------------------------------------------------------
    def _rows(self, entries: list[Entry], ctx: dict, *, scope: str) -> list[dict]:
        rows = []
        for e in entries:
            try:
                r = self._resolve(e, ctx)
            except Exception as err:  # never let one key break a page
                r = {"key": e.key, "error": str(err)}
            r["schema"] = e.describe()
            r["editable"] = not (e.setting.bootstrap or e.managed) and (
                scope == e.setting.scope or scope in e.setting.overridable)
            r["scope_here"] = scope
            here = [l for l in r.get("layers", []) if l.get("source") == scope]
            r["set_here"] = bool(here)
            rows.append(r)
        return rows

    GROUP_ORDER = ("General", "Identity", "Access", "Network", "Sign-in", "Updates", "Features",
                   "Recognition", "Speech", "Agent", "Search", "Timing", "Data", "Storage",
                   "Plugins", "Keys & access", "Worker defaults", "Probe", "Thresholds", "Alerts")

    @classmethod
    def _group(cls, rows: list[dict]) -> list[dict]:
        groups: dict[str, list] = {}
        for r in sorted(rows, key=lambda r: r["schema"].get("order", 0)):  # stable: declaration order
            groups.setdefault(r["schema"].get("group") or "General", []).append(r)
        rank = {n: i for i, n in enumerate(cls.GROUP_ORDER)}
        return [{"name": n, "rows": groups[n]}
                for n in sorted(groups, key=lambda n: (rank.get(n, len(rank)), n))]

    def bands(self) -> list[dict]:
        if self.enrollment is None:
            return []
        try:
            return [{"id": b["id"], "name": b["name"], "label": b["label"],
                     "primary": bool(b.get("is_primary")), "active": bool(b.get("active")),
                     "epoch": b.get("epoch")} for b in self.enrollment.bands()]
        except Exception:
            log.exception("listing bands failed")
            return []

    def _band_for_label(self, label: str) -> str | None:
        """Enrollment band id for a roster band label (derived band id)."""
        if self.enrollment is None or not label or label == "*":
            return None
        try:
            from telesthete.protocol.crypto import derive_band_id
            for b in self.enrollment.bands(active_only=True, secrets_visible=True):
                if derive_band_id(b["psk"]).hex()[:8] == label:
                    return b["id"]
        except Exception:
            return None
        return None

    def workers(self) -> list[dict]:
        client = getattr(self.node, "client", None)
        if client is None:
            return []
        hub_id = getattr(self.node, "worker_id", None)
        out = []
        try:
            roster = client.workers
        except Exception:
            return []
        for wid, w in roster.items():
            if wid == hub_id:
                continue
            out.append({"worker_id": wid, "name": w.get("name") or wid[:8],
                        "band": w.get("band"), "version": w.get("version"),
                        "build": w.get("build"), "caps": len(w.get("caps", [])),
                        "typed_settings": WORKER_SETTINGS_CAP in w.get("caps", []),
                        "last_seen": w.get("last_seen")})
        return sorted(out, key=lambda w: w["name"].lower())

    def _worker_entry(self, name: str) -> tuple[str | None, dict | None]:
        client = getattr(self.node, "client", None)
        if client is None:
            return None, None
        hub_id = getattr(self.node, "worker_id", None)
        for wid, w in client.workers.items():
            if wid != hub_id and ((w.get("name") or "") == name or wid == name):
                return wid, w
        return None, None

    def conflicts(self) -> list[dict]:
        out = []
        for e in self.schema:
            if e.setting.scope not in ("hub",) and not e.managed:
                continue
            ctxs = [{}]
            if e.managed:
                ctxs = [{"band": b["id"]} for b in self.bands() if b["active"]]
            for ctx in ctxs:
                try:
                    r = self._resolve(e, ctx)
                except Exception:
                    continue
                if r.get("conflict"):
                    out.append({"key": e.key, "label": e.setting.label or e.key,
                                "owner": e.owner, **ctx, **r["conflict"]})
        seen = {(c.get("key"), c.get("owner")) for c in out}
        for proc, rep in self.store.runtime().items():
            for c in rep.get("conflicts") or []:
                if (c.get("key"), proc) in seen:
                    continue
                seen.add((c.get("key"), proc))
                e = self.schema.get(c.get("key") or "")
                out.append({"owner": proc, "label": e.setting.label if e else c.get("key"), **c})
        return out

    def relay_status(self) -> dict:
        """The relay is displayed and checked, not managed: the address each
        hub process dials and whether the MCP's band client sees peers."""
        out: dict = {"mcp": self._resolve(self.schema.get("core.mcp.relay"), {}).get("value")}
        dash = self.store.runtime("dashboard").get("env") or {}
        host = (dash.get("core.dashboard.relay_host") or {}).get("value")
        port = (dash.get("core.dashboard.relay_port") or {}).get("value")
        if host or port:
            out["dashboard"] = f"{host or '127.0.0.1'}:{port or 7474}"
        client = getattr(self.node, "client", None)
        if client is not None:
            try:
                hub_id = getattr(self.node, "worker_id", None)
                peers = [w for wid, w in client.workers.items() if wid != hub_id]
                out["peers"] = len(peers)
                out["reachable"] = bool(peers)
            except Exception:
                out["reachable"] = None
        if out.get("dashboard") and out.get("mcp") and out["dashboard"] != out["mcp"]:
            out["note"] = ("The dashboard and the MCP dial different relay addresses; fine when "
                           "one is a container name, a problem otherwise.")
        return out

    def overview(self) -> dict:
        plugins = self.schema.plugins()
        return {
            "relay": self.relay_status(),
            "bands": self.bands(),
            "workers": self.workers(),
            "plugins": [{"namespace": ns, "title": p["title"], "origin": p["origin"],
                         "owner": p["owner"], "count": len(p["keys"])}
                        for ns, p in sorted(plugins.items(), key=lambda kv: kv[1]["title"].lower())],
            "conflicts": self.conflicts(),
            "runtime": {k: {kk: vv for kk, vv in v.items() if kk not in ("env",)}
                        for k, v in self.store.runtime().items()},
            "history": self.store.history(limit=15),
        }

    def hub_page(self) -> dict:
        entries = [e for e in self.schema if e.origin == "core" and e.setting.scope == "hub"]
        rows = self._rows(entries, {}, scope="hub")
        return {"view": "hub", "title": "Hub", "groups": self._group(rows),
                "history": self.store.history(key="core.", scope="hub", limit=30)}

    def band_page(self, band_id: str) -> dict:
        band = next((b for b in self.bands() if b["id"] == band_id), None)
        if band is None:
            raise SettingsError(f"no band {band_id!r}")
        entries = [e for e in self.schema if e.origin == "core"
                   and self.schema.applies_at(e, "band")]
        rows = self._rows(entries, {"band": band_id}, scope="band")
        return {"view": "band", "title": band["name"], "band": band,
                "groups": self._group(rows),
                "history": self.store.history(scope="band", target=band_id, limit=30)}

    async def worker_page(self, name: str) -> dict:
        wid, w = self._worker_entry(name)
        wname = (w or {}).get("name") or name
        band_id = self._band_for_label((w or {}).get("band", ""))
        ctx = {"band": band_id, "worker": wname}
        entries = [e for e in self.schema if e.owner == "worker"
                   and self.schema.applies_at(e, "worker")]
        rows = self._rows(entries, ctx, scope="worker")
        live: dict = {"online": w is not None}
        if w is not None:
            caps = w.get("caps", [])
            live.update(worker_id=wid, version=w.get("version"), build=w.get("build"),
                        band=w.get("band"), typed_settings=WORKER_SETTINGS_CAP in caps)
            for cap, key in (("worker.plugin.list", "plugins"), ("worker.config_get", "config"),
                             (WORKER_SETTINGS_CAP, "report")):
                if cap not in caps:
                    continue
                try:
                    reply = await self.node.client.call(cap=cap, args={}, target=wid, timeout=8.0,
                                                        identity="system:rook-settings")
                    res = reply.get("result") if reply.get("ok", True) else None
                    if key == "config" and isinstance(res, dict):
                        res = {**res, "config": cs.mask_worker_config(
                            res.get("config"), self.schema.worker_env_names())}
                    live[key] = res
                except Exception as err:
                    live[key + "_error"] = str(err) or type(err).__name__
        plugin_rows: dict[str, list] = {}
        for r in rows:
            ns = r["schema"]["namespace"]
            if r["schema"]["origin"] == "worker-plugin":
                plugin_rows.setdefault(r["schema"].get("plugin") or ns, []).append(r)
        core_rows = [r for r in rows if r["schema"]["origin"] == "core"]
        return {"view": "worker", "title": wname, "worker": wname, "band_id": band_id,
                "live": live, "groups": self._group(core_rows), "plugin_settings": plugin_rows,
                "delivered": self.store.runtime(f"worker:{wname}") or None,
                "job": self._latest_job(wname),
                "history": self.store.history(scope="worker", target=wname, limit=30)}

    def plugin_page(self, namespace: str) -> dict:
        page = self.schema.plugins().get(namespace)
        if page is None:
            raise SettingsError(f"no plugin settings for {namespace!r}")
        entries = [self.schema.get(k) for k in page["keys"]]
        hub_entries = [e for e in entries if e.setting.scope == "hub"]
        rows = self._rows(hub_entries, {}, scope="hub")
        other = []
        for e in entries:
            if e.setting.scope == "hub" and not e.setting.overridable:
                continue
            counts = {s: len(self.store.rows(s, prefix=e.key)) for s in SCOPES
                      if s != "hub" and self.schema.applies_at(e, s)}
            other.append({"key": e.key, "label": e.setting.label or e.key,
                          "scope": e.setting.scope, "overrides": counts, "schema": e.describe()})
        readers = self.resolve("core.settings.service_readers").get("value") or {}
        return {"view": "plugin", "title": page["title"], "namespace": namespace,
                "origin": page["origin"], "owner": page["owner"], "groups": self._group(rows),
                "scoped": other, "service_readers": readers.get(namespace, []),
                "history": self.store.history(key=namespace + ".", limit=30)}

    def user_page(self, user_id: str, label: str = "") -> dict:
        entries = [e for e in self.schema if self.schema.applies_at(e, "user")]
        rows = self._rows(entries, {"user": user_id}, scope="user")
        return {"view": "user", "title": label or "My preferences", "user": user_id,
                "groups": self._group(rows),
                "history": self.store.history(scope="user", target=user_id, limit=30)}

    def search(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        if not q:
            return []
        out = []
        for e in self.schema:
            s = e.setting
            hay = " ".join([e.key, s.label, s.help, s.group, *e.env_names()]).lower()
            if q in hay:
                out.append({"key": e.key, "label": s.label or e.key, "scope": s.scope,
                            "namespace": e.namespace, "origin": e.origin,
                            "env": list(e.env_names())})
        return out[:50]

    # -- writes ------------------------------------------------------------
    def _target(self, e: Entry, scope: str, target: str | None) -> str:
        if scope not in SCOPES:
            raise SettingsError(f"scope must be one of {SCOPES}")
        if not self.schema.applies_at(e, scope):
            allowed = (e.setting.scope,) + tuple(e.setting.overridable)
            raise SettingsError(f"{e.key} can be set at {', '.join(allowed)} scope, not {scope}")
        target = (target or "").strip()
        if scope == "hub":
            return ""
        if not target:
            raise SettingsError(f"{scope} scope needs a target")
        if scope == "band" and self.enrollment is not None:
            if not any(b["id"] == target for b in self.bands()):
                raise SettingsError(f"no band {target!r}")
        return target

    def _vault_name(self, e: Entry, scope: str, target: str) -> str:
        if scope == "hub":
            return cs.vault_name("plugin", e.namespace, e.setting.name)
        return cs.vault_name(scope, target, e.key)

    def _check_writable(self, e: Entry | None, key: str) -> Entry:
        if e is None:
            raise SettingsError(f"unknown setting {key!r}; settings.describe lists them")
        if e.managed == "enrollment":
            raise SettingsError(f"{key} is managed on the Bands page (rotate / revoke)")
        if e.setting.bootstrap:
            names = ", ".join(e.env_names()) or e.setting.flag or "its flag"
            raise SettingsError(f"{key} is read before the settings store is reachable; set "
                                f"{names} in the {e.owner} environment")
        return e

    def set(self, key: str, value: Any, *, scope: str | None = None, target: str | None = None,
            actor: str = "system", note: str = "", source: str = "api",
            dry_run: bool = False, expect_rev: int | None = None) -> dict:
        e = self._check_writable(self.schema.get(key), key)
        scope = scope or e.setting.scope
        target = self._target(e, scope, target)
        ctx = self._ctx(scope, target)
        before = self._resolve(e, ctx)
        if e.setting.secret:
            if not isinstance(value, str) or not value:
                raise SettingsError(f"{key} is a secret: send the new value as a non-empty string")
            if cs.secret_ref(value):
                ref, new_value = cs.secret_ref(value), None
            else:
                ref, new_value = self._vault_name(e, scope, target), value
            shown_new = "fp:" + cs.fingerprint(new_value) if new_value else cs.make_ref(ref)
            if dry_run:
                return {"ok": True, "dry_run": True, "key": key, "scope": scope, "target": target,
                        "before": before, "apply": e.setting.apply}
            if new_value is not None:
                if self.vault is None:
                    raise SettingsError("the vault is unavailable on this hub")
                self.vault.set(ref, new_value, f"setting {key} ({scope}{':' + target if target else ''})",
                               actor)
            old = before.get("fingerprint")
            res = self.store.set(key, scope, target, secret_ref=ref, actor=actor, note=note,
                                 source=source, old_shown=("fp:" + old) if old else None,
                                 new_shown=shown_new, expect_rev=expect_rev)
        else:
            try:
                coerced = e.setting.coerce(value)
            except (ValueError, TypeError) as err:
                raise SettingsError(str(err)) from None
            if dry_run:
                after = cs.resolve(e.setting, key, self._layers(e, ctx) + [(scope, coerced)],
                                   env=self._env_for(e) if e.owner != "worker" else None)
                return {"ok": True, "dry_run": True, "key": key, "scope": scope, "target": target,
                        "before": before, "after": after, "apply": e.setting.apply}
            res = self.store.set(key, scope, target, value=coerced, actor=actor, note=note,
                                 source=source, expect_rev=expect_rev)
        self._changed(e)
        after = self._resolve(e, ctx)
        return {"ok": True, **res, "apply": e.setting.apply, "effective": after,
                "deliver": e.owner == "worker",
                "note": self._apply_note(e, after)}

    def reset(self, key: str, *, scope: str | None = None, target: str | None = None,
              actor: str = "system", note: str = "", source: str = "api") -> dict:
        e = self._check_writable(self.schema.get(key), key)
        scope = scope or e.setting.scope
        target = self._target(e, scope, target)
        row = self.store.get(key, scope, target)
        if row is None:
            return {"ok": True, "key": key, "scope": scope, "target": target, "removed": False}
        old = None
        if row.get("secret_ref") and self.vault is not None:
            try:
                old = "fp:" + cs.fingerprint(self.vault.get(row["secret_ref"], actor, via="reset"))
            except KeyError:
                old = None
            if row["secret_ref"] == self._vault_name(e, scope, target):
                try:
                    self.vault.delete(row["secret_ref"], actor)
                except Exception:
                    log.exception("deleting vault entry for %s failed", key)
        self.store.delete(key, scope, target, actor=actor, note=note, source=source, old_shown=old)
        self._changed(e)
        after = self._resolve(e, self._ctx(scope, target))
        return {"ok": True, "key": key, "scope": scope, "target": target, "removed": True,
                "effective": after, "deliver": e.owner == "worker",
                "note": self._apply_note(e, after)}

    def _ctx(self, scope: str, target: str) -> dict:
        if scope == "hub":
            return {}
        ctx = {scope: target}
        if scope == "worker":
            _, w = self._worker_entry(target)
            if w is not None:
                band = self._band_for_label(w.get("band", ""))
                if band:
                    ctx["band"] = band
        return ctx

    def _apply_note(self, e: Entry, eff: dict) -> str:
        if eff.get("locked"):
            return f"saved, but {eff.get('env')} in the environment still wins"
        if e.owner == "worker":
            return "saved; apply it on the worker page (commit-confirmed restart)"
        return {"live": "active now", "reload": "active when the plugin reloads",
                "restart": f"active after the {e.owner} process restarts",
                "risky": "active after a commit-confirmed restart"}.get(e.setting.apply, "")

    def on_change(self, fn: Callable[[Entry], None]) -> None:
        self._on_change.append(fn)

    def _changed(self, e: Entry) -> None:
        for fn in self._on_change:
            try:
                fn(e)
            except Exception:
                log.exception("settings change hook failed for %s", e.key)

    # -- services (scoped-token read) -------------------------------------
    def readers_for(self, namespace: str) -> list[str]:
        readers = self.resolve("core.settings.service_readers").get("value") or {}
        got = readers.get(namespace) if isinstance(readers, dict) else None
        return [str(x) for x in got] if isinstance(got, list) else []

    def fetch(self, namespace: str, principal: dict | None) -> dict:
        """Everything a service needs to run ``namespace``: effective hub
        values (secrets included) and per-user overrides. Only for a token
        listed in ``core.settings.service_readers[namespace]``."""
        page = self.schema.plugins().get(namespace)
        if page is None:
            raise SettingsError(f"no settings for {namespace!r}")
        principal = principal or {}
        allowed = self.readers_for(namespace)
        who = {principal.get("agent_id"), principal.get("label")} - {None, ""}
        if principal.get("kind") != "agent" or not principal.get("verified", True) \
                or not (who & set(allowed)):
            raise PermissionError(
                f"settings.fetch {namespace}: this token is not a reader for {namespace} "
                f"(add its label or agent_id to core.settings.service_readers.{namespace})")
        values, users = {}, {}
        for key in page["keys"]:
            e = self.schema.get(key)
            s = e.setting
            if s.scope == "hub":
                row = self.store.get(key, "hub", "")
                raw = self._row_value(row)
                if s.secret:
                    ref = cs.secret_ref(raw) if raw else None
                    val = None
                    if ref and self.vault is not None:
                        try:
                            val = self.vault.get(ref, f"agent:{principal.get('label') or '?'}",
                                                 via=f"settings.fetch {namespace}")
                        except KeyError:
                            val = None
                    values[s.name] = val
                else:
                    values[s.name] = s.coerce(raw) if raw is not None else (
                        s.default() if callable(s.default) else s.default)
            if self.schema.applies_at(e, "user"):
                for row in self.store.rows("user", prefix=key):
                    if row["key"] == key and not row["secret_ref"]:
                        users.setdefault(row["target"], {})[s.name] = row["value"]
        env_names = {self.schema.get(k).setting.name: list(self.schema.get(k).env_names())
                     for k in page["keys"]}
        return {"namespace": namespace, "values": values, "users": users,
                "env": env_names, "note": "The service's own environment still wins over these."}

    def report_service(self, namespace: str, principal: dict | None, env: dict) -> dict:
        """A service tells the hub which of its keys its environment locks
        (names only; values of secrets are never sent)."""
        self.fetch(namespace, principal)  # same gate
        page = self.schema.plugins()[namespace]
        locked = {}
        for key in page["keys"]:
            e = self.schema.get(key)
            var = (env or {}).get(e.setting.name)
            if var:
                locked[key] = {"env": str(var)[:64], "value": cs.MASK if e.setting.secret else None}
        self.store.report_runtime(f"service:{namespace}", {"env": locked, "started_at": time.time()})
        return {"ok": True, "locked": sorted(locked)}

    # -- worker delivery ---------------------------------------------------
    def delivery(self, worker: str, typed: bool) -> tuple[dict, list[str]]:
        """The config push for ``worker``: ``{name?, announce_interval?, log_level?,
        env: {...}}`` from store values (defaults are not pushed), with keys
        removed since the last delivery sent as ``None``. Secrets go as
        ``{{secret:…}}`` references, which only typed-settings workers resolve."""
        _, w = self._worker_entry(worker)
        ctx = {"worker": worker, "band": self._band_for_label((w or {}).get("band", ""))}
        settings: dict = {}
        env: dict = {}
        problems: list[str] = []
        for e in self.schema:
            if e.owner != "worker" or not self.schema.applies_at(e, "worker"):
                continue
            layers = self._layers(e, ctx)
            if not layers:
                continue
            r = cs.resolve(e.setting, e.key, layers)
            if r["source"] not in SCOPES:
                continue
            value = layers[-1][1] if e.setting.secret else r["value"]
            if e.config_key:
                settings[e.config_key] = value
                continue
            var = e.setting.env
            if not var:
                continue
            if e.setting.secret:
                if not typed:
                    problems.append(f"{e.key}: this worker's build cannot fetch secrets at use; "
                                    "update it first (the value is never written to its disk)")
                    continue
                env[var] = value  # the {{secret:…}} reference
            else:
                env[var] = _env_text(value)
        last = self.store.runtime(f"worker:{worker}") or {}
        for var in (last.get("env_keys") or []):
            if var not in env:
                env[var] = None
        for k in (last.get("config_keys") or []):
            if k not in settings and k in ("announce_interval", "log_level", "name"):
                settings[k] = None   # a null config key is ignored at boot: back to the flag
        if env:
            settings["env"] = env
        return settings, problems

    def _latest_job(self, worker: str) -> dict | None:
        jobs = [j for j in self._jobs.values() if j["worker"] == worker]
        return max(jobs, key=lambda j: j["started"]) if jobs else None

    async def apply_worker(self, worker: str, actor: str, *, confirm_within: float = 120.0,
                           wait: bool = False) -> dict:
        wid, w = self._worker_entry(worker)
        if w is None:
            raise SettingsError(f"worker {worker!r} is not on the band")
        wname = w.get("name") or worker
        typed = WORKER_SETTINGS_CAP in w.get("caps", [])
        settings, problems = self.delivery(wname, typed)
        if problems:
            raise SettingsError("; ".join(problems))
        if not settings:
            return {"ok": True, "worker": wname, "note": "nothing stored for this worker to push"}
        from .worker_config import apply_confirmed
        job = {"id": uuid.uuid4().hex[:12], "worker": wname, "state": "running",
               "started": time.time(), "actor": actor,
               "pushed": cs.mask_worker_config(settings, self.schema.worker_env_names())}
        self._jobs[job["id"]] = job

        async def run() -> dict:
            try:
                res = await apply_confirmed(self.node.client, wid, settings, identity=actor,
                                            confirm_within=confirm_within,
                                            public_env=self.schema.worker_env_names())
            except Exception as err:
                res = {"ok": False, "error": f"{type(err).__name__}: {err}"}
            job.update(state="done" if res.get("ok") else "failed", result=res,
                       finished=time.time())
            if res.get("ok"):
                env = settings.get("env") or {}
                self.store.report_runtime(f"worker:{wname}", {
                    "env_keys": sorted(k for k, v in env.items() if v is not None),
                    "config_keys": sorted(k for k, v in settings.items()
                                          if k != "env" and v is not None),
                    "epoch": res.get("epoch"), "by": actor, "pushed": job["pushed"]})
            self.store.record("core.worker.config", "worker", wname, old=None,
                              new=job["pushed"], actor=actor,
                              note=("applied" if res.get("ok") else "apply failed: "
                                    + str(res.get("error") or res.get("stage"))),
                              source="apply")
            return res

        if wait:
            await run()
            return {"ok": job["state"] == "done", "job": job}
        job["task"] = asyncio.ensure_future(run())
        return {"ok": True, "job": {k: v for k, v in job.items() if k != "task"}}

    async def plugin_toggle(self, worker: str, module: str, enable: bool, actor: str) -> dict:
        wid, w = self._worker_entry(worker)
        if w is None:
            raise SettingsError(f"worker {worker!r} is not on the band")
        cap = "worker.plugin.enable" if enable else "worker.plugin.disable"
        reply = await self.node.client.call(cap=cap, args={"module": module}, target=wid,
                                            timeout=20.0, identity=actor)
        res = reply.get("result", reply)
        ok = bool(reply.get("ok", True)) and bool((res or {}).get("ok"))
        self.store.record(f"core.worker.plugins.{module}.enabled", "worker",
                          w.get("name") or worker, old=not enable, new=enable, actor=actor,
                          note="" if ok else f"failed: {(res or {}).get('error')}", source="ui")
        return {"ok": ok, "result": res}

    def worker_secrets(self, worker_id: str, names: list[str]) -> dict:
        """Secrets a worker may fetch at use: only vault entries referenced by
        a stored setting that applies to that worker (its own rows, or its
        band's). Any band member can impersonate a worker id, so this exposes
        what shell access to that worker exposes: its own secrets, no others."""
        client = getattr(self.node, "client", None)
        w = client.workers.get(worker_id) if client is not None else None
        if w is None:
            raise SettingsError("unknown worker")
        name = w.get("name") or worker_id
        band_id = self._band_for_label(w.get("band", ""))
        allowed: set[str] = set()
        for e in self.schema:
            if e.owner != "worker" or not e.setting.secret:
                continue
            for scope, target in (("worker", name), ("band", band_id)):
                if not target:
                    continue
                row = self.store.get(e.key, scope, target)
                if row and row.get("secret_ref"):
                    allowed.add(row["secret_ref"])
        out, missing = {}, []
        for n in names or []:
            n = str(n)
            if n not in allowed or self.vault is None:
                missing.append(n)
                continue
            try:
                out[n] = self.vault.get(n, f"worker:{name}", via="worker secret at use")
            except KeyError:
                missing.append(n)
        return {"secrets": out, "missing": missing}

    # -- runtime -----------------------------------------------------------
    def env_report(self, owner: str, environ: Any = None) -> dict:
        """What ``owner``'s environment (and command line) sets, per key
        (secrets masked)."""
        environ = environ if environ is not None else self.environ
        out = {}
        if owner == self.process:
            for key, (flag, raw) in self.flags.items():
                e = self.schema.get(key)
                if e is None or raw in (None, ""):
                    continue
                out[key] = {"env": flag, "value": (cs.MASK + ":" + cs.fingerprint(raw))
                            if e.setting.secret else raw}
        for e in self.schema:
            if e.owner != owner:
                continue
            if e.key in out:
                continue
            for var in e.env_names():
                if environ.get(var) is not None:
                    raw = environ[var]
                    out[e.key] = {"env": var,
                                  "value": (cs.MASK + ":" + cs.fingerprint(raw)) if e.setting.secret
                                  else raw}
                    break
        return out
