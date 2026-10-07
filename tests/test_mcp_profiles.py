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


# -- owner profiles ------------------------------------------------------------


def test_an_owner_profile_offers_every_tool_of_that_owner(monkeypatch):
    from tests.scimappro_fixtures import install_fake_scimappro

    install_fake_scimappro(monkeypatch)
    registry._reset_for_tests()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            registry.discover(None)
        caps = registry.all_capabilities()
        owned = [c.tool_name for c in caps if c.owner == "scimappro"]
        assert owned and all(profiles.allows("analysis", c.tool_name, c.owner)
                             for c in caps if c.owner == "scimappro")
        # The bridge tools come with it; gating's session does not.
        assert profiles.allows("analysis", "bridge_handoff", "bridge")
        assert not profiles.allows("analysis", "gating_answer", "gating")
        tools = [c.tool_name for c in caps]
        for name in profiles.names():
            assert profiles.unknown(name, tools) == [], name
    finally:
        registry._reset_for_tests()


def test_an_absent_owner_is_not_an_unknown_tool():
    """analysis-lite names SCIMAP Pro's three generic tools; without the plugin
    installed that is an absent plugin, not a rename."""
    with contextlib.redirect_stdout(sys.stderr):
        registry.discover(None)
    tools = [c.tool_name for c in registry.all_capabilities()]
    assert not any(t.startswith("scimappro_") for t in tools)
    assert profiles.unknown("analysis-lite", tools) == []
    # Once the owner is present, a missing name of its is a real problem.
    assert profiles.unknown("analysis-lite", tools + ["scimappro_run"]) == [
        "scimappro_describe_function", "scimappro_search_functions"]


def test_the_server_offers_an_owner_profiles_tools(monkeypatch):
    from plexora.mcp.server import Runtime, _offered
    from tests.scimappro_fixtures import install_fake_scimappro

    install_fake_scimappro(monkeypatch)
    registry._reset_for_tests()
    try:
        runtime = Runtime(names=["scimappro"])
        runtime.profile = "analysis"
        offered = {cap.tool_name for cap in _offered(runtime)}
        assert "scimappro_sp_spatial_neighbors" in offered and "bridge_route" in offered
        assert "list_projects" in offered and "render_region" not in offered
    finally:
        registry._reset_for_tests()
