"""Rook worker — self-contained plugins exposing dot-namespaced capabilities.

Capabilities live in a flat registry keyed by dotted strings ("shell.exec",
"files.read", "screen.capture"). A plugin is one Python file under
`rook.worker.plugins` that subclasses :class:`Plugin`, claims a top-level
namespace via `NAMESPACE`, and marks its methods with `@capability("sub")`.
Sub-namespaces may themselves be dotted ("env.get").

Transports are the same shape but live under `rook.worker.transports`. A
transport plugin is responsible for moving framed messages between the
worker and the outside world (Telesthete hub, raw UDP, WebSocket, …).
"""

from .plugin import Plugin, capability
from .registry import CapabilityRegistry

__all__ = ["Plugin", "capability", "CapabilityRegistry"]
