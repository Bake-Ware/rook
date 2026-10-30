"""Builtin manifests: core's own ``plugin.yaml`` (``builtin: true``).

``CORE`` is core.yaml: the base contracts, core events and the four user
presets. Load it with ``rook.kit.manifest.load(CORE)``. ``WORKER`` and
``STREAM`` are the worker's own builtin caps (liveness, status, its call
record, restart; the stream caps).
"""

from pathlib import Path

CORE = Path(__file__).with_name("core.yaml")
# The worker's own caps beside core's: worker.* and stream.* (rook/core2/worker/builtins.py).
WORKER = Path(__file__).with_name("worker.yaml")
STREAM = Path(__file__).with_name("stream.yaml")
