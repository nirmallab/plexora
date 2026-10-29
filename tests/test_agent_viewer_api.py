"""Viewer tools end to end: a fake tab, a real server, the agent's client.

The tab is a thread doing what agentBridge.js does -- poll, run, ack -- so the
Python half of the control plane is tested without a browser. The browser
half has its own probe (tests/js/agent_bridge_probe.mjs).
"""

import json
import threading

import pytest

import plexora
from plexora.agent import AgentSession, invoke, registry
from plexora.agent.attach import ServerLink
from plexora.agent.sessions import mirror as session_mirror
from plexora.server.models import viewer_sessions as vs
from tests.agent_fixtures import make_synthetic_project

#: These exercise Pro (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.pro


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
        started = invoke(session, "gating_session_start", {"engine": "v1", 
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
            "mirror_delay_ms": 0, "display_check": "rule"}, link=served)
        assert started["ok"], started
        jobs.drain(60)
        answer = invoke(session, "gating_next", {"session_id": started["result"]["session_id"]},
                        link=served)
        assert answer["ok"], answer
        packet = answer["result"]["packet"]
        # Off the session's path: the packet comes back at once, the tab is
        # shown it by the session's presenter.
        assert packet["mirror"]["status"] == "queued", packet["mirror"]
        assert session_mirror.wait_idle(started["result"]["session_id"])
        types = [c["type"] for c in tab.seen]
        for expected in ("set_hd_mode", "set_channels", "set_active_marker", "preview_gate",
                         "highlight_cells", "show_evidence"):
            assert expected in types, types
        channels = next(c for c in tab.seen if c["type"] == "set_channels")["arguments"]
        assert channels["persist"] is False
        colours = {c["name"]: c["color"] for c in channels["channels"]}
        assert colours == {"DNA": "#4f6fae", "CD4": "#ffd60a"}
        preview = next(c for c in tab.seen if c["type"] == "preview_gate")["arguments"]
        assert preview["low"] == packet["evidence"]["candidate"]["low"]
        assert preview["persist"] is False
        # The tab flies there itself, a look played at a person's pace.
        assert preview["show"]["field"]["width"] > 0 and preview["show"]["cells"]
        highlighted = next(c for c in tab.seen if c["type"] == "highlight_cells")["arguments"]
        assert highlighted["cells"] and all("x" in c and "y" in c for c in highlighted["cells"])
        status = invoke(session, "gating_session_status",
                        {"session_id": started["result"]["session_id"]})["result"]
        assert status["mirror"]["status"] == "ok", status["mirror"]
        # The session records what was sent, and a packet fetched again is not re-sent.
        assert status["mirror"]["sent"] >= 6
        assert types.index("get_state") < types.index("set_hd_mode")
        before = len(tab.seen)
        again = invoke(session, "gating_next", {"session_id": started["result"]["session_id"]},
                       link=served)["result"]["packet"]
        assert session_mirror.wait_idle(started["result"]["session_id"])
        assert again["mirror"]["resent"] is False and len(tab.seen) == before


