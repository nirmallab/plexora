"""plexora.mcp.profiles: each profile names only tools that exist."""

import contextlib
import sys

import pytest

from plexora.agent import registry
from plexora.mcp import profiles


def test_every_profile_names_only_registered_tools():
    with contextlib.redirect_stdout(sys.stderr):
        registry.discover(None)
    tools = [c.tool_name for c in registry.all_capabilities()]
    for name in profiles.names():
        assert profiles.unknown(name, tools) == [], name
    gating = [t for t in tools if profiles.allows("gating", t)]
    assert "gating_answer" in gating and "qc_answer" not in gating
    assert len(gating) < len(tools) and all(profiles.allows("full", t) for t in tools)


def test_an_unknown_profile_is_refused_by_name():
    with pytest.raises(SystemExit, match="gating"):
        profiles.check("everything")
    assert profiles.check(None) == "full"
