"""Plexora for external agents: the headless half.

Claude Code, Codex, Cursor -- anything that speaks MCP -- reaches Plexora
through `plexora mcp serve` (plexora/mcp), which is a thin adapter over this
package. Here is what an agent can actually do:

- **capabilities** (`registry.py`): typed operations with a purpose, a
  permission class and the requirements a project must meet. Core registers its
  own (`core/`); each plugin declares its own through
  `Plugin.capabilities_factory`.
- **sessions** (`session.py`): provider-backed handles, so an agent reading a
  project never swaps the one on the user's screen.
- **policy, receipts and audit**: what may run, what a mutation did, and a log
  of every attempt.
- **visual evidence** (`render.py`, `gate_panel.py`): deterministic server-side
  renders with segmentation and gate overlays, and a manifest of exactly what
  was drawn.

There is no model-provider code anywhere in it. Agents bring their own model;
Plexora brings the data, the rules and the pictures.
"""

from plexora.agent.errors import AgentError
from plexora.agent.policy import Policy
from plexora.agent.registry import Capability, discover, invoke
from plexora.agent.session import AgentSession

__all__ = ["AgentError", "AgentSession", "Capability", "Policy", "discover", "invoke"]
