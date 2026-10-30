"""Contracts: shared interfaces (``notify.send``, ``llm.complete``, ``voice.tts``...)
and the plugins that provide them (castling-manifest-2, "Contracts and provides")::

    contracts:                       # usually core or a package
      notify.send: { doc: Get a message to a person., args: { to: { type: str }, text: { type: str, required: true } } }
    provides:                        # in an implementing plugin
      notify.send: telegram.send

The hub serves each contract as a cap of its own name; a call to it is routed
to the chosen provider: the one picked in the hub setting (house agent or
user), else the first installed. The provider runs with the original caller's
rights, as if they had called it directly.

The compatibility check here is by argument names (the provider accepts every
contract arg and needs nothing the contract doesn't give).
TODO(castling:manifest): swap for the validator's full schema check.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping, MutableMapping

from .ctx import Caller, KitError

__all__ = ["Contracts"]


class Contracts:
    """``choices`` is the hub setting mapping contract -> chosen provider cap."""

    def __init__(self, choices: MutableMapping[str, str] | None = None):
        self.choices = choices if choices is not None else {}
        self._defs: dict[str, dict] = {}            # contract -> {spec, owner}
        self._providers: dict[str, list[tuple[str, str]]] = {}   # contract -> [(cap, plugin)] install order
        self._specs: dict[str, Mapping | None] = {}              # provider cap -> its spec

    # -- manifest adapter -----------------------------------------------------

    def load(self, manifest: Mapping) -> None:
        """Register a manifest's ``contracts`` and ``provides``."""
        plugin = manifest["plugin"]
        for name, spec in (manifest.get("contracts") or {}).items():
            self.define(name, spec or {}, plugin)
        caps = manifest.get("caps") or {}
        for contract, cap in (manifest.get("provides") or {}).items():
            short = cap[len(plugin) + 1:] if cap.startswith(plugin + ".") else cap
            self.provide(contract, f"{plugin}.{short}", plugin, caps.get(short))

    def unload(self, plugin: str) -> None:
        """Drop a plugin's providers and contracts. A choice naming a gone
        provider falls back to the first installed until it returns."""
        for contract in list(self._providers):
            self._providers[contract] = [(c, p) for c, p in self._providers[contract] if p != plugin]
            self._specs = {c: s for c, s in self._specs.items() if not c.startswith(plugin + ".")}
            if not self._providers[contract]:
                del self._providers[contract]
        for name in [n for n, d in self._defs.items() if d["owner"] == plugin]:
            del self._defs[name]

    # -- registry -------------------------------------------------------------

    def define(self, name: str, spec: Mapping, owner: str) -> None:
        have = self._defs.get(name)
        if have is not None and have["owner"] != owner:
            raise KitError(f"contract {name} is already defined by {have['owner']}")
        for cap in self.providers(name):     # providers installed before their contract
            if self._specs.get(cap) is not None:
                _check_compatible(name, spec, cap, self._specs[cap])
        self._defs[name] = {"spec": dict(spec), "owner": owner}

    def provide(self, contract: str, cap: str, plugin: str, cap_spec: Mapping | None = None) -> None:
        if not cap.startswith(plugin + "."):
            raise KitError(f"{plugin} can only provide {contract} with its own cap, not {cap}")
        if contract in self._defs and cap_spec is not None:
            _check_compatible(contract, self._defs[contract]["spec"], cap, cap_spec)
        entries = self._providers.setdefault(contract, [])
        if (cap, plugin) not in entries:
            entries.append((cap, plugin))
        self._specs[cap] = cap_spec

    def contract(self, name: str) -> dict | None:
        d = self._defs.get(name)
        return dict(d["spec"]) if d else None

    def contracts(self) -> list[str]:
        return sorted(set(self._defs) | set(self._providers))

    def providers(self, contract: str) -> list[str]:
        return [c for c, _ in self._providers.get(contract, [])]

    def choose(self, contract: str) -> str:
        """The provider cap a call to ``contract`` goes to."""
        installed = self.providers(contract)
        if not installed:
            raise KitError(f"nothing provides {contract}; install a plugin that does")
        chosen = self.choices.get(contract)
        return chosen if chosen in installed else installed[0]

    def set_choice(self, contract: str, cap: str | None) -> None:
        """Pick a provider (None clears it back to the default)."""
        if cap is None:
            self.choices.pop(contract, None)
            return
        if cap not in self.providers(contract):
            raise KitError(f"{cap} doesn't provide {contract}")
        self.choices[contract] = cap

    async def route(self, contract: str, args: Mapping, caller: Caller,
                    dispatch: Callable[[str, dict, Caller], Any]) -> Any:
        """Serve a call to the contract's service cap through the chosen provider."""
        res = dispatch(self.choose(contract), dict(args), caller)
        return await res if inspect.isawaitable(res) else res


def _check_compatible(contract: str, spec: Mapping, cap: str, cap_spec: Mapping) -> None:
    want = spec.get("args") or {}
    have = cap_spec.get("args") or {}
    missing = [a for a in want if a not in have]
    extra = [a for a, s in have.items() if (s or {}).get("required") and a not in want]
    if missing or extra:
        why = []
        if missing:
            why.append(f"doesn't take {', '.join(missing)}")
        if extra:
            why.append(f"requires {', '.join(extra)} the contract doesn't give")
        raise KitError(f"{cap} can't provide {contract}: it {' and '.join(why)}")
