"""A QC session mirrored into a tab decides exactly what a headless one does.

The tab is `FakeTab` (tests/test_agent_viewer_api.py): a thread that polls,
records and acknowledges commands the way agentBridge.js does. What is
pinned: the same regions and cells with and without it (mirroring never
enters a decision or a memo key), the candidate's outline reaching the tab
(`show_shapes`), and the view given back at the end (`restore_viewer`).
"""

import threading

import pytest

import plexora
from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.agent.attach import ServerLink
from plexora.server.models import viewer_sessions as vs
from tests.qc_fixtures import make_qc_project
from tests.test_agent_viewer_api import FakeTab
from tests.test_qc_session import QCOracle

pytestmark = pytest.mark.paid


@pytest.fixture
def served(tmp_path):
    from werkzeug.serving import make_server

    registry.discover(["roi", "qc"])
    vs._reset_for_tests()
    server = make_server("127.0.0.1", 0, plexora.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ServerLink(f"http://127.0.0.1:{server.server_port}/")
    server.shutdown()
    vs._reset_for_tests()


def _run(session, info, *, link=None, mirror=False):
    started = invoke(session, "qc_session_start", {
        "project": "qcsynth", "map_cell_um": 25.0, "mirror": mirror, "mirror_delay_ms": 0,
        "reuse_answers": False}, link=link)
    assert started["ok"], started
    sid = started["result"]["session_id"]
    jobs.drain(180)
    agent = QCOracle(info)
    mirrors = []
    result = invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}, link=link)["result"]
    while result["state"] == "decision":
        packet = result["packet"]
        mirrors.append(packet.get("mirror"))
        answered = invoke(session, "qc_answer", {"session_id": sid,
                                                 "packet_id": packet["packet_id"],
                                                 "answer": agent.answer(packet, sid)},
                          link=link)
        assert answered["ok"], answered
        result = answered["result"]["next"]
    finished = invoke(session, "qc_session_finish", {"session_id": sid}, link=link)["result"]
    rois = invoke(session, "list_rois", {"project": "qcsynth"})["result"]["rois"]
    regions = sorted((r["category_id"], r["name"], tuple(r["bounds"])) for r in rois)
    return sid, regions, mirrors, finished


def test_a_mirrored_session_decides_what_a_headless_one_does(served, tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    _sid, headless, off, _f = _run(session, info)
    cells_headless = results.cells("qcsynth")["pass"].to_list()
    invoke(session, "qc_session_finish", {"session_id": _sid, "action": "rollback"})
    with FakeTab(project="qcsynth") as tab:
        sid, mirrored, on, finished = _run(session, info, link=served, mirror=True)
    assert mirrored == headless and headless
    assert results.cells("qcsynth")["pass"].to_list() == cells_headless
    assert all(m is None for m in off)
    assert any(m and m.get("status") == "ok" for m in on), on
    types = [c["type"] for c in tab.seen]
    assert "show_shapes" in types and "fit_region" in types and "show_evidence" in types
    shapes = next(c for c in tab.seen if c["type"] == "show_shapes")["arguments"]["shapes"]
    assert shapes and shapes[0].get("geometry", {}).get("type") in ("Polygon", "MultiPolygon")
    assert types[-1] == "restore_viewer", types[-5:]
    assert finished["teardown"]["status"] == "ok"


def test_mirroring_with_no_tab_degrades_and_the_session_completes(served, tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    sid, regions, mirrors, _finished = _run(session, info, link=served, mirror=True)
    assert regions
    status = invoke(session, "qc_session_status", {"session_id": sid})["result"]
    assert status["mirror"]["status"] == "off"


def test_an_open_tab_is_mirrored_by_default_and_false_opts_out(served, tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    with FakeTab(project="qcsynth") as tab:
        started = invoke(session, "qc_session_start", {
            "project": "qcsynth", "map_cell_um": 25.0, "mirror_delay_ms": 0}, link=served)
        assert started["ok"], started
        mirror = started["result"]["mirror"]
        assert mirror["status"] == "pending" and mirror["requested"] == "auto", mirror
        invoke(session, "qc_session_finish", {"session_id": started["result"]["session_id"],
                                              "action": "cancel"}, link=served)
        jobs.drain(180)
        seen = len(tab.seen)
        _sid, _regions, on, _finished = _run(session, info, link=served, mirror=None)
        auto_types = [c["type"] for c in tab.seen[seen:]]
        seen = len(tab.seen)
        _sid, _regions, off, _finished = _run(session, info, link=served, mirror=False)
        opted_out = [c["type"] for c in tab.seen[seen:]]
    assert any(m and m.get("status") == "ok" for m in on), on
    assert "show_evidence" in auto_types and "fit_region" in auto_types
    assert all(m is None for m in off)
    assert not {"show_evidence", "fit_region", "show_shapes"} & set(opted_out), opted_out


def test_auto_mirror_with_no_tab_is_off_and_says_why(served, tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = invoke(session, "qc_session_start", {"project": "qcsynth", "map_cell_um": 25.0},
                     link=served)
    assert started["ok"], started
    mirror = started["result"]["mirror"]
    assert mirror["status"] == "off" and mirror["reason"].startswith("no viewer"), mirror
    assert "last_error" not in mirror    # nobody asked for it: not an error
    invoke(session, "qc_session_finish", {"session_id": started["result"]["session_id"],
                                          "action": "cancel"})
    jobs.drain(180)
