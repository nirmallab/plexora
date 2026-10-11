"""First-time imports paid before traffic, for a process that is not the viewer.

`plexora mcp serve` runs QC and gating jobs beside the threads that answer
tool calls, so it pays every lazy initializer up front exactly as a data node
does (`data_model.prime_hot_code`, which says why: two threads' first imports
can deadlock the interpreter or leave a module half-initialized). The agent
layer never names the viewer's data model (`tests/test_agent_architecture.py`),
so it primes through here; the work stays where the encoders it warms live.
"""

from __future__ import annotations


def prime_hot_code(log=None):
    """`data_model.prime_hot_code`, looked up when called."""
    from plexora.server.models import data_model

    return data_model.prime_hot_code(log=log)
