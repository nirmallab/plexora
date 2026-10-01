"""Core's own capabilities: what every project has, whatever plugins are installed."""

from __future__ import annotations


def core_capabilities() -> list:
    from plexora.agent.core import image, jobs, operations, project, table

    capabilities = []
    for module in (project, image, table, operations, jobs):
        capabilities.extend(module.capabilities())
    # Added by later modules when they exist in this build.
    for name in ("plexora.agent.core.scene", "plexora.agent.core.visual",
                 "plexora.agent.core.cells", "plexora.agent.core.viewer",
                 # The Plexora AI conversation (plexora/ai/harness), absent from a
                 # build that ships without the harness.
                 "plexora.ai.harness.chat_capabilities"):
        try:
            module = __import__(name, fromlist=["capabilities"])
        except ImportError:
            continue
        capabilities.extend(module.capabilities())
    return capabilities
