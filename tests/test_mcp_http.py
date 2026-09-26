"""The MCP server over streamable HTTP, in-process: tokens, scopes, host checks."""

import json
import socket
import subprocess
import sys
import time

import anyio
import pytest

pytest.importorskip("mcp")
httpx2 = pytest.importorskip("httpx2")

from plexora.agent import AgentSession, registry  # noqa: E402
from plexora.agent.tokens import TokenStore  # noqa: E402
from plexora.mcp import server as mcp_server  # noqa: E402
from plexora.mcp.auth import PlexoraTokenVerifier  # noqa: E402
from tests.agent_fixtures import make_synthetic_project  # noqa: E402

@pytest.fixture
def served(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating"])
    store = TokenStore()
    tokens = {scope: store.create(scope=scope, label=scope)[0]
              for scope in ("read", "write", "admin")}
    server = mcp_server.build_server(AgentSession(), names=["gating"],
                                     token_verifier=PlexoraTokenVerifier(store),
                                     transport="http")
    return server, tokens


def _json(result):
    return json.loads(result.content[-1].text)


def _session(server, token, fn, host="127.0.0.1:8321"):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        app = server.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=mcp_server.http_security("127.0.0.1"))
        async with app.router.lifespan_context(app):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          base_url=f"http://{host}",
                                          headers=headers) as http:
                async with Client(streamable_http_client(f"http://{host}/mcp",
                                                         http_client=http)) as client:
                    return await fn(client)

    return anyio.run(go)


def _raw(server, token=None, host="127.0.0.1:8321", path="/mcp"):
    async def go():
        app = server.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=mcp_server.http_security("127.0.0.1"))
        async with app.router.lifespan_context(app):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            headers.update({"Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json", "Host": host})
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          base_url=f"http://{host}") as http:
                if path == "/health":
                    return await http.get(path, headers={"Host": host})
                return await http.post(path, headers=headers, content=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}}))

    return anyio.run(go)


def test_a_valid_token_lists_tools_and_reads(served):
    server, tokens = served

    async def fn(client):
        tools = {t.name for t in (await client.list_tools()).tools}
        gate = await client.call_tool("get_gate", {"project": "synth", "marker": "CD8"})
        info = await client.call_tool("server_info", {})
        return tools, gate, info

    tools, gate, info = _session(server, tokens["read"], fn)
    assert "set_gate" in tools and not gate.is_error
    info = _json(info)
    assert info["transport"] == "http" and info["auth"] == "bearer"
    assert info["policy"]["allow_writes"] is False
    assert info["policy"]["principal"].startswith("token:")


def test_no_token_or_a_bad_one_is_401(served):
    server, tokens = served
    assert _raw(server).status_code == 401
    assert _raw(server, token="plx_bad_token").status_code == 401
    assert _raw(server, token=tokens["read"]).status_code == 200


def test_a_read_token_cannot_write_and_an_admin_one_can(served, tmp_path):
    server, tokens = served

    async def write(client):
        return await client.call_tool("set_gate", {"project": "synth", "marker": "CD8",
                                                   "low": 900})

    refused = _session(server, tokens["read"], write)
    assert refused.is_error and "permission_required" in refused.content[0].text
    allowed = _session(server, tokens["admin"], write)
    assert not allowed.is_error
    lines = [json.loads(line) for line in
             (tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()]
    admin_id = TokenStore().verify(tokens["admin"])["id"]
    assert lines[-1]["status"] == "ok" and lines[-1]["principal"] == f"token:{admin_id}"
    assert lines[-2]["status"] == "refused"


def test_health_needs_no_token(served):
    server, _ = served
    response = _raw(server, path="/health")
    assert response.status_code == 200 and response.json()["ok"] is True


def test_a_rebinding_host_is_refused(served):
    server, tokens = served
    assert _raw(server, token=tokens["read"], host="evil.example:8321").status_code in (
        400, 403, 421)


def test_serve_refuses_an_open_server(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run",
                        lambda self, *a, **k: calls.append((a, k)))
    with pytest.raises(SystemExit, match="token"):
        mcp_server.serve(transport="http", names=[])
    with pytest.raises(SystemExit, match="loopback"):
        mcp_server.serve(transport="http", host="0.0.0.0", require_auth=False, names=[])
    mcp_server.serve(transport="http", require_auth=False, names=[])
    assert calls[-1][0] == ("streamable-http",)
    assert calls[-1][1]["host"] == "127.0.0.1" and calls[-1][1]["port"] == 8321
    TokenStore().create(scope="read")
    mcp_server.serve(transport="http", host="0.0.0.0", port=9001, names=[],
                     allowed_hosts=("proxy.example",))
    security = calls[-1][1]["transport_security"]
    assert "proxy.example:*" in security.allowed_hosts and "0.0.0.0:*" in security.allowed_hosts


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_real_process_round_trip(tmp_path):
    """The HTTP twin of test_mcp_stdio: a subprocess, a token, one call."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    make_synthetic_project(tmp_path)
    secret, _ = TokenStore().create(scope="read")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "plexora", "mcp", "serve", "--transport", "http",
         "--port", str(port), "--no-attach", "--plugins", "gating",
         "--data-dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx2.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(proc.stderr.read().decode()[-2000:])
            time.sleep(0.3)

        async def go():
            async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {secret}"}) as http:
                async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp",
                                                         http_client=http)) as client:
                    return await client.call_tool("inspect_project", {"project": "synth"})

        result = anyio.run(go)
        assert _json(result)["table"]["n_cells"] == 64
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