def test_a_detached_session_leaves_the_tab_be_and_attach_replays_the_packet(served, tmp_path,
                                                                            monkeypatch):
    """Continue in background: packets go on without a command reaching the
    tab. Watch in viewer (the tab's own POST): the packet out is shown again
    at once, not at the next one."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.autogate import engine
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    with FakeTab(project="gsynth") as tab:
        started = invoke(session, "gating_session_start", {"engine": "v1", 
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
            "mirror_delay_ms": 0, "display_check": "rule"}, link=served)["result"]
        sid = started["session_id"]
        jobs.drain(60)
        engine.store().set_control(sid, viewer_detached=True)
        before = len(tab.seen)
        packet = invoke(session, "gating_next", {"session_id": sid},
                        link=served)["result"]["packet"]
        assert len(tab.seen) == before
        assert packet["mirror"]["detached"] is True

        monkeypatch.setitem(plexora.app.config, "PLEXORA_SERVING", True)
        answer = plexora.app.test_client().post(
            f"/plugins/gating/agent_session/{sid}/control",
            data=json.dumps({"action": "attach_viewer", "view_id": tab.view_id}))
        assert answer.status_code == 200
        import time

        deadline = time.time() + 5
        while not any(c["type"] == "preview_gate" for c in tab.seen[before:]) \
                and time.time() < deadline:
            time.sleep(0.05)
        assert any(c["type"] == "preview_gate" for c in tab.seen[before:])
        deadline = time.time() + 5
        status = {}
        while time.time() < deadline:
            status = invoke(session, "gating_session_status", {"session_id": sid})["result"]
            if (status.get("mirror") or {}).get("status") == "ok":
                break
            time.sleep(0.05)
        assert status["mirror"]["status"] == "ok", status["mirror"]
        assert status["control"]["viewer_detached"] is False


def test_the_mirror_does_not_repeat_what_the_tab_already_shows(served, tmp_path):
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    shown = {"hd_mode": True, "cell_mode": "outlines", "tools_open": ["gating"]}
    with FakeTab(project="gsynth", state=shown) as tab:
        started = invoke(session, "gating_session_start", {"engine": "v1", 
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
            "mirror_delay_ms": 0, "display_check": "rule"}, link=served)["result"]
        jobs.drain(60)
        invoke(session, "gating_next", {"session_id": started["session_id"]}, link=served)
        assert session_mirror.wait_idle(started["session_id"])
        types = [c["type"] for c in tab.seen]
        assert "set_hd_mode" not in types and "open_tool" not in types
        assert "set_cell_render_mode" not in types and "open_project" not in types
        status = invoke(session, "gating_session_status",
                        {"session_id": started["session_id"]})["result"]
        assert status["mirror"]["status"] == "ok"


def test_a_slow_hd_swap_is_waited_for(served, tmp_path, monkeypatch):
    """The HD swap rebuilds every tile: it gets its own timeout (as does a
    channel change, which may wait for a contrast check's animation to
    finish), while an ordinary command that slow still marks the mirror
    degraded."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.autogate import mirror_script
    from tests.autogate_fixtures import make_gating_project

    monkeypatch.setattr(mirror_script, "DEFAULT_TIMEOUT_S", 0.5)
    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    for slow, expected in (("set_hd_mode", "ok"), ("set_channels", "ok"),
                           ("set_active_marker", "degraded")):
        with FakeTab(project="gsynth", delays={slow: 1.0}):
            started = invoke(session, "gating_session_start", {"engine": "v1", 
                "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True,
                "mirror_delay_ms": 0, "display_check": "rule"}, link=served)["result"]
            jobs.drain(60)
            invoke(session, "gating_next", {"session_id": started["session_id"]}, link=served)
            assert session_mirror.wait_idle(started["session_id"], 30)
            mirror = invoke(session, "gating_session_status",
                            {"session_id": started["session_id"]})["result"]["mirror"]
            assert mirror["status"] == expected, (slow, mirror)
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
        started = invoke(session, "gating_session_start", {"engine": "v1", 
            "scope": "project", "project": "gsynth", "markers": ["CD4"], "mirror": True},
            link=link)["result"]
        assert started["mirror"]["status"] == "off"
        assert started["mirror"]["last_error"]["code"] == "capability_unavailable"
    finally:
        server.shutdown()


def test_a_contrast_check_is_shown_settling_marker_by_marker(served, tmp_path):
    """The contrast check's answer is shown in the tab channel by channel
    (`animate_contrast`, one command), sent right after the answer and so
    before the next packet's own channels -- the next question goes to the
    model while the tab plays it, and the tab holds the look's commands until
    it is done."""
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    with FakeTab(project="gsynth", state={"hd_mode": True}) as tab:
        started = invoke(session, "gating_session_start", {"engine": "v1", 
            "scope": "project", "project": "gsynth", "markers": ["CD3", "CD4"], "mirror": True,
            "mirror_delay_ms": 0}, link=served)["result"]
        sid = started["session_id"]
        jobs.drain(60)
        packet = invoke(session, "gating_next", {"session_id": sid}, link=served)["result"]["packet"]
        while packet["kind"] != "display_setup":
            assert packet["kind"] == "panel_context", packet["kind"]
            packet = invoke(session, "gating_answer", {
                "session_id": sid, "packet_id": packet["packet_id"],
                "answer": {"kind": "panel_context", "entries": []}},
                link=served)["result"]["next"]["packet"]
        rows = [r["marker"] for r in packet["evidence"]["rows"]]
        before = len(tab.seen)
        following = invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": {"kind": "display_setup", "background_ok": {m: ["c3"] for m in rows},
                       "structure_ok": {m: ["c3"] for m in rows}}}, link=served)
        assert following["ok"], following
        assert session_mirror.wait_idle(sid)
        types = [c["type"] for c in tab.seen[before:]]
        assert "animate_contrast" in types, types
        animate = next(c for c in tab.seen[before:] if c["type"] == "animate_contrast")
        shown = animate["arguments"]["channels"]
        # The nuclear stain first, in its own slot; then the markers judged.
        assert shown[0]["name"] == "DNA" and shown[0]["slot"] == 0
        assert {c["name"] for c in shown[1:]} == set(rows)
        assert all(c["curve"]["kind"] in ("asinh", "linear") for c in shown)
        assert animate["arguments"]["slot"] == 1 and animate["arguments"]["pace"] == "natural"
        if "set_channels" in types:
            assert types.index("animate_contrast") < types.index("set_channels")


