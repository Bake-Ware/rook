"""The ``backup.*`` hub caps over :mod:`rook.core2.hub.backup`. Owners only
(in person, through their agents, or a job they made).

A blank ``target`` is the default (castling-backup, Bake: "Multiple backup
paths, with a reasonable one by default"; the default is "nightly snapshots
to another worker on the band (no setup, data stays on own machines)"):
another online worker of an owner's that runs ``backup-store``, named first
in order, never one on the hub's own machine."""
from __future__ import annotations

import socket
from pathlib import Path

from rook.kit.ctx import KitError

from . import KEEP, BackupError, backups, fetch_verify, run, target

MANIFEST = Path(__file__).parent
STORE = "backup-store.put"


def default_worker(hub) -> str | None:
    """The worker a blank target means: online, an owner's, serving
    backup-store, and not the hub's own machine (a worker is named after its
    host unless told otherwise); the first by name, so it stays the same one
    night after night while it is up."""
    here = socket.gethostname().lower()
    for k in sorted(hub.known.values(), key=lambda k: k.name):
        w = hub.dispatcher.workers.get(k.id)
        if (k.online and k.id != hub.node and k.name.lower() != here and w is not None and STORE in w.caps
                and hub.identities.is_owner(hub.enroll.owner_of(k.id))):
            return k.name
    return None


def install(hub) -> None:
    def setup(ctx):
        if not ctx.caller.owner:
            raise KitError("denied: only an owner backs up the hub")
        spec = str(ctx.settings.get("target") or "")
        if not spec:
            name = default_worker(hub)
            if name is None:
                raise BackupError("no backup target yet: the default is another of your workers running "
                                  "backup-store, and none is online. Install backup-store on one, or set "
                                  "backup's target (dir:/path, worker:<name> or rclone:<remote>:<path>)")
            spec = f"worker:{name}"
        worker = spec.partition(":")[2] if spec.startswith("worker:") else None

        def call(cap: str, args: dict):
            return ctx.call_on(worker, cap, **args)
        return spec, int(ctx.settings.get("keep") or KEEP), (call if worker else None)

    def guard(fn):
        def go(ctx, **args):
            try:
                return fn(ctx, *setup(ctx), **args)
            except BackupError as e:
                raise KitError(str(e)) from None
            except OSError as e:
                raise KitError(f"backup: {e}") from None
        return go

    def run_(ctx, spec, keep, call) -> dict:
        if hub.cfg.file is None:
            raise KitError("this hub was started without a config file to back up")
        return run(config=hub.cfg.file, data=hub.data, library=hub.cfg.library, spec=spec,
                   keep=keep, call=call)

    def list_(ctx, spec, keep, call) -> dict:
        t = target(spec, call)
        return {"target": t.label, "keep": keep, "backups": backups(t, hub.hub_id)}

    def verify(ctx, spec, keep, call, name: str | None = None) -> dict:
        t = target(spec, call)
        if not name:
            found = backups(t, hub.hub_id)
            if not found:
                raise KitError(f"no backups of this hub on {t.label}")
            name = found[0]["name"]
        return {"name": name, "target": t.label, **fetch_verify(t, name, hub.data)}

    hub.plugins.add_builtin(MANIFEST, {"backup.run": guard(run_), "backup.list": guard(list_),
                                       "backup.verify": guard(verify)})
