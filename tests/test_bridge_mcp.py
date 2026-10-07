"""Plexora's MCP server as a peer on the shared protocol.

SCIMAP Pro reaches Plexora over MCP when no Plexora server is running: it
spawns `plexora mcp serve --origin bridge --bridge-nonce N` with
`SPATIALBRIDGE_NONCE=N`. These tests build that server in-process and talk to
it with the protocol's own client: the conformance suite passes, the bridge
tools answer, errors come back in the protocol's shape, and the origin is
`bridge` only when the nonce matches.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from plexora.agent import bridge_wire, registry
from plexora.agent.registry import ORIGIN_BRIDGE, ORIGIN_MCP, Capability


class _Empty(BaseModel):
    pass


def _paid(call, inp):
    from plexora.agent.registry import CALL_ORIGIN

    return {"answered": True, "origin": CALL_ORIGIN.get()}


def _server(origin=None):
    from plexora.mcp.server import Runtime, build_server

    runtime = Runtime(names=["gating", "roi", "qc"], origin=origin)
    registry.register(Capability(name="test.paid", owner="test", purpose="A Paid read.",
                                 permission="read", input_model=_Empty, handler=_paid,
                                 entitlement="ai"))
    return build_server(runtime=runtime)


def _peer(server):
    from spatialbridge.client import PeerClient
    from spatialbridge.transports.mcp import McpTransport

    return PeerClient("plexora", McpTransport(server, provider="plexora"))


@pytest.fixture
def peer():
    registry._reset_for_tests()
    client = _peer(_server(ORIGIN_BRIDGE))
    yield client
    client.close()
    registry._reset_for_tests()


def test_plexora_passes_the_protocols_conformance_suite(peer):
    from spatialbridge import conformance

    assert conformance.check(peer) == []


def test_the_bridge_tools_are_all_there_by_the_contracts_names(peer):
    from spatialbridge.tools import ALL_TOOLS

    names = {tool["name"] for tool in peer.transport.list_tools()}
    assert set(ALL_TOOLS) <= names
    info = peer.invoke("bridge_info", {})
    assert info["provider"] == "plexora" and info["protocol"].startswith("1.")
    assert info["origin"] == "bridge"


def test_capabilities_over_mcp_are_descriptors_with_roles(peer):
    descriptors = {d.tool_name: d for d in peer.capabilities()}
    assert descriptors["viewer_open_project"].roles == ["image.inspect"]
    assert descriptors["qc_session_start"].roles == ["qc.auto"]
    assert descriptors["viewer_open_project"].viewer_required


def test_errors_come_back_in_the_protocols_shape(peer):
    from spatialbridge.errors import BridgeError

    with pytest.raises(BridgeError) as unknown_project:
        peer.invoke("inspect_project", {"project": "nope"})
    assert unknown_project.value.code == "job_failed"
    assert unknown_project.value.detail["plexora_code"] == "unknown_project"
    with pytest.raises(BridgeError) as invalid:
        peer.invoke("bridge_collect", {"kind": "gates", "arguments": {}})
    assert invalid.value.code == "invalid_input"


def test_a_bridge_origin_needs_no_mcp_add_on(license_issuer):
    license_issuer.install()                       # Paid default: ["ai"], no mcp
    registry._reset_for_tests()
    bridged = _peer(_server(ORIGIN_BRIDGE))
    try:
        assert bridged.invoke("test_paid", {}) == {"answered": True, "origin": "bridge"}
    finally:
        bridged.close()
    registry._reset_for_tests()
    outside = _peer(_server(ORIGIN_MCP))
    try:
        from spatialbridge.errors import BridgeError

        with pytest.raises(BridgeError) as refused:
            outside.invoke("test_paid", {})
        assert refused.value.code == "license_required"
    finally:
        outside.close()
        registry._reset_for_tests()


def test_an_outside_agent_calling_a_bridge_tool_stays_an_outside_agent(license_issuer):
    """`bridge_invoke(to="plexora")` from an agent on Plexora's own MCP server
    runs the capability as that agent: no origin is laundered."""
    from spatialbridge.errors import BridgeError

    license_issuer.install()
    registry._reset_for_tests()
    outside = _peer(_server(ORIGIN_MCP))
    try:
        with pytest.raises(BridgeError) as refused:
            outside.invoke("bridge_invoke", {"to": "plexora", "capability": "test_paid"})
        assert refused.value.code == "license_required"
    finally:
        outside.close()
        registry._reset_for_tests()


def test_the_nonce_decides_the_origin():
    env = {"SPATIALBRIDGE_NONCE": "abc123"}
    assert bridge_wire.mcp_origin("bridge", "abc123", env) == ORIGIN_BRIDGE
    assert bridge_wire.mcp_origin("bridge", "wrong", env) == ORIGIN_MCP
    assert bridge_wire.mcp_origin("bridge", None, env) == ORIGIN_MCP
    assert bridge_wire.mcp_origin("bridge", "abc123", {}) == ORIGIN_MCP
    assert bridge_wire.mcp_origin("mcp", "abc123", env) == ORIGIN_MCP
    assert bridge_wire.mcp_origin(None, None, env) == ORIGIN_MCP


def test_the_cli_takes_the_flags_and_falls_back_loudly(monkeypatch, capsys):
    from plexora import cli
    from plexora.mcp import server as mcp_server

    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda **kwargs: seen.update(kwargs))
    monkeypatch.setenv("SPATIALBRIDGE_NONCE", "n1")
    assert cli.main(["mcp", "serve", "--no-attach", "--origin", "bridge",
                     "--bridge-nonce", "n1"]) == 0
    assert seen["origin"] == ORIGIN_BRIDGE
    assert cli.main(["mcp", "serve", "--no-attach", "--origin", "bridge",
                     "--bridge-nonce", "nope"]) == 0
    assert seen["origin"] == ORIGIN_MCP
    assert "--origin bridge ignored" in capsys.readouterr().err


def test_server_info_names_the_bridge_and_the_origin():
    from plexora.mcp.server import Runtime, _server_info

    registry._reset_for_tests()
    try:
        info = _server_info(Runtime(names=[], origin=ORIGIN_BRIDGE))
        assert info["origin"] == "bridge"
        assert info["bridge"]["protocol"] == "1.0" and info["bridge"]["available"] is True
        assert "token" not in str(info["bridge"]).lower()
    finally:
        registry._reset_for_tests()


def test_the_codes_agree_with_the_protocol():
    from spatialbridge.errors import CODES
    from spatialbridge.schema import ROLES

    from plexora.agent import bridge_roles

    assert set(bridge_wire.BRIDGE_CODES) == set(CODES)
    assert set(bridge_roles.ROLES) == set(ROLES)
    assert all(role in ROLES for roles in bridge_roles.TOOL_ROLES.values() for role in roles)


def test_without_the_package_the_bridge_tools_say_how_to_get_it(monkeypatch):
    from plexora.agent import AgentSession
    from plexora.agent.core import bridge

    registry._reset_for_tests()
    registry.discover([])
    monkeypatch.setattr(bridge, "available", lambda: False)
    try:
        answer = registry.invoke(AgentSession(), "bridge_info", {})
        assert answer["error"]["code"] == "capability_unavailable"
        assert "plexora[bridge]" in answer["error"]["detail"]["hint"]
    finally:
        registry._reset_for_tests()
