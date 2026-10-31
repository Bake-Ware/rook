"""The ``secret.*`` hub caps over :class:`~rook.core2.hub.vault.Vault`.
Owners only; ``secret.delete`` and ``secret.reveal`` are hard stops by their
effects (dispatch holds them for the person)."""
from __future__ import annotations

from pathlib import Path

from rook.kit.ctx import KitError

from . import Locked, VaultError

MANIFEST = Path(__file__).parent


def install(hub) -> None:
    v = hub.vault

    def owner(ctx) -> None:
        if not ctx.caller.owner:
            raise KitError("denied: only an owner uses the vault")

    def guard(fn):
        def run(ctx, **args):
            owner(ctx)
            try:
                return fn(ctx, **args)
            except (VaultError, Locked) as e:
                raise KitError(str(e)) from None
        return run

    def list_(ctx) -> dict:
        return {"secrets": v.list(), "locked": v.locked}

    def set_(ctx, name: str, value: str, description: str | None = None) -> dict:
        return v.set(name, value, by=ctx.caller.person or "", description=description or None)

    def delete(ctx, name: str) -> dict:
        if not v.delete(name):
            raise KitError(f"{name} is not in the vault")
        return {"name": name, "deleted": True}

    def reveal(ctx, name: str) -> dict:
        try:
            return {"name": name, "value": v.get(name)}
        except KeyError:
            raise KitError(f"{name} is not in the vault") from None

    hub.plugins.add_builtin(MANIFEST, {"secret.list": guard(list_), "secret.set": guard(set_),
                                       "secret.delete": guard(delete), "secret.reveal": guard(reveal)})
