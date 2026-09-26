"""Core's own capabilities: what every project has, whatever plugins are installed."""

from __future__ import annotations


def core_capabilities() -> list:
    from plexora.agent.core import image, project, table

    capabilities = []
    for module in (project, image, table):
        capabilities.extend(module.capabilities())
    # Added by later modules when they exist in this build.
    for name in ("plexora.agent.core.scene", "plexora.agent.core.visual",
                 "plexora.agent.core.viewer"):
        try:
            module = __import__(name, fromlist=["capabilities"])
        except ImportError:
            continue
        capabilities.extend(module.capabilities())
    return capabilities
