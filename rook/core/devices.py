"""Device workers: things a delegate worker stands in for.

A *device* is something that cannot run a worker itself (a TV, a light
switch, a thermostat) but that a worker on the same network can drive. The
worker that drives it is its *delegate*. Each device shows on the band as a
worker of its own: its own ``worker_id``, name, caps and heartbeat, plus
``delegate`` (the delegate's ``worker_id``) and a ``device`` block, so an agent
calls ``office-tv`` directly and never needs to know which box is in the
middle. See ``docs/design/devices.md``.

A plugin becomes a delegate by implementing ``devices()``, returning the
:class:`Device` objects it currently drives. Devices declare caps exactly as
plugins do, with ``@capability`` methods under their :attr:`Device.NAMESPACE`,
and report live state through :meth:`Device.heartbeat`.

Stdlib only: it ships inside the worker bundle.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

__all__ = ["Device", "clean_device", "device_worker_id", "valid_device_name"]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")


def valid_device_name(name: str) -> bool:
    """Device names follow worker names: letters, digits, space, ``.``, ``_``
    and ``-``, starting with a letter or digit, at most 63 characters."""
    return isinstance(name, str) and bool(_NAME.match(name))


def clean_device(block) -> dict:
    """The announce ``device`` block as a receiver keeps it: a few short
    scalar facts (``type``, ``driver``, ``model``…), never more. ``type``
    always present (``"device"`` when the announce gave none)."""
    out: dict[str, Any] = {}
    if isinstance(block, dict):
        for k, v in list(block.items())[:8]:
            if isinstance(k, str) and len(k) <= 32 and (
                    isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 64)):
                out[k] = v
    if not isinstance(out.get("type"), str) or not out["type"]:
        out["type"] = "device"
    return out


def device_worker_id(delegate_id: str, namespace: str, key: str) -> str:
    """The stable ``worker_id`` of a device: derived from its delegate's id,
    the delegating plugin's namespace and the device's key, so it survives
    restarts of the delegate. 32 hex characters like a worker's own uuid4
    id, so everything that stores or validates worker ids takes it as is."""
    h = hashlib.sha256(f"device\0{delegate_id}\0{namespace}\0{key}".encode()).hexdigest()
    return "d" + h[:31]


class Device:
    """One device a delegate drives. Subclass, set :attr:`NAMESPACE` and
    :attr:`TYPE`, and decorate methods with ``@capability``.

    ``key`` identifies the device within its delegate plugin and must stay
    stable (it feeds the derived ``worker_id``); ``name`` is what the band
    shows and callers address.
    """

    NAMESPACE: str = ""   # caps namespace, e.g. "tv" -> tv.status, tv.launch
    TYPE: str = ""        # what the device is: "tv", "switch", "climate"…
    DRIVER: str = ""      # how it is driven: "webos", "homeassistant"…

    def __init__(self, key: str, name: str, description: str = "") -> None:
        if not self.NAMESPACE:
            raise ValueError(f"{type(self).__name__} must set NAMESPACE")
        if not valid_device_name(name):
            raise ValueError(f"invalid device name: {name!r}")
        self.key = key
        self.name = name
        self.description = description

    def caps(self) -> dict[str, Callable[..., Any]]:
        """``{dotpath: bound method}`` for every ``@capability`` method,
        named under :attr:`NAMESPACE` the same way plugin caps are."""
        out: dict[str, Callable[..., Any]] = {}
        cls = type(self)
        for attr_name in dir(self):
            if isinstance(getattr(cls, attr_name, None), property):
                continue
            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue
            suffix = getattr(attr, "_rook_cap_suffix", None)
            if suffix is None:
                continue
            out[self.NAMESPACE if not suffix else f"{self.NAMESPACE}.{suffix}"] = attr
        return out

    @classmethod
    def declared_caps(cls) -> dict[str, Callable[..., Any]]:
        """``{dotpath: function}`` for every cap the class declares, without
        constructing it (generators, contract tests); like ``Plugin``'s."""
        out: dict[str, Callable[..., Any]] = {}
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if isinstance(attr, property) or not callable(attr):
                continue
            suffix = getattr(attr, "_rook_cap_suffix", None)
            if suffix is None:
                continue
            out[cls.NAMESPACE if not suffix else f"{cls.NAMESPACE}.{suffix}"] = attr
        return out

    def info(self) -> dict:
        """The announce ``device`` block: what this device is and how it is
        driven. Subclasses may add a few scalar facts (model, firmware)."""
        out = {"type": self.TYPE or self.NAMESPACE}
        if self.DRIVER:
            out["driver"] = self.DRIVER
        return out

    def heartbeat(self) -> dict | None:
        """Compact live state carried on the device's announce under its
        namespace (``hb.tv = {on, app, volume}``). Same rules as
        ``Plugin.heartbeat``: tiny, cheap, must not raise, never does I/O.
        Return ``None`` to contribute nothing."""
        return None
