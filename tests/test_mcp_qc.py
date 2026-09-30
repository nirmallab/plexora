"""Quality control over the real MCP protocol: tools, prompts, resources."""

import json

import pytest

from plexora.agent import AgentSession, registry
from tests.qc_fixtures import make_qc_project

pytest.importorskip("mcp")


def test_qc_tools_prompts_and_resources_are_served(tmp_path):
    import anyio
    from mcp import Client

    from plexora.mcp.server import build_server

    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    registry.discover(["roi", "qc"])
    server = build_server(AgentSession(), names=["roi", "qc"])

    async def go():
        async with Client(server) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            prompt = await c.get_prompt("qc_image", {"project": "qcsynth"})
            text = await c.read_resource("plexora://project/qcsynth/qc")
            templates = {t.uri_template for t in (await c.list_resource_templates())
                         .resource_templates}
            return tools, prompts, prompt, text, templates

    tools, prompts, prompt, text, templates = anyio.run(go)
    assert {"qc_session_start", "qc_next", "qc_answer", "get_qc_results",
            "set_qc_strictness", "export_qc", "refresh_qc"} <= tools
    assert {"run_blur_check", "get_blur_check", "set_blur_check", "clear_blur_check",
            "write_blur_regions"} <= tools
    assert {"qc_image", "review_qc"} <= prompts
    body = prompt.messages[0].content.text
    assert "qc_session_start" in body and "## Decision logic" in body
    found = json.loads(text[0].text if isinstance(text, list) else text.contents[0].text)
    assert found.get("project") == "qcsynth" or "result" in json.dumps(found)
    assert "plexora://qc/session/{session_id}" in templates
