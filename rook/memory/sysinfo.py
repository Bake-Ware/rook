"""Cheap system stats for context injection."""

from __future__ import annotations

import shutil
import psutil


def get_system_stats() -> str:
    """Return a compact one-liner of system stats. ~100ms."""
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    total_disk, _, free_disk = shutil.disk_usage("C:")
    net = psutil.net_io_counters()

    parts = [
        f"CPU {cpu}%",
        f"RAM {mem.used // (1024**3)}/{mem.total // (1024**3)}GB ({mem.percent}%)",
        f"Disk {free_disk // (1024**3)}GB free",
        f"Net {net.bytes_sent // (1024**2)}MB↑ {net.bytes_recv // (1024**2)}MB↓",
    ]

    # GPU/VRAM
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            info = pynvml.nvmlDeviceGetMemoryInfo(h)
            parts.append(f"GPU{i} {info.used // (1024**3)}/{info.total // (1024**3)}GB VRAM")
        pynvml.nvmlShutdown()
    except Exception:
        pass

    return " | ".join(parts)
