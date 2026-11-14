"""`gating_session_finish` on a session that is already finished: a retried
close (an MCP client that timed out and called again) or a close after a
rollback must not record the session's decisions a second time, and must not
turn a rolled-back session back into a done one."""

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.gating.server import experience
from plexora.plugins.gating.server.autogate import engine as engines
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import Oracle, drive, start

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _session(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], Oracle(info))
    jobs.drain(120)
    return session, started["session_id"]


def _decisions(sid):
    return [r for r in experience.read("gsynth")
            if r["row"] == "decision" and r["session_id"] == sid]


def test_a_retried_close_records_each_decision_once(tmp_path):
    session, sid = _session(tmp_path)
    first = invoke(session, "gating_session_finish", {"session_id": sid})
    assert first["ok"], first
    once = len(_decisions(sid))
    assert once == 3                                  # CD3, CD8, CD20
    invoke(session, "gating_session_finish", {"session_id": sid})
    rows = _decisions(sid)
    assert len(rows) == once, sorted(r["marker"] for r in rows)


def test_a_close_after_a_rollback_leaves_the_session_rolled_back(tmp_path):
    session, sid = _session(tmp_path)
    rolled = invoke(session, "gating_session_finish", {"session_id": sid, "action": "rollback"})
    assert rolled["ok"], rolled
    corrections = [r for r in experience.read("gsynth")
                   if r["row"] == "correction" and r.get("what") == "rolled_back"]
    decisions = len(_decisions(sid))
    invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"})
    assert engines.store().load(sid)["state"] == "rolled_back"
    assert len(_decisions(sid)) == decisions
    assert len([r for r in experience.read("gsynth")
                if r["row"] == "correction" and r.get("what") == "rolled_back"]) \
        == len(corrections)
