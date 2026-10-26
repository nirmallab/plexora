"""A bridge call cannot carry the user's approval for a source write.

The chat asks the person before any capability writes into their files or
cannot be undone; a direct MCP call is confirmed by the client. Reached
through `bridge_invoke`, the `confirm: true` inside the nested arguments was
written by whoever composed the bridge call -- so those capabilities only run
when called directly.
"""

import pytest

pytest.importorskip("spatialbridge")

from spatialbridge.errors import BridgeError  # noqa: E402

from plexora.agent import registry  # noqa: E402
from plexora.agent.bridge_provider import PlexoraProvider  # noqa: E402
from plexora.agent import Policy  # noqa: E402


def _gated():
    registry.discover(None)
    return [c for c in registry.all_capabilities()
            if c.permission in ("source_file_write", "destructive")]


def test_a_gated_capability_is_refused_through_the_bridge():
    gated = _gated()
    assert gated, "expected at least one source-write or destructive capability"
    provider = PlexoraProvider(policy=Policy(allow_writes=True, allow_source_writes=True,
                                             allow_destructive=True))
    for capability in gated:
        with pytest.raises(BridgeError) as raised:
            provider.invoke(capability.tool_name, {"confirm": True})
        assert "directly" in str(raised.value.hint or raised.value)
