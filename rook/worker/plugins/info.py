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

    @capability("host")
    def _host(self) -> dict:
        return {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        }

    @capability("uptime")
    def _uptime(self) -> float:
        return time.time() - _BOOT

    @capability("ping")
    def _ping(self) -> str:
        return "pong"


PLUGIN = InfoPlugin
