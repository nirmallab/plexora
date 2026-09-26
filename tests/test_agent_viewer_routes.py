"""/agent/v1 over the Flask test client: the tab's side and the agent's side."""

import threading

import pytest

import plexora
from plexora.server.models import viewer_sessions as vs


@pytest.fixture
def client():
    vs._reset_for_tests()
    yield plexora.app.test_client()
    vs._reset_for_tests()


def _register(client, project="synth"):
    answer = client.post("/agent/v1/viewer/sessions", json={"project": project,
                                                            "client": "browser"})
    assert answer.status_code == 200
    return answer.get_json()["session"]["view_id"]


def test_register_enqueue_poll_ack(client):
    sid = _register(client)
    listed = client.get("/agent/v1/viewer/sessions").get_json()["sessions"]
    assert [s["view_id"] for s in listed] == [sid]
    queued = client.post(f"/agent/v1/viewer/sessions/{sid}/commands",
                         json={"type": "get_state", "wait_s": 0})
    assert queued.status_code == 202
    command_id = queued.get_json()["command"]["command_id"]
    polled = client.get(f"/agent/v1/viewer/sessions/{sid}/commands?after=0&wait=0").get_json()
    assert polled["commands"][0]["command_id"] == command_id
    acked = client.post(f"/agent/v1/viewer/sessions/{sid}/acks",
                        json={"command_id": command_id, "status": "done",
                              "result": {"project": "synth"}})
    assert acked.status_code == 200
    status = client.get(f"/agent/v1/viewer/commands/{command_id}").get_json()["command"]
    assert status["status"] == "done"


def test_a_waiting_sender_gets_the_ack(client):
    sid = _register(client)

    def tab():
        work = vs.wait_for_work(sid, wait_s=5)
        vs.ack(sid, work["commands"][0]["command_id"], status="done", result={"ok": 1})

    thread = threading.Thread(target=tab)
    thread.start()
    answer = client.post(f"/agent/v1/viewer/sessions/{sid}/commands",
                         json={"type": "pan_to", "arguments": {"x": 1}, "wait_s": 5})
    thread.join(5)
    assert answer.status_code == 200
    assert answer.get_json()["command"]["result"] == {"ok": 1}


def test_a_silent_tab_is_a_504(client):
    sid = _register(client)
    answer = client.post(f"/agent/v1/viewer/sessions/{sid}/commands",
                         json={"type": "pan_to", "wait_s": 0.05})
    assert answer.status_code == 504
    assert answer.get_json()["code"] == "viewer_not_responding"


def test_unknown_session_is_404(client):
    assert client.post("/agent/v1/viewer/sessions/nope/commands",
                       json={"type": "x"}).status_code == 404
    assert client.get("/agent/v1/viewer/sessions/nope/commands").status_code == 404


def test_events_reach_the_tab(client):
    sid = _register(client)
    answer = client.post("/agent/v1/events", json={"project": "synth", "plugin": "gating",
                                                   "kind": "gating.set"})
    assert answer.get_json()["delivered"] == 1
    events = client.get(f"/agent/v1/viewer/sessions/{sid}/commands?wait=0").get_json()["events"]
    assert events[0]["kind"] == "gating.set"


def test_captures_are_stored_and_served(client, tmp_path):
    from io import BytesIO

    from PIL import Image

    sid = _register(client)
    buffer = BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, "PNG")
    answer = client.post(f"/agent/v1/viewer/sessions/{sid}/captures?project=synth",
                         data=buffer.getvalue(), content_type="image/png")
    assert answer.status_code == 200
    artifact = answer.get_json()["artifact"]
    assert artifact["kind"] == "viewer_capture"
    served = client.get(f"/agent/v1/captures/{artifact['id']}")
    assert served.status_code == 200 and served.data == buffer.getvalue()
    assert client.post(f"/agent/v1/viewer/sessions/{sid}/captures",
                       data=b"not a png").status_code == 400
