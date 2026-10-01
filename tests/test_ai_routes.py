"""`/ai/v1` and the `ai.*` capabilities: Plexora AI runs inside the server.

The gateway is `FakeGateway`, reached the way the app reaches the real one
(`PLEXORA_AI_GATEWAY`, `PLEXORA_AI_TOKEN`); the model is the scripted oracle
of each workflow's session tests. What is pinned: the routes are guarded by
the `ai` entitlement and this machine; a run started over HTTP is an
`ai.run_session` job that drives a real session to the end, with receipts in
the audit log; an open tab hears the session's events and the harness's own
(the quote, usage per packet, a pause for credit with what to resume); a
credit pause resumes over HTTP; the balance carries the estimate the panel
shows before a start; control pauses and stops the run's session.
"""

import json

import pytest

import plexora
from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.server.models import viewer_sessions as vs
from tests.ai_harness_fixtures import FakeGateway
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, rois_of

FAST = {"map_cell_um": 25.0}


@pytest.fixture
def client():
    vs._reset_for_tests()
    with plexora.app.test_client() as test_client:
        yield test_client
    vs._reset_for_tests()


@pytest.fixture
def scene(tmp_path):
    registry.discover(["roi", "qc"])
    return make_qc_project(tmp_path, artifacts=("saturation", "fold"))


@pytest.fixture
def gateway(scene, monkeypatch):
    oracle = QCOracle(scene)

    def brain(packet, body):
        return oracle.answer(packet, body["context"]["session_id"])

    with FakeGateway(brain) as fake:
        monkeypatch.setenv("PLEXORA_AI_GATEWAY", fake.url)
        monkeypatch.setenv("PLEXORA_AI_TOKEN", "PLXAI1.test")
        monkeypatch.delenv("PLEXORA_AI_DEV", raising=False)
        yield fake


def _tab(client, project="qcsynth"):
    answer = client.post("/agent/v1/viewer/sessions", json={"project": project, "client": "browser"})
    return answer.get_json()["session"]["view_id"]


def _events(client, view_id):
    polled = client.get(f"/agent/v1/viewer/sessions/{view_id}/commands?after=0&after_event=0&wait=0")
    return [e for e in polled.get_json()["events"] if str(e["kind"]).endswith(".session")]


def _start(client, **body):
    answer = client.post("/ai/v1/runs", json={"kind": "qc", "project": "qcsynth", "start_options": FAST,
                                              **body})
    assert answer.status_code == 201, answer.get_json()
    return answer.get_json()


# -- the guards ----------------------------------------------------------------------------


def test_every_route_needs_the_ai_entitlement(client):
    for method, path in (("post", "/ai/v1/runs"), ("get", "/ai/v1/runs"), ("get", "/ai/v1/runs/air_x"),
                         ("post", "/ai/v1/runs/air_x/control"), ("get", "/ai/v1/balance")):
        answer = getattr(client, method)(path, json={} if method == "post" else None)
        assert answer.status_code == 403, (path, answer.status_code)
        body = answer.get_json()
        assert body["success"] is False and "ai" in json.dumps(body)


