"""A detector that runs as a fallback after the channel audit keeps what it
finds.

The bulk pass marks the channels scanned before it runs the image checks,
so the agent audits the channels while the checks are still scoring. When
the Artifact Detector check then cannot run, the scan's saturation detector
runs in its place (`checks_bulk.fallback`). Its candidates must become
units to confirm even though their channels were audited, and closed clean,
in the meantime: a session never loses a kind of artifact to a failed check.
"""

import threading

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, ok

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _saturation_session(tmp_path, monkeypatch, name, *, audit_first):
    """A session on a scene with one saturated region in CD3, its Artifact
    Detector check held until released and then skipped. `audit_first`: the
    agent answers every channel audit while the check is held."""
    from plexora.plugins.qc.server import checks_bulk
    from plexora.plugins.qc.server.engine import store

    info = make_qc_project(tmp_path, name=name, artifacts=("saturation",))
    release = threading.Event()

    def held_then_skipped(call, project, unit, progress, cancelled):
        release.wait(120)
        raise checks_bulk._Skip("the Artifact Detector could not run here")

    monkeypatch.setitem(checks_bulk.RUNNERS, "artifacts", held_then_skipped)
    session = AgentSession()
    sid = ok(invoke(session, "qc_session_start", {"project": name,
                                                   "map_cell_um": 25.0}))["session_id"]
    agent = QCOracle(info)
    audits = 0
    if audit_first:
        for _ in range(60):
            nxt = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 2}))
            if nxt["state"] == "decision" and nxt["packet"]["kind"] == "channel_audit":
                packet = nxt["packet"]
                ok(invoke(session, "qc_answer", {
                    "session_id": sid, "packet_id": packet["packet_id"],
                    "answer": agent.answer(packet, sid), "include_next": False}))
                audits += 1
                continue
            if audits and not any(u["type"] == "channel" and u["state"] == "scanned"
                                  for u in store().load(sid)["units"].values()):
                break
        assert audits, "no channel audit was served while the check was held"
        record = store().load(sid)
        assert record["state"] == "bulk_running"
        assert record["units"][f"{name}::channel::CD3"]["state"] not in (
            "pending", "scanned"), "CD3 was not audited before the check finished"
    release.set()
    jobs.drain(180)
    record = store().load(sid)
    skipped = record["scan"][name]["skipped_detectors"]
    assert any(s.get("name") == "saturation" and s.get("fallback") for s in skipped), \
        "the saturation detector did not run as a fallback"
    return record


def _saturation_units(record):
    return [u for u in record["units"].values()
            if u["type"] == "candidate" and u.get("class_hint") == "saturation_or_clipping"]


def test_a_fallback_detector_run_before_the_audit_keeps_its_saturation_candidates(
        tmp_path, monkeypatch):
    record = _saturation_session(tmp_path, monkeypatch, "qcsatlate", audit_first=False)
    assert _saturation_units(record), "the scene's saturated region was not found at all"


def test_a_fallback_detector_run_after_the_audit_keeps_its_saturation_candidates(
        tmp_path, monkeypatch):
    record = _saturation_session(tmp_path, monkeypatch, "qcsatfirst", audit_first=True)
    channels = {u["id"]: u["state"] for u in record["units"].values()
                if u["type"] == "channel"}
    found = _saturation_units(record)
    assert found, ("the saturation detector ran as a fallback after the channel audit, "
                   f"but none of its candidates was kept; channels closed as {channels}")
    # Still to be looked at: its audit has passed, so not left waiting for one.
    assert all(u["state"] not in ("awaiting_audit",) for u in found), \
        [(u["id"], u["state"]) for u in found]
