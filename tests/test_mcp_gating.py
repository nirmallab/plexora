"""Automatic gating over MCP: prompts, resources, and a packet's images."""

import json

import anyio
import pytest

pytest.importorskip("mcp")

from plexora.agent import AgentSession, jobs  # noqa: E402
from plexora.mcp.server import build_server  # noqa: E402
from tests.autogate_fixtures import make_gating_project  # noqa: E402


def test_prompts_resources_and_webp_packets(tmp_path):
    from mcp import Client
    from mcp.types import ImageContent

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    server = build_server(AgentSession(), names=["gating"])

    async def go():
        async with Client(server) as c:
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            assert {"gate_image", "gate_dataset", "review_gating", "diagnose_marker"} <= prompts
            prompt = await c.get_prompt("gate_image", {"project": "gsynth"})
            text = prompt.messages[0].content.text
            assert "gsynth" in text and "gating_session_start" in text
            templates = {t.uri_template for t in
                         (await c.list_resource_templates()).resource_templates}
            assert "plexora://gating/session/{session_id}" in templates
            started = await c.call_tool("gating_session_start", {
                "scope": "project", "project": "gsynth", "markers": ["CD4"]})
            session_id = json.loads(started.content[-1].text)["session_id"]
            await anyio.to_thread.run_sync(jobs.drain, 60)
            packet = await c.call_tool("gating_next", {"session_id": session_id})
            images = [part for part in packet.content if isinstance(part, ImageContent)]
            assert images and all(part.mime_type == "image/webp" for part in images)
            body = json.loads(packet.content[-1].text)
            assert body["state"] == "decision"
            resource = await c.read_resource(f"plexora://gating/session/{session_id}")
            status = json.loads(resource.contents[0].text)
            assert status["outstanding_packet"] == body["packet"]["packet_id"]

    anyio.run(go)


def test_the_instructions_name_real_tools_and_every_skill():
    import re

    from plexora.agent import registry
    from plexora.agent.policy import SCOPE_STATES
    from plexora.ai import skills
    from plexora.mcp import SERVER_TOOLS
    from plexora.mcp.server import instructions

    registry.discover()
    text = instructions()
    tools = {cap.tool_name for cap in registry.all_capabilities()} | set(SERVER_TOOLS)
    named = set(re.findall(r"`([a-z_]+)`", text))
    assert named and named <= tools, named - tools
    assert all(s["name"] in text for s in skills.list_skills())
    assert all(state in text for state in SCOPE_STATES)
    assert "{" not in text.replace("{code, message, detail, retryable}", "")


def test_prompts_are_the_manifests():
    from plexora.ai import skills
    from plexora.mcp import prompts

    class Server:
        def __init__(self):
            self.names = []

        def prompt(self, name, title=None, description=None):
            self.names.append(name)
            return lambda fn: fn

    server = Server()
    registered = prompts.register(server, runtime=None)
    wanted = [s["prompt"] for s in skills.manifest()["skills"] if s.get("prompt")]
    assert registered == server.names == wanted
    with pytest.raises(ValueError):
        prompts._mode("sometimes")


def test_the_gating_resources_call_real_capabilities():
    import ast
    import inspect

    from plexora.agent import registry
    from plexora.mcp import resources_gating

    registry.discover()
    tree = ast.parse(inspect.getsource(resources_gating))
    called = [node.args[1].value for node in ast.walk(tree)
              if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_answer"]
    assert called
    for name in called:
        registry.get(name)


def test_the_streaming_wait_defaults_to_the_tools_own():
    from plexora.agent.core.jobs import WaitInput
    from plexora.mcp import tools

    assert tools._default_wait_s() == WaitInput.model_fields["timeout_s"].default
