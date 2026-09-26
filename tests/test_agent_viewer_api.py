"""Viewer tools end to end: a fake tab, a real server, the agent's client.

The tab is a thread doing what agentBridge.js does -- poll, run, ack -- so the
Python half of the control plane is tested without a browser. The browser
half has its own probe (tests/js/agent_bridge_probe.mjs).
"""

import threading

import pytest

import plexora
from plexora.agent import AgentSession, invoke, registry
from plexora.agent.attach import ServerLink
from plexora.server.models import viewer_sessions as vs
from tests.agent_fixtures import make_synthetic_project


class FakeTab:
    def __init__(self, project="synth"):
        self.view_id = vs.register(project=project, client="browser")["view_id"]
        self.seen = []
        self._stop = threading.Event()
        self.events = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        after = after_event = 0
        while not self._stop.is_set():
            work = vs.wait_for_work(self.view_id, after=after, after_event=after_event,
                                    wait_s=0.2)
            if work is None:
                return
            for event in work["events"]:
                after_event = max(after_event, event["event_seq"])
                self.events.append(event)
            for command in work["commands"]:
                after = max(after, command["seq"])
                self.seen.append(command)
                if command["type"] == "explode":
                    vs.ack(self.view_id, command["command_id"], status="rejected",
                           error="boom")
                elif command["type"] == "get_state":
                    vs.ack(self.view_id, command["command_id"], status="done",
                           result={"project": "synth", "channels": []}, resulting_revision=3)
                else:
                    vs.ack(self.view_id, command["command_id"], status="done",
                           result={"applied": command["type"]}, resulting_revision=4)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(2)


@pytest.fixture
def served(tmp_path):
    from werkzeug.serving import make_server

    make_synthetic_project(tmp_path)
    registry.discover(["gating"])
    vs._reset_for_tests()
    server = make_server("127.0.0.1", 0, plexora.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ServerLink(f"http://127.0.0.1:{server.server_port}/")
    server.shutdown()
    vs._reset_for_tests()


def test_without_a_server_every_viewer_tool_says_so(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover([])
    session = AgentSession()
    assert invoke(session, "list_viewers", {})["result"]["attached"] is False
    for tool in ("viewer_get_state", "viewer_set_channels", "viewer_capture"):
        arguments = {"channels": [{"name": "CD8"}]} if tool == "viewer_set_channels" else {}
        assert invoke(session, tool, arguments)["error"]["code"] == "viewer_not_available"
    # Headless tools are unaffected.
    assert invoke(session, "inspect_project", {"project": "synth"})["ok"]


def test_remote_commands_reach_the_tab_and_come_back(served):
    session = AgentSession()
    with FakeTab() as tab:
        listed = invoke(session, "list_viewers", {}, link=served)["result"]
        assert [v["view_id"] for v in listed["viewers"]] == [tab.view_id]
        state = invoke(session, "viewer_get_state", {}, link=served)
        assert state["ok"], state
        assert state["result"]["state"]["project"] == "synth"
        changed = invoke(session, "viewer_set_channels",
                         {"channels": [{"name": "CD8", "color": "#ffff00"}]}, link=served)
        assert changed["ok"], changed
        receipt = changed["result"]["receipt"]
        assert receipt["persistent_state"] == "none"
        sent = next(c for c in tab.seen if c["type"] == "set_channels")
        assert sent["arguments"]["persist"] is False
        moved = invoke(session, "viewer_navigate", {"cell_id": 10}, link=served)
        assert moved["ok"], moved
        focus = next(c for c in tab.seen if c["type"] == "focus_cell")
        assert focus["arguments"]["x"] == pytest.approx(96.0)  # row 1, column 1 of the 64 px grid


def test_a_refusal_in_the_tab_is_a_structured_error(served):
    from plexora.agent import viewer

    with FakeTab() as tab:
        control = viewer.RemoteViewerControl(served)
        with pytest.raises(Exception) as caught:
            control.send(tab.view_id, "explode", {})
        assert caught.value.code == "invalid_input"


def test_two_viewers_are_ambiguous(served):
    session = AgentSession()
    with FakeTab(), FakeTab():
        result = invoke(session, "viewer_get_state", {}, link=served)
        assert result["error"]["code"] == "ambiguous_view"
        assert len(result["error"]["detail"]["viewers"]) == 2


def test_a_gate_set_by_the_agent_notifies_the_tab(served):
    session = AgentSession()
    with FakeTab() as tab:
        result = invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                              "low": 900}, link=served,
                        notify=lambda p, plugin, kind, payload:
                        served.notify(p, plugin, kind, payload))
        assert result["result"]["receipt"]["viewer_notified"] is True
        import time

        deadline = time.time() + 2
        while not tab.events and time.time() < deadline:
            time.sleep(0.05)
        assert tab.events[0]["plugin"] == "gating"


def test_in_process_control_needs_no_link(tmp_path, monkeypatch):
    make_synthetic_project(tmp_path)
    registry.discover([])
    vs._reset_for_tests()
    monkeypatch.setitem(plexora.app.config, "PLEXORA_SERVING", True)
    with FakeTab():
        result = invoke(AgentSession(), "viewer_get_state", {})
        assert result["ok"], result
    vs._reset_for_tests()
