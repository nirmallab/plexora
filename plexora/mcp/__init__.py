"""Plexora as an MCP server: `plexora mcp serve`.

A thin adapter. Every tool here is one capability from `plexora.agent`,
called through `plexora.agent.registry.invoke` -- so validation, permissions,
receipts and the audit log are the agent layer's, and this package only
translates between them and the protocol.

The MCP SDK is an optional dependency (`pip install 'plexora[ai]'`), imported
only when a server is actually built; importing this package costs nothing.
"""

INSTALL_HINT = "pip install 'plexora[ai]'"


class MCPMissing(ImportError):
    """The MCP SDK is not installed."""


def require_mcp():
    """The SDK's server module, or an ImportError that says how to get it."""
    try:
        from mcp.server import mcpserver
    except ImportError as exc:
        raise MCPMissing(
            "Plexora's MCP server needs the MCP SDK, which is not installed.\n"
            f"  {INSTALL_HINT}") from exc
    return mcpserver