def test_a_mirror_with_no_viewer_turns_itself_off_and_the_session_goes_on(served, tmp_path):
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    session = AgentSession()
    started = invoke(session, "gating_session_start", {"engine": "v1", 
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


def test_the_3d_view_is_cut_in_image_units(served):
    """viewer_set_inspect: a crop, a cutaway, the cut faces reach the tab as
    the browser's cut (services/inspectGeometry.js) takes them -- pixels
    across x and y, planes along z -- and the browser's answer comes back."""
    session = AgentSession()
    with FakeTab() as tab:
        cut = invoke(session, "viewer_set_inspect",
                     {"mode": "crop", "axes": ["x", "z"], "crop": {"x": [100, 600], "z": [4, 9]}},
                     link=served)
        assert cut["ok"], cut
        sent = next(c for c in tab.seen if c["type"] == "set_inspect")
        assert sent["arguments"]["mode"] == "crop" and sent["arguments"]["axes"] == ["x", "z"]
        assert sent["arguments"]["crop"] == {"x": [100.0, 600.0], "z": [4.0, 9.0]}
        assert cut["result"]["receipt"]["persistent_state"] == "none"
        faced = invoke(session, "viewer_set_inspect", {"face": True}, link=served)
        assert faced["ok"], faced
        sent = [c for c in tab.seen if c["type"] == "set_inspect"][-1]["arguments"]
        assert sent == {"face": True}
        whole = invoke(session, "viewer_set_inspect", {"mode": "full"}, link=served)
        assert whole["ok"], whole
        assert [c for c in tab.seen if c["type"] == "set_inspect"][-1]["arguments"] == {"mode": "full"}
        opened = invoke(session, "viewer_set_inspect",
                        {"mode": "cutaway", "axes": ["y"], "cutaway": {"y": {"at": 300, "keep": "lo"}}},
                        link=served)
        assert opened["ok"], opened
        sent = [c for c in tab.seen if c["type"] == "set_inspect"][-1]["arguments"]
        assert sent["axes"] == ["y"] and sent["cutaway"] == {"y": {"at": 300.0, "keep": "lo"}}


def test_a_cut_that_cannot_be_is_refused_before_the_tab(served):
    session = AgentSession()
    with FakeTab() as tab:
        backwards = invoke(session, "viewer_set_inspect", {"crop": {"x": [600, 100]}}, link=served)
        assert backwards["error"]["code"] == "invalid_input"
        assert invoke(session, "viewer_set_inspect", {}, link=served)["error"]["code"] == "invalid_input"
        wedge = invoke(session, "viewer_set_inspect", {"mode": "wedge"}, link=served)
        assert wedge["error"]["code"] == "invalid_input"
        # The section and slab cuts are gone, and so is everything only they took.
        for mode in ("section", "slab"):
            gone = invoke(session, "viewer_set_inspect", {"mode": mode, "axes": ["z"]}, link=served)
            assert gone["error"]["code"] == "invalid_input", mode
        for argument in ({"thickness": {"z": 4}}, {"composite": "mean"}, {"opacity": 0.5},
                         {"context": 0.2}, {"z": 3}, {"section_x": 10}, {"section_y": 10}):
            refused = invoke(session, "viewer_set_inspect", {"mode": "crop", **argument}, link=served)
            assert refused["error"]["code"] == "invalid_input", argument
        # A cutaway is along one axis; a crop along any.
        corner = invoke(session, "viewer_set_inspect", {"mode": "cutaway", "axes": ["x", "y"]}, link=served)
        assert corner["error"]["code"] == "invalid_input"
        assert "one axis" in corner["error"]["message"]
        assert not any(c["type"] == "set_inspect" for c in tab.seen)


def test_the_3d_view_can_draw_one_area(served):
    """`viewer_set_volume_mode` with an area draws that part of the image
    (the Area tool's square): forwarded as image pixels; `clear_area` goes back
    to the whole stack; an area of nothing is refused before the tab."""
    session = AgentSession()
    with FakeTab() as tab:
        placed = invoke(session, "viewer_set_volume_mode",
                        {"mode": "volume", "area": {"x": 100, "y": 50, "width": 300, "height": 300}},
                        link=served)
        assert placed["ok"], placed
        sent = [c for c in tab.seen if c["type"] == "set_volume_mode"][-1]["arguments"]
        assert sent["area"] == {"x": 100.0, "y": 50.0, "width": 300.0, "height": 300.0}
        cleared = invoke(session, "viewer_set_volume_mode", {"mode": "volume", "clear_area": True}, link=served)
        assert cleared["ok"], cleared
        assert [c for c in tab.seen if c["type"] == "set_volume_mode"][-1]["arguments"]["area"] is None
        seen = len(tab.seen)
        flat = invoke(session, "viewer_set_volume_mode",
                      {"area": {"x": 0, "y": 0, "width": 0, "height": 10}}, link=served)
        assert flat["error"]["code"] == "invalid_input"
        both = invoke(session, "viewer_set_volume_mode",
                      {"area": {"x": 0, "y": 0, "width": 5, "height": 5}, "clear_area": True}, link=served)
        assert both["error"]["code"] == "invalid_input"
        assert len(tab.seen) == seen


def test_a_z_step_can_stay_in_the_3d_view(served):
    """viewer_set_z with mode 'keep' moves z and leaves the view as it is (a
    projection, the 3-D view); by default it shows that one plane."""
    session = AgentSession()
    with FakeTab() as tab:
        assert invoke(session, "viewer_set_z", {"z": 5, "mode": "keep"}, link=served)["ok"]
        assert invoke(session, "viewer_set_z", {"z": 6}, link=served)["ok"]
        sent = [c["arguments"] for c in tab.seen if c["type"] == "set_z"]
        assert sent[0] == {"z": 5} and sent[1] == {"z": 6, "mode": "plane"}
