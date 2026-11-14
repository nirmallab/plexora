"""A QC session rolled back while its bulk pass is still running stays rolled back.

`qc_session_finish(action="rollback")` undoes every receipt the session holds
and says so. If the deterministic pass is still running at that moment, it
must not go on writing afterwards: the Background ROI it writes in apply mode
(and its receipt) would land in the project after the rollback said
everything was undone, with nothing left to undo it. Either the finish waits
for or stops the pass, or it refuses until the pass is over.
"""

import json
import threading

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.pro

PROJECT = "qcrollbulk"


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def rois_of(session):
    return ok(invoke(session, "list_rois", {"project": PROJECT}))["rois"]


def test_a_rollback_during_the_bulk_pass_leaves_no_region_written_after_it(
        tmp_path, monkeypatch):
    from plexora.plugins.qc.server import bulk

    make_qc_project(tmp_path, name=PROJECT, artifacts=("saturation",))
    reached, release = threading.Event(), threading.Event()
    original = bulk.background_step

    def held_background_step(call, session_id, **kwargs):
        # The pass has scanned and is about to write the Background ROI:
        # hold it here while the agent finishes the session.
        reached.set()
        release.wait(120)
        return original(call, session_id, **kwargs)

    monkeypatch.setattr(bulk, "background_step", held_background_step)
    session = AgentSession()
    started = ok(invoke(session, "qc_session_start", {"project": PROJECT,
                                                      "map_cell_um": 25.0}))
    session_id = started["session_id"]
    try:
        assert reached.wait(180), "the bulk pass never reached the background step"
        assert rois_of(session) == []
        rolled = invoke(session, "qc_session_finish", {"session_id": session_id,
                                                       "action": "rollback"})
    finally:
        release.set()
        jobs.drain(180)
    if not rolled["ok"]:
        # Refusing while the pass runs is a correct answer too: then a
        # rollback once it is over must undo everything it wrote.
        assert rolled["error"]["code"] == "conflict", rolled["error"]
        ok(invoke(session, "qc_session_finish", {"session_id": session_id,
                                                 "action": "rollback"}))
    left = rois_of(session)
    assert left == [], (
        "the bulk pass wrote regions after the session was rolled back: "
        f"{[(r['name'], r['category_id']) for r in left]}")
