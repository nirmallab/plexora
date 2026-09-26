"""`plexora mcp serve` as a client launches it: a subprocess over stdio.

The in-process tests prove the tools; this proves the process -- that it
starts, that nothing it prints while loading plugins lands in the protocol
stream, and that a tool call makes it across and back.
"""

import json
import os
import sys
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp")

from tests.agent_fixtures import make_synthetic_project  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def test_a_stdio_round_trip(tmp_path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    make_synthetic_project(tmp_path)
    env = dict(os.environ, PLEXORA_DATA_PATH=str(tmp_path),
               PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "plexora", "mcp", "serve", "--no-attach", "--plugins", "gating"],
        env=env, cwd=str(REPO))

    async def go():
        with anyio.fail_after(180):
            async with Client(params, read_timeout_seconds=120) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                answer = await client.call_tool("get_gate",
                                                {"project": "synth", "marker": "CD8"})
                return tools, answer

    tools, answer = anyio.run(go)
    assert "get_gate" in tools and "create_roi" not in tools
    assert not answer.is_error, answer.content[0].text
    assert json.loads(answer.content[0].text)["gate"]["marker"] == "CD8"
