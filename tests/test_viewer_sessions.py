"""The viewer session registry: register, poll, ack, events, expiry."""

import threading
import time

import pytest

from plexora.server.models import viewer_sessions as vs


@pytest.fixture(autouse=True)
def clean():
    vs._reset_for_tests()
    yield
    vs._reset_for_tests()


def test_register_is_idempotent_and_keeps_queued_commands():
    first = vs.register(project="a")
    sid = first["view_id"]
    vs.enqueue(sid, "pan_to", {"x": 1, "y": 2})
    again = vs.register(session_id=sid, project="b")
    assert again["view_id"] == sid and again["project"] == "b"
    work = vs.wait_for_work(sid)
    assert [c["type"] for c in work["commands"]] == ["pan_to"]


def test_a_command_round_trip():
    sid = vs.register(project="a")["view_id"]
    command = vs.enqueue(sid, "get_state")
    work = vs.wait_for_work(sid, after=0)
    assert work["commands"][0]["command_id"] == command.command_id
    assert work["attached"] is True  # sending attaches
    # Delivered once: a later poll after that seq sees nothing.
    assert vs.wait_for_work(sid, after=work["commands"][0]["seq"])["commands"] == []
    vs.ack(sid, command.command_id, status="done", result={"project": "a"},
           resulting_revision=7)
    answered = vs.wait_for_ack(command.command_id, 0.1)
    assert answered["status"] == "done" and answered["result"] == {"project": "a"}
    assert vs.describe(sid)["revision"] == 7
    assert vs.describe(sid)["last_state"] == {"project": "a"}


def test_a_held_poll_wakes_on_enqueue():
    sid = vs.register(project="a")["view_id"]
    got = {}

    def poll():
        got["work"] = vs.wait_for_work(sid, wait_s=5)

    thread = threading.Thread(target=poll)
    started = time.time()
    thread.start()
    time.sleep(0.1)
    vs.enqueue(sid, "pan_to")
    thread.join(3)
    assert got["work"]["held"] is True
    assert got["work"]["commands"][0]["type"] == "pan_to"
    assert time.time() - started < 3


def test_the_held_budget_is_respected(monkeypatch):
    monkeypatch.setattr(vs, "MAX_HELD_REQUESTS", 0)
    sid = vs.register(project="a")["view_id"]
    work = vs.wait_for_work(sid, wait_s=5)
    assert work["held"] is False and work["budget_exhausted"] is True


def test_ack_timeout_reports_pending():
    sid = vs.register(project="a")["view_id"]
    command = vs.enqueue(sid, "pan_to")
    assert vs.wait_for_ack(command.command_id, 0.05)["status"] == "pending"


def test_events_fan_out_by_project():
    a = vs.register(project="a")["view_id"]
    b = vs.register(project="b")["view_id"]
    assert vs.publish("a", "gating", "gating.set", {"x": 1}) == 1
    assert vs.wait_for_work(a)["events"][0]["plugin"] == "gating"
    assert vs.wait_for_work(b)["events"] == []
    assert vs.publish(None, "core", "reload") == 2


def test_expiry_and_leave(monkeypatch):
    sid = vs.register(project="a")["view_id"]
    command = vs.enqueue(sid, "pan_to")
    vs.leave(sid, navigating_to="b")
    assert vs.describe(sid)["status"] == "navigating"
    vs.leave(sid)
    assert vs.describe(sid) is None
    assert vs.command(command.command_id)["status"] == "expired"
    other = vs.register(project="a")["view_id"]
    vs._SESSIONS[other].last_seen -= vs.SESSION_TTL_S + 1
    assert vs.list_sessions() == []


def test_unknown_session_is_none():
    assert vs.wait_for_work("nope") is None
    assert vs.enqueue("nope", "x") is None
