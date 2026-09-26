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