@pytest.mark.paid
def test_a_neighbour_on_the_network_is_refused(client):
    answer = client.get("/ai/v1/runs", environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert answer.status_code == 403
    assert "this machine" in answer.get_json()["error"]


def test_the_ai_capabilities_are_registered_paid_and_the_run_is_a_job():
    registry.register_core()
    described = {d["name"]: d for d in registry.describe()}
    for name in ("ai.run_session", "ai.run_status", "ai.run_control", "ai.balance"):
        assert described[name]["entitlement"] == "ai" and described[name]["plan"] == "paid", name
    assert described["ai.run_session"]["execution"] == "job"
    assert described["ai.run_session"]["permission"] == "reversible_write"
    assert described["ai.run_status"]["permission"] == "read"
    refused = invoke(AgentSession(), "ai_run_status", {})
    assert not refused["ok"] and refused["error"]["code"] == "license_required"


@pytest.mark.paid
def test_an_unknown_run_is_invalid_input():
    registry.register_core()
    answer = invoke(AgentSession(), "ai_run_status", {"run_id": "air_000000000000"})
    assert not answer["ok"] and answer["error"]["code"] == "invalid_input"


# -- runs ----------------------------------------------------------------------------------


@pytest.mark.paid
def test_the_balance_carries_an_estimate_for_the_open_project(client, gateway, scene):
    body = client.get("/ai/v1/balance?project=qcsynth&kind=qc").get_json()
    assert body["success"] and body["available_micro"] == gateway.credits
    estimate = body["estimates"]["qc"]
    assert estimate["unit"] == "channel" and estimate["units"] == len(scene["channels"])
    assert estimate["credits"] == 12 * len(scene["channels"]) and estimate["affordable"]
    both = client.get("/ai/v1/balance?project=qcsynth").get_json()["estimates"]
    assert set(both) == {"gating", "qc"} and both["gating"]["unit"] == "marker"
    picked = client.get("/ai/v1/balance?project=qcsynth&kind=qc&channels=DAPI,CD3").get_json()
    assert picked["estimates"]["qc"]["units"] == 2


@pytest.mark.paid
def test_a_run_started_over_http_qcs_the_image_and_the_tab_hears_it(client, gateway, scene):
    view = _tab(client)
    started = _start(client)
    assert started["run_id"] == "air_" + started["job_id"].removeprefix("job_")
    jobs.drain(240)
    run = client.get(f"/ai/v1/runs/{started['run_id']}").get_json()
    assert run["success"] and run["status"] == "done", run
    assert run["kind"] == "qc" and run["session_id"].startswith("qs")
    assert run["usage"]["model_calls"] == len(gateway.calls) > 0
    assert run["usage"]["verdicts"].get("miss", 0) == 0
    assert 0 < run["usage"]["cache_read_share"] <= 1
    assert run["job"]["status"] == "done"
    assert gateway.runs["run_1"]["feature"] == "qc" and gateway.runs["run_1"]["status"] == "finished"
    assert rois_of(AgentSession())
    # The same run by its job id, and in the list.
    assert client.get(f"/ai/v1/runs/{started['job_id']}").get_json()["run_id"] == started["run_id"]
    listed = client.get("/ai/v1/runs").get_json()["runs"]
    assert started["run_id"] in [r["run_id"] for r in listed]

    events = _events(client, view)
    names = [e["payload"]["event"] for e in events]
    assert "started" in names and "issued" in names and "finished" in names       # the session's own
    assert "ai_run" in names and "ai_usage" in names and "ai_finished" in names    # the harness's
    usage = [e["payload"] for e in events if e["payload"]["event"] == "ai_usage"]
    assert usage[-1]["usage"]["packets"] == run["summary"]["packets"]
    assert all(e["kind"] == "qc.session" and e["payload"]["control"]["url"].endswith("/control")
               for e in events)
    quoted = next(e["payload"] for e in events if e["payload"]["event"] == "ai_run")
    assert quoted["quote_credits"] == 12 * len(scene["channels"])

    # Receipts: the run's operation and the session's writes are in the audit.
    from plexora.agent.audit import AuditLog

    lines = [line for line in AuditLog().tail(500) if line.get("capability")]
    capabilities = {line["capability"] for line in lines}
    assert "ai.run_session" in capabilities and "qc.session_start" in capabilities


@pytest.mark.paid
def test_a_credit_pause_tells_the_tab_and_resumes_over_http(client, gateway):
    view = _tab(client)
    original = gateway._messages

    def broke_after_one(handler, body):
        if len(gateway.calls) >= 1 and not getattr(gateway, "topped_up", False):
            return handler._json(402, {"error": {"code": "insufficient_credits", "message": "top up",
                                                 "details": {"top_up_url": "https://example.test/top"}}})
        return original(handler, body)
    gateway._messages = broke_after_one
    started = _start(client)
    jobs.drain(240)
    run = client.get(f"/ai/v1/runs/{started['run_id']}").get_json()
    assert run["status"] == "paused" and run["reason"] == "insufficient_credits", run
    paused = [e["payload"] for e in _events(client, view) if e["payload"]["event"] == "ai_paused"]
    assert paused and paused[0]["reason"] == "insufficient_credits"
    assert paused[0]["top_up_url"] == "https://example.test/top"
    assert paused[0]["resume"] == {"kind": "qc", "project": "qcsynth", "resume_session": run["session_id"]}

    # Control reaches the paused session: still paused, then a stop and back.
    controlled = client.post(f"/ai/v1/runs/{started['run_id']}/control", json={"action": "pause"})
    assert controlled.status_code == 200 and controlled.get_json()["control"]["paused"] is True
    assert client.post(f"/ai/v1/runs/{started['run_id']}/control",
                       json={"action": "jump"}).status_code == 400

    gateway.topped_up = True
    resumed = _start(client, **paused[0]["resume"])
    jobs.drain(240)
    second = client.get(f"/ai/v1/runs/{resumed['run_id']}").get_json()
    assert second["status"] == "done", second
    assert second["session_id"] == run["session_id"] and len(gateway.runs) == 1
    assert gateway.runs["run_1"]["status"] == "finished"


@pytest.mark.paid
def test_stop_through_control_ends_the_session_at_its_next_packet(client, gateway):
    original = gateway._messages
    state = {"stopped": False}

    def stop_on_second(handler, body):
        if len(gateway.calls) == 1 and not state["stopped"]:
            state["stopped"] = True
            other = plexora.app.test_client()        # the user, from another thread
            runs = other.get("/ai/v1/runs").get_json()["runs"]
            answer = other.post(f"/ai/v1/runs/{runs[0]['run_id']}/control", json={"action": "stop"})
            assert answer.status_code == 200, answer.get_json()
            assert answer.get_json()["control"]["stopped"] is True
        return original(handler, body)
    gateway._messages = stop_on_second
    started = _start(client)
    jobs.drain(240)
    run = client.get(f"/ai/v1/runs/{started['run_id']}").get_json()
    assert state["stopped"] and run["status"] == "stopped", run
    assert len(gateway.calls) == 2, [c["packet_id"] for c in gateway.calls]


@pytest.mark.paid
def test_a_gating_run_over_http_gates_every_marker(client, tmp_path, monkeypatch):
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import Oracle

    registry.discover(["gating"])
    info = make_gating_project(tmp_path, markers=("CD3", "CD8"))
    oracle = Oracle(info)
    with FakeGateway(lambda packet, body: oracle.answer(packet)) as fake:
        monkeypatch.setenv("PLEXORA_AI_GATEWAY", fake.url)
        monkeypatch.setenv("PLEXORA_AI_TOKEN", "PLXAI1.test")
        estimate = client.get("/ai/v1/balance?project=gsynth&kind=gating").get_json()["estimates"]
        units = estimate["gating"]["units"]
        assert units >= 2 and estimate["gating"]["credits"] == 25 * units
        answer = client.post("/ai/v1/runs", json={"kind": "gating", "datasource": "gsynth"})
        assert answer.status_code == 201, answer.get_json()
        jobs.drain(240)
        run = client.get(f"/ai/v1/runs/{answer.get_json()['run_id']}").get_json()
    assert run["status"] == "done", run
    assert fake.runs["run_1"]["feature"] == "gating", fake.runs
    assert fake.runs["run_1"]["units"] == units, (units, fake.runs)
    status = invoke(AgentSession(), "gating_session_status", {"session_id": run["session_id"]})
    states = {u["marker"]: u["state"] for u in status["result"]["units"]}
    assert all(s in ("accepted", "accepted_low_confidence") for s in states.values()), states
