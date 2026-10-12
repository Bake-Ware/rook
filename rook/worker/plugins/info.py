"""info.* — host/system identification, cheap to call."""

from __future__ import annotations

import os
import platform
import socket
import time

from ..plugin import Plugin, capability


_BOOT = time.time()


class InfoPlugin(Plugin):
    NAMESPACE = "info"

    @capability("host", risk="read")
    def _host(self) -> dict:
        """Hostname, OS, release, machine, Python version and process id of this worker."""
        return {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        }

    @capability("uptime", risk="read")
    def _uptime(self) -> float:
        """Seconds since this worker process started."""
        return time.time() - _BOOT

    @capability("ping", risk="read")
    def _ping(self) -> str:
        """Liveness check; returns ``"pong"``."""
        return "pong"


PLUGIN = InfoPlugin
