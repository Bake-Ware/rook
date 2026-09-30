"""The hub side of the plugin API.

:class:`rook.hub.node.HubNode` hosts hub-placed plugins (placement
``is_hub``) with the same :class:`rook.core.host.PluginHost` the workers use
and serves their caps on the band as the reserved worker ``rook``.
:mod:`rook.hub.mcp_tools` generates MCP tools for caps declared
``tool=True``. Built-in hub plugins live in :mod:`rook.hub.plugins`.
"""

from .node import HUB_WORKER_NAME, HubNode

__all__ = ["HUB_WORKER_NAME", "HubNode"]
