"""`/agent/v1/capabilities` and `/agent/v1/jobs`: the bridge's HTTP rung.

Another application (SCIMAP Pro, through spatialbridge) lists Plexora's
capabilities and calls them by tool name over plain HTTP. Every call goes
through `registry.invoke`; what it may do is decided by the request's token,
never by anything in its body:

- no token on loopback (or the server's own token) -> the bridge: `write`
  policy, origin `bridge` -- a Paid capability does not also need `mcp`;
- a `bridge` token -> the same; a `read`/`write`/`admin` token -> an outside
  agent, origin `mcp`;
- source-file writes need an `admin` token AND `--allow-source-writes`.
"""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel

import plexora
from plexora.agent import registry
from plexora.agent.registry import Capability
from plexora.agent.tokens import TokenStore
from plexora.server.routes import agent_routes


class _Empty(BaseModel):
    pass


class _Slow(BaseModel):
    seconds: float = 0.05


def _paid(call, inp):
    from plexora.agent.registry import CALL_ORIGIN

    return {"answered": True, "origin": CALL_ORIGIN.get()}


def _job(call, inp):
    time.sleep(inp.seconds)
    return {"slept": inp.seconds}


@pytest.fixture
def client(monkeypatch):
    registry._reset_for_tests()
    registry.discover(["gating"])
    registry.register(Capability(name="test.paid", owner="test", purpose="A Paid read.",
                                 permission="read", input_model=_Empty, handler=_paid,
                                 entitlement="ai"))
    registry.register(Capability(name="test.job", owner="test", purpose="A short job.",
                                 permission="read", input_model=_Slow, handler=_job,
                                 execution="job"))
    monkeypatch.setattr(agent_routes, "_DISCOVERED", True)
    monkeypatch.setattr(agent_routes, "_CAPABILITY_SESSION", None)
    monkeypatch.delenv("PLEXORA_ALLOW_SOURCE_WRITES", raising=False)
    yield plexora.app.test_client()
    registry._reset_for_tests()


def _bearer(scope):
    secret, _record = TokenStore().create(scope=scope)
    return {"Authorization": f"Bearer {secret}"}


def _tools(answer):
    return {entry["tool_name"] for entry in answer.get_json()["capabilities"]}


def test_listing_is_descriptors_the_protocol_validates(client):
    from spatialbridge.schema import CapabilityDescriptor

    answer = client.get("/agent/v1/capabilities")
    assert answer.status_code == 200
    body = answer.get_json()
    assert body["success"] is True
    for entry in body["capabilities"]:
        descriptor = CapabilityDescriptor.model_validate(entry)
        assert descriptor.id == f"plexora:{entry['id'].split(':', 1)[1]}"
        assert descriptor.provider == "plexora"
    tools = _tools(answer)
    assert {"bridge_info", "bridge_collect", "find_project_for_table", "bind_project",
            "viewer_set_color_by", "set_selection"} <= tools
    gating = next(e for e in body["capabilities"] if e["tool_name"] == "gating_session_start")
    assert gating["roles"] == ["gate.auto"] and "image" in gating["needs"]
    assert gating["entitlement"] == "ai:gating:session"


def test_plain_loopback_never_lists_a_source_write(client):
    tools = _tools(client.get("/agent/v1/capabilities"))
    assert "write_gates_to_source" not in tools
    assert "set_gate" in tools                      # Plexora's own reversible state


def test_a_read_token_lists_reads_only(client):
    answer = client.get("/agent/v1/capabilities", headers=_bearer("read"))
    entries = answer.get_json()["capabilities"]
    assert entries and all(e["permission"] == "read" for e in entries)


def test_source_writes_need_an_admin_token_and_the_flag(client, monkeypatch):
    assert "write_gates_to_source" not in _tools(
        client.get("/agent/v1/capabilities", headers=_bearer("admin")))
    monkeypatch.setenv("PLEXORA_ALLOW_SOURCE_WRITES", "1")
    assert "write_gates_to_source" not in _tools(client.get("/agent/v1/capabilities"))
    assert "write_gates_to_source" not in _tools(
        client.get("/agent/v1/capabilities", headers=_bearer("bridge")))
    assert "write_gates_to_source" in _tools(
        client.get("/agent/v1/capabilities", headers=_bearer("admin")))


def test_a_call_answers_its_result(client):
    answer = client.post("/agent/v1/capabilities/list_projects", json={"arguments": {}})
    assert answer.status_code == 200
    body = answer.get_json()
    assert body["success"] is True and "projects" in body["result"]


def test_an_unknown_tool_is_a_404_in_the_protocols_shape(client):
    answer = client.post("/agent/v1/capabilities/no_such_tool", json={"arguments": {}})
    assert answer.status_code == 404
    error = answer.get_json()["error"]
    assert error["code"] == "capability_unavailable" and error["provider"] == "plexora"
    assert set(error) >= {"code", "message", "hint", "detail", "provider", "retryable"}


