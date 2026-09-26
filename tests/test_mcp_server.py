"""The MCP server, through the real protocol (an in-process client)."""

import json

import anyio
import pytest

mcp = pytest.importorskip("mcp")

from plexora.agent import AgentSession, Policy, registry  # noqa: E402
from plexora.mcp import serialize  # noqa: E402
from plexora.mcp.server import build_server  # noqa: E402
from tests.agent_fixtures import make_synthetic_project  # noqa: E402


@pytest.fixture
def server(tmp_path):
    make_synthetic_project(tmp_path)
    return build_server(AgentSession())


def _run(fn):
    return anyio.run(fn)


def _json(result):
    return json.loads(result.content[-1].text)


def test_tools_carry_the_capabilities_schema_and_hints(server):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = _run(go)
    for name in ("list_projects", "inspect_project", "set_gate", "server_info",
                 "validate_scope", "read_skill", "create_roi"):
        assert name in tools, name
    schema = tools["set_gate"].input_schema
    assert schema["required"] == ["project", "marker", "low"]
    assert "table's own units" in schema["properties"]["low"]["description"]
    assert tools["inspect_project"].annotations.read_only_hint is True
    assert tools["write_gates_to_source"].annotations.destructive_hint is True
    assert "receipt" in tools["set_gate"].description


def test_inspect_status_and_a_gate_round_trip(server, tmp_path):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            inspected = await client.call_tool("inspect_project", {"project": "synth"})
            status = await client.call_tool("get_resource_status", {"project": "synth"})
            written = await client.call_tool("set_gate", {"project": "synth",
                                                          "marker": "CD8", "low": 900})
            resource = await client.read_resource("plexora://project/synth/gates")
            return inspected, status, written, resource

    inspected, status, written, resource = _run(go)
    assert _json(inspected)["table"]["n_cells"] == 64
    assert _json(status)["resources"]["image"]["readable"] is True
    receipt = _json(written)["receipt"]
    assert receipt["after"]["low"] == 900
    assert (tmp_path / ".agent" / "audit.jsonl").exists()
    gates = json.loads(resource.contents[0].text)["gates"]
    assert next(g for g in gates if g["marker"] == "CD8")["low"] == 900


def test_failures_are_tool_errors_with_a_problem(server):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool("get_gate", {"project": "nope", "marker": "CD8"})

    result = _run(go)
    assert result.is_error
    text = result.content[0].text
    problem = json.loads(text[text.index("{"):])["error"]
    assert problem["code"] == "unknown_project"


def test_validate_scope_answers_in_four_states(server):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            answers = {}
            for request in ("what markers does it have", "gate CD8",
                            "run a single-cell RNA velocity analysis"):
                result = await client.call_tool("validate_scope",
                                                {"request": request, "project": "synth"})
                answers[request] = _json(result)["state"]
            return answers

    answers = _run(go)
    assert answers["what markers does it have"] == "can_analyze"
    assert answers["gate CD8"] == "can_execute"
    assert answers["run a single-cell RNA velocity analysis"] == "outside_domain"


def test_bounding_truncates_and_says_so():
    big = {"items": list(range(50_000)), "name": "x"}
    text = serialize.bound(big, limit=2_000)
    assert len(text) <= 2_000
    parsed = json.loads(text)
    assert parsed["truncated"] is True and parsed["_truncated"][0]["path"] == "items"


def test_nan_and_bytes_survive_serialization():
    parsed = json.loads(serialize.bound({"x": float("nan"), "b": b"abc"}))
    assert parsed == {"x": None, "b": "<3 bytes>"}
