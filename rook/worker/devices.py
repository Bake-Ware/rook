"""The devices this worker is a delegate for (``rook.core.devices``).

Rebuilt from the loaded plugins on every announce, so a plugin enabled,
disabled or reloaded at runtime brings its devices with it and takes them
away again, with no bookkeeping of its own. Each device gets a capability
registry of its own (its ``@capability`` methods plus ``caps.describe`` and
``info.ping``), and the worker dispatches calls targeted at a device's
``worker_id`` into it (``Worker._on_message``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..core.devices import Device, device_worker_id
from ..core.registry import CapabilityRegistry

log = logging.getLogger("rook.worker.devices")


@dataclass
class DeviceEntry:
    worker_id: str
    namespace: str          # the delegating plugin's namespace
    device: Device
    registry: CapabilityRegistry = field(default_factory=CapabilityRegistry)

    def tiers(self) -> dict[str, str]:
        out = {}
        for cap in self.registry.list():
            risk = getattr(self.registry.meta(cap), "risk", None)
            if risk:
                out[cap] = risk
        return out


class DeviceTable:
    """``worker_id -> DeviceEntry`` for every device the loaded plugins drive."""

    def __init__(self, delegate_id: str) -> None:
        self.delegate_id = delegate_id
        self._entries: dict[str, DeviceEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, worker_id: str | None) -> DeviceEntry | None:
        return self._entries.get(worker_id) if worker_id else None

    def entries(self) -> list[DeviceEntry]:
        return list(self._entries.values())

    def refresh(self, plugins) -> list[DeviceEntry]:
        """Re-read ``devices()`` from every plugin that has it. An entry is
        kept (registry and all) while the plugin returns the same object."""
        fresh: dict[str, DeviceEntry] = {}
        for p in plugins:
            devices = getattr(p, "devices", None)
            if not callable(devices):
                continue
            try:
                listed = list(devices() or ())
            except Exception:
                log.exception("%s.devices() raised; its devices are left out", p.NAMESPACE)
                continue
            for dev in listed:
                if not isinstance(dev, Device):
                    log.warning("%s.devices() returned a non-Device %r", p.NAMESPACE, dev)
                    continue
                wid = device_worker_id(self.delegate_id, p.NAMESPACE, dev.key)
                if wid in fresh:
                    log.warning("%s: duplicate device key %r; keeping the first",
                                p.NAMESPACE, dev.key)
                    continue
                old = self._entries.get(wid)
                fresh[wid] = old if old is not None and old.device is dev \
                    else self._build(wid, p.NAMESPACE, dev)
        self._entries = fresh
        return self.entries()

    def _build(self, wid: str, namespace: str, dev: Device) -> DeviceEntry:
        entry = DeviceEntry(worker_id=wid, namespace=namespace, device=dev)
        reg = entry.registry
        for dotpath, fn in dev.caps().items():
            reg.register(dotpath, fn)

        def _caps_describe(prefix: str = "") -> dict:
            """Arg schema + docstring for every capability on this device (for the UI)."""
            return reg.describe(prefix)

        def _ping() -> dict:
            """Liveness of the delegate answering for this device."""
            return {"ok": True, "ts": time.time(), "device": dev.name,
                    "delegate": self.delegate_id}

        reg.register("caps.describe", _caps_describe)
        if not reg.has("info.ping"):
            reg.register("info.ping", _ping)
        return entry

    def announce(self, entry: DeviceEntry, base: dict) -> dict:
        """The announce for one device. ``base`` carries what the device
        shares with its delegate (version, build, ``authz``)."""
        dev = entry.device
        caps = entry.registry.list()
        msg = {
            "kind": "announce",
            "worker_id": entry.worker_id,
            "name": dev.name,
            "description": dev.description,
            "caps": caps,
            # No envelope features: callers send a device plain JSON, as a
            # broadcast targeted call the delegate picks up (core spec 14).
            "features": [],
            "plugins": [],
            "delegate": self.delegate_id,
            "device": dev.info(),
            **base,
        }
        tiers = entry.tiers()
        if tiers:
            msg["tiers"] = tiers
        try:
            hb = dev.heartbeat()
        except Exception:
            log.debug("heartbeat() raised for device %s", dev.name, exc_info=True)
            hb = None
        if hb:
            msg["hb"] = {dev.NAMESPACE: hb}
        return msg
