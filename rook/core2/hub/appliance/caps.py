"""The ``appliance.*`` hub caps over :class:`~rook.core2.hub.appliance.Appliances`.

Bake (castling-workers-appliances): "Onboarding sets the route: the worker
that pairs with and test-calls the appliance has proven reach; hub records
address, reach scope and that worker as known-good." castling-manifest-appliance:
"Each instance gets its own name and is called like a worker; the delegate
worker and route are recorded at onboarding (hub state)."
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from rook.kit.ctx import KitError
from rook.kit.manifest import compile_fields
from rook.kit.manifest.types import setting_spec

from ..vault import LONE_REF, NAME as VAULT_NAME, Locked, VaultError
from . import NAME, secret_decl

MANIFEST = Path(__file__).parent


def install(hub) -> None:
    reg = hub.appliances

    def owner(ctx) -> None:
        if not ctx.caller.owner:
            raise KitError("denied: only an owner changes the band's appliances")

    def worker_of(target: str):
        w = hub.dispatcher.resolve(target)
        if w is None or w.id == hub.node:
            raise KitError(f"unknown worker: {target}")
        return w

    def decls_of(w, plugin: str) -> dict:
        m = hub.worker_manifest(w.id, plugin)
        if m is None:
            raise KitError(f"{w.name} doesn't run {plugin}")
        app = m.get("appliance")
        if not isinstance(app, dict):
            raise KitError(f"{plugin} has no appliance section: its caps run on the worker itself")
        return app

    def instance_settings(plugin: str, decls: dict, given: dict | None) -> dict:
        out = {}
        for key, value in (given or {}).items():
            decl = decls.get(key)
            if decl is None:
                raise KitError(f"{plugin} has no appliance setting {key}")
            if secret_decl(decl):
                ref = LONE_REF.fullmatch(value) if isinstance(value, str) else None
                name = ref[1] if ref else value
                if not (isinstance(name, str) and VAULT_NAME.fullmatch(name)):
                    raise KitError(f"setting {key} is a secret: give a vault name ({{{{secret:NAME}}}}), not a value")
                out[key] = name
                continue
            spec = {k: v for k, v in decl.items() if k not in ("doc", "setup")}
            errors = list(Draft202012Validator(compile_fields({key: setting_spec(spec)})).iter_errors({key: value}))
            if errors:
                raise KitError(f"setting {key}: {errors[0].message}")
            out[key] = value
        return out

    def view(rec: dict) -> dict:
        out = {k: v for k, v in rec.items() if k != "delegates"}
        out["delegates"] = []
        for wid in rec["delegates"]:
            k = hub.known.get(wid)
            out["delegates"].append({"id": wid, "name": k.name if k else None, "online": bool(k and k.online)})
        return out

    async def add(ctx, name: str, plugin: str, worker: str, host: str | None = None,
                  port: int | None = None, settings: dict | None = None, pair: dict | None = None) -> dict:
        owner(ctx)
        if not NAME.fullmatch(name) or name == "rook":
            raise KitError(f"bad name: {name}")
        w = worker_of(worker)
        decls = decls_of(w, plugin)
        old = reg.get(name)
        if old is not None:
            # Another delegate for one already onboarded: it is tried when the
            # known-good one is offline, and becomes known-good once it carries a call.
            if old["plugin"] != plugin:
                raise KitError(f"{name} is a {old['plugin']} appliance already")
            if host is not None or port is not None or settings or pair:
                raise KitError(f"{name} is added already: appliance.remove it to onboard it again")
            if w.id not in old["delegates"]:
                old["delegates"].append(w.id)
                reg.put(old)
            return {"appliance": view(old), "added": False}
        if hub.dispatcher.resolve(name) is not None:
            raise KitError(f"{name} is a worker's name")
        rec = {"name": name, "plugin": plugin, "delegates": [w.id],
               "settings": instance_settings(plugin, decls.get("settings") or {}, settings),
               "added": time.time(), "added_by": ctx.caller.person or ctx.caller.agent or ""}
        if host is not None:
            rec["host"] = host
        if port is not None:
            rec["port"] = port
        paired = None
        cap = decls.get("pair")
        if cap:
            # Pairing is the proof this worker reaches it: the route is kept
            # only when it works. The call goes through dispatch like any other,
            # as the caller, addressed to the new instance.
            reg.put(rec)
            try:
                reply = await hub.call_as(replace(ctx.caller, plugin=None, why=f"pair {name} at onboarding"),
                                          cap, dict(pair or {}), target=name)
            except BaseException:
                reg.remove(name)
                raise
            if not reply.get("ok"):
                reg.remove(name)
                raise KitError(f"pairing {name} failed: {reply.get('error', 'failed')}")
            paired = reply.get("result")
            got = (paired.get("settings") if isinstance(paired, dict) else None) or {}
            for key, value in got.items() if isinstance(got, dict) else ():
                decl = (decls.get("settings") or {}).get(key)
                if decl is None or value in (None, ""):
                    continue
                if secret_decl(decl):
                    vname = f"appliance.{name}.{key}"
                    if not VAULT_NAME.fullmatch(vname):
                        reg.remove(name)
                        raise KitError(f"{vname} is too long for a vault name")
                    try:
                        hub.vault.set(vname, str(value), by=rec["added_by"],
                                      description=f"{plugin} {key} of appliance {name}, from {cap}")
                    except (VaultError, Locked) as e:
                        reg.remove(name)
                        raise KitError(f"pairing {name}: the vault refused {key}: {e}") from None
                    rec["settings"][key] = vname
                else:
                    rec["settings"].update(instance_settings(plugin, decls.get("settings") or {}, {key: value}))
            reg.put(rec)
            paired = {k: v for k, v in paired.items() if k != "settings"} if isinstance(paired, dict) else paired
        reg.put(rec)
        out = {"appliance": view(rec), "added": True}
        if cap:
            out["paired"] = paired
        return out

    def list_(ctx, name: str | None = None) -> dict:
        recs = reg.list() if name is None else [r for r in [reg.get(name)] if r]
        return {"appliances": [view(r) for r in recs]}

    def remove(ctx, name: str, worker: str | None = None) -> dict:
        owner(ctx)
        rec = reg.get(name)
        if rec is None:
            raise KitError(f"unknown appliance: {name}")
        if worker is None:
            reg.remove(name)
            return {"name": name, "removed": True}
        w = hub.dispatcher.resolve(worker)
        wid = w.id if w else worker
        if wid not in rec["delegates"]:
            raise KitError(f"{worker} is not a delegate of {name}")
        if rec["delegates"] == [wid]:
            raise KitError(f"{worker} is {name}'s only delegate: remove the appliance instead")
        rec["delegates"].remove(wid)
        reg.put(rec)
        return {"appliance": view(rec), "removed": False}

    hub.plugins.add_builtin(MANIFEST, {"appliance.add": add, "appliance.list": list_,
                                       "appliance.remove": remove})