def test_a_refusal_keeps_plexoras_own_code(client):
    answer = client.post("/agent/v1/capabilities/set_gate", headers=_bearer("read"),
                         json={"arguments": {"project": "x", "marker": "CD8", "low": 1.0}})
    assert answer.status_code == 403
    error = answer.get_json()["error"]
    assert error["detail"]["plexora_code"] == "permission_required"


def test_a_bad_body_or_token_is_refused(client):
    assert client.post("/agent/v1/capabilities/list_projects",
                       json={"arguments": [1]}).status_code == 400
    bad = client.post("/agent/v1/capabilities/list_projects", json={"arguments": {}},
                      headers={"Authorization": "Bearer plx_nope_nope"})
    assert bad.status_code == 403


def test_origin_bridge_passes_paid_without_the_mcp_add_on(client, license_issuer):
    license_issuer.install()                       # the plain Paid default: ["ai"], no mcp
    loopback = client.post("/agent/v1/capabilities/test_paid", json={"arguments": {}})
    assert loopback.status_code == 200, loopback.get_json()
    assert loopback.get_json()["result"] == {"answered": True, "origin": "bridge"}
    bridged = client.post("/agent/v1/capabilities/test_paid", json={"arguments": {}},
                          headers=_bearer("bridge"))
    assert bridged.status_code == 200
    # An outside agent's token is an outside agent, whatever the transport.
    outside = client.post("/agent/v1/capabilities/test_paid", json={"arguments": {}},
                          headers=_bearer("write"))
    assert outside.status_code == 403
    assert outside.get_json()["error"]["code"] == "license_required"


def test_the_bridge_still_needs_the_capabilitys_own_entitlement(client):
    answer = client.post("/agent/v1/capabilities/test_paid", json={"arguments": {}})
    assert answer.status_code == 403                # Free: no `ai` grant at all
    assert answer.get_json()["error"]["code"] == "license_required"


def test_the_audit_line_records_the_origin(client, tmp_path):
    from plexora.agent.audit import AuditLog

    client.post("/agent/v1/capabilities/set_gate",
                json={"arguments": {"project": "missing", "marker": "CD8", "low": 1.0}})
    line = AuditLog().tail(1)[0]
    assert line["capability"] == "gating.set" and line["origin"] == "bridge"
    assert line["principal"] == "bridge:loopback"


def test_a_job_is_followed_on_the_jobs_route(client):
    started = client.post("/agent/v1/capabilities/test_job", json={"arguments": {}})
    job_id = started.get_json()["result"]["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/agent/v1/jobs/{job_id}").get_json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["slept"] == 0.05
    assert job["plexora_status"] == "done"
    missing = client.get("/agent/v1/jobs/job_nope")
    assert missing.status_code == 404 and missing.get_json()["error"]["code"] == "invalid_input"


def test_another_machine_is_refused_without_a_server_token(client):
    answer = client.get("/agent/v1/capabilities", environ_base={"REMOTE_ADDR": "10.0.0.8"})
    assert answer.status_code == 403


# -- the real client, against a live server ------------------------------------


@pytest.fixture
def live(client):
    from werkzeug.serving import make_server

    from plexora.server.models import server_records

    server = make_server("127.0.0.1", 0, plexora.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_records.announce(server.server_port, None, mode="terminal")
    yield f"http://127.0.0.1:{server.server_port}/"
    server_records.forget()
    server.shutdown()


def test_spatialbridges_http_transport_round_trip(live):
    from spatialbridge.client import PeerClient
    from spatialbridge.errors import BridgeError
    from spatialbridge.transports.http import HttpTransport

    transport = HttpTransport(live, provider="plexora")
    assert transport.health()
    peer = PeerClient("plexora", transport)
    peer.check_protocol()
    tools = {d.tool_name for d in peer.capabilities()}
    assert "find_project_for_table" in tools and "bridge_route" in tools
    assert "projects" in peer.invoke("list_projects", {})
    with pytest.raises(BridgeError) as unknown:
        peer.invoke("no_such_tool", {})
    assert unknown.value.code == "capability_unavailable"
    # A job is started, then waited on through bridge_status, as the client does.
    started = transport.invoke("test_job", {})
    assert peer.wait(started["job_id"], timeout=10)["slept"] == 0.05
    with pytest.raises(BridgeError) as gone:
        peer.invoke("bridge_status", {"job_id": "job_nope"})
    assert gone.value.code == "invalid_input"


def test_the_client_finds_the_server_through_servers_json(live):
    from spatialbridge import client, peers

    found = [p for p in peers.records("plexora") if p.url == live]
    assert found and found[0].protocol == "1.0"
    peer = client.connect("plexora", spawn=False)
    assert peer.rung == "http"
    assert peer.invoke("bridge_info", {})["provider"] == "plexora"
