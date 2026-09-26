"""`plexora mcp smoke`: prove the server works against this data root.

Read-only, and through the real MCP protocol (an in-process client against the
built server): list the tools, list the projects, inspect the first one and ask
where its resources are. What an agent would do first, so a pass here means an
agent's first minute will work.
"""

from __future__ import annotations

import json


async def _run(server, project=None, out=print):
    from mcp import Client

    ok = True
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        out(f"tools: {len(tools)} ({', '.join(sorted(t.name for t in tools)[:8])}, ...)")
        listed = await client.call_tool("list_projects", {"limit": 50})
        if listed.is_error:
            out(f"list_projects FAILED: {listed.content[0].text}")
            return False
        projects = json.loads(listed.content[0].text)["projects"]
        out(f"projects: {len(projects)}")
        name = project or (projects[0]["name"] if projects else None)
        if name is None:
            out("no projects registered; nothing more to check")
            return True
        for tool, arguments in (("inspect_project", {"project": name, "include_table": False}),
                                ("get_resource_status", {"project": name})):
            answer = await client.call_tool(tool, arguments)
            if answer.is_error:
                ok = False
                out(f"{tool}({name}) FAILED: {answer.content[0].text[:300]}")
            else:
                out(f"{tool}({name}): ok ({len(answer.content[0].text)} chars)")
    return ok


def run(project=None, names=None) -> int:
    import contextlib
    import sys

    import anyio

    from plexora.mcp.server import build_server

    with contextlib.redirect_stdout(sys.stderr):
        server = build_server(names=names)
    return 0 if anyio.run(_run, server, project) else 1
