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

#: These exercise Paid (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.paid


class FakeTab:
    """`state` is what its `get_state` reports; `delays` holds a command's
    acknowledgement back that many seconds (an HD swap rebuilding tiles)."""

    def __init__(self, project="synth", *, state=None, delays=None):
        self.view_id = vs.register(project=project, client="browser")["view_id"]
        self.project = project
        self.state = {"project": project, "channels": [], **(state or {})}
        self.delays = dict(delays or {})
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
                           result=dict(self.state), resulting_revision=3)
                elif command["type"] in self.delays:
                    threading.Timer(self.delays[command["type"]], vs.ack, args=(
                        self.view_id, command["command_id"]), kwargs={
                        "status": "done", "result": {"applied": command["type"]},
                        "resulting_revision": 4}).start()
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


def test_a_mirrored_gating_session_shows_the_tab_what_the_agent_sees(served, tmp_path):
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD8", "CD20", "CD4"))
    session = AgentSession()
    with FakeTab(project="gsynth") as tab:
        started = invoke(session, "gating_session_start", {
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
            "mirror_delay_ms": 0}, link=served)
        assert started["ok"], started
        jobs.drain(60)
        answer = invoke(session, "gating_next", {"session_id": started["result"]["session_id"]},
                        link=served)
        assert answer["ok"], answer
        packet = answer["result"]["packet"]
        types = [c["type"] for c in tab.seen]
        for expected in ("set_hd_mode", "set_channels", "set_active_marker", "preview_gate",
                         "fit_region", "highlight_cells", "show_evidence"):
            assert expected in types, types
        channels = next(c for c in tab.seen if c["type"] == "set_channels")["arguments"]
        assert channels["persist"] is False
        colours = {c["name"]: c["color"] for c in channels["channels"]}
        assert colours == {"DNA": "#4f6fae", "CD4": "#ffd60a"}
        preview = next(c for c in tab.seen if c["type"] == "preview_gate")["arguments"]
        assert preview["low"] == packet["evidence"]["candidate"]["low"]
        assert preview["persist"] is False
        highlighted = next(c for c in tab.seen if c["type"] == "highlight_cells")["arguments"]
        assert highlighted["cells"] and all("x" in c and "y" in c for c in highlighted["cells"])
        status = invoke(session, "gating_session_status",
                        {"session_id": started["result"]["session_id"]})["result"]
        assert status["mirror"]["status"] == "ok", status["mirror"]
        # The agent is told too, and a packet fetched again is not re-sent.
        assert packet["mirror"]["status"] == "ok" and packet["mirror"]["sent"] >= 6
        assert types.index("get_state") < types.index("set_hd_mode")
        before = len(tab.seen)
        again = invoke(session, "gating_next", {"session_id": started["result"]["session_id"]},
                       link=served)["result"]["packet"]
        assert again["mirror"]["resent"] is False and len(tab.seen) == before


def test_the_mirror_does_not_repeat_what_the_tab_already_shows(served, tmp_path):
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    shown = {"hd_mode": True, "cell_mode": "outlines", "tools_open": ["gating"]}
    with FakeTab(project="gsynth", state=shown) as tab:
        started = invoke(session, "gating_session_start", {
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
            "mirror_delay_ms": 0}, link=served)["result"]
        jobs.drain(60)
        packet = invoke(session, "gating_next", {"session_id": started["session_id"]},
                        link=served)["result"]["packet"]
        types = [c["type"] for c in tab.seen]
        assert "set_hd_mode" not in types and "open_tool" not in types
        assert "set_cell_render_mode" not in types and "open_project" not in types
        assert packet["mirror"]["status"] == "ok"


def test_a_slow_hd_swap_is_waited_for(served, tmp_path, monkeypatch):
    """The HD swap rebuilds every tile: it gets its own timeout, while an
    ordinary command that slow still marks the mirror degraded."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.autogate import mirror_script
    from tests.autogate_fixtures import make_gating_project

    monkeypatch.setattr(mirror_script, "DEFAULT_TIMEOUT_S", 0.5)
    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    for slow, expected in (("set_hd_mode", "ok"), ("set_channels", "degraded")):
        with FakeTab(project="gsynth", delays={slow: 1.0}):
            started = invoke(session, "gating_session_start", {
                "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
                "mirror_delay_ms": 0}, link=served)["result"]
            jobs.drain(60)
            packet = invoke(session, "gating_next", {"session_id": started["session_id"]},
                            link=served)["result"]["packet"]
            assert packet["mirror"]["status"] == expected, (slow, packet["mirror"])
            invoke(session, "gating_session_finish", {"session_id": started["session_id"],
                                                      "action": "cancel"})
        vs._reset_for_tests()


def test_a_viewer_that_predates_agent_control_says_restart(tmp_path):
    """The live run's viewer had been up since before /agent/v1 existed: it
    answered /health and nothing else, and the agent simply found no views."""
    from flask import Flask
    from werkzeug.serving import make_server

    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    registry.discover(["gating"])
    old = Flask("old")
    old.add_url_rule("/health", "health", lambda: {"status": "ok"})
    server = make_server("127.0.0.1", 0, old, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        link = ServerLink(f"http://127.0.0.1:{server.server_port}/")
        assert link.control_plane is False and link.describe()["control_plane"] is False
        session = AgentSession()
        listed = invoke(session, "list_viewers", {}, link=link)["result"]
        assert listed["control_plane"] is False and "restart" in listed["hint"]
        refused = invoke(session, "viewer_set_channels", {"channels": [{"name": "CD4"}]},
                         link=link)
        assert refused["error"]["code"] == "capability_unavailable"
        assert "restart" in refused["error"]["message"]
        started = invoke(session, "gating_session_start", {
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True},
            link=link)["result"]
        assert started["mirror"]["status"] == "off"
        assert started["mirror"]["last_error"]["code"] == "capability_unavailable"
    finally:
        server.shutdown()


def test_a_mirror_with_no_viewer_turns_itself_off_and_the_session_goes_on(served, tmp_path):
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    started = invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True},
        link=served)
    jobs.drain(60)
    sid = started["result"]["session_id"]
    answer = invoke(session, "gating_next", {"session_id": sid}, link=served)
    assert answer["ok"] and answer["result"]["state"] == "decision"
    status = invoke(session, "gating_session_status", {"session_id": sid})["result"]
    assert status["mirror"]["status"] == "off"
    assert started["result"]["mirror"]["last_error"]["code"] == "viewer_not_available"
    assert answer["result"]["packet"]["mirror"]["status"] == "off"
