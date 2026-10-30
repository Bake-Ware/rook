"""Builtin manifests: core's own ``plugin.yaml`` (``builtin: true``).

``CORE`` is core.yaml: the base contracts, core events and the four user
presets. Load it with ``rook.kit.manifest.load(CORE)``.
"""

from pathlib import Path

CORE = Path(__file__).with_name("core.yaml")
