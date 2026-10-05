"""How a session reports registration: a local slip as the nuclei it moved,
a whole-cycle shift as a verdict on the channel (no ROI over the tissue),
and nothing at all when the cycles' DNA channels are not certain."""

import pytest

from plexora.agent import AgentSession, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, drive, ok, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _record(sid):
    from plexora.plugins.qc.server.engine import store

    return store().load(sid)


def _registration_candidates(sid):
    return [u for u in _record(sid)["units"].values()
            if u["type"] == "candidate" and u.get("detector") == "registration"]


def test_a_local_slip_is_outlined_by_the_nuclei_it_moved(tmp_path):
    from plexora.agent import invoke
    from plexora.plugins.qc.server import polygons

    info = make_qc_project(tmp_path, artifacts=("misregistration",))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    found = [u for u in _registration_candidates(started["session_id"])
             if u.get("roi_id") and not u.get("one_cycle")]
    assert found
    traced = [u for u in found if (u.get("refinement") or {}).get("method") == "nuclei"]
    assert traced, [u.get("nuclei_trace") for u in found]
    for unit in traced:
        record = unit["refinement"]
        assert record["kept"] >= 3 and record["kept"] == record["nuclei"]["displaced"]
        assert polygons.area_of(unit["geometry"]) <= polygons.area_of(
            unit["envelope_geometry"]) * 1.2
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    table = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["registration"]
    row = next(r for r in table if r["channel"] == "DNA_2")
    assert row["status"] == "local" and row["reference"] == "DNA_1"
    assert row["local_regions"] >= 1 and row["nuclei"]["displaced"] > 0


def test_a_whole_cycle_shift_is_a_channel_verdict_not_a_region(tmp_path):
    from plexora.agent import invoke
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("global_shift",))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    found = _registration_candidates(started["session_id"])
    whole = [u for u in found if u.get("whole_tissue")]
    assert whole and all(u.get("channel_level") and not u.get("roi_id") for u in whole)
    assert not [u for u in found if u.get("roi_id")]
    table = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["registration"]
    row = next(r for r in table if r["channel"] == "DNA_2")
    assert row["status"] == "shifted" and row["correctable"] and row["local_regions"] == 0
    assert set(row["markers"]) == {"DNA_2", "CD20"} and row["cells_affected"] > 0
    # Every cell has the shifted cycle's markers flagged; none is failed for it.
    cells = results.cells("qcsynth")
    flag = "CD20|region:cross_cycle_registration_error|"
    assert all(any(f.startswith(flag) for f in row or [])
               for row in cells["marker_flags"].to_list())
    assert not any("region:cross_cycle_registration_error" in (row or [])
                   for row in cells["excluded_by"].to_list())


def test_uncertain_dna_channels_skip_every_cycle_comparison(tmp_path, monkeypatch):
    """Two DNA channels a regular gap apart resolve at 0.9; demanding more
    leaves the cycles unresolved, and then nothing compares cycles: not the
    Registration Check, not its fallback detector, not tissue loss."""
    from plexora.plugins.qc.server import cycles

    assert cycles.resolved(cycles.infer(["DNA_1", "CD3", "CD8", "DNA_2", "CD20"]))[0]
    assert not cycles.resolved(cycles.infer(["DNA", "CD3", "CD8"]))[0]
    monkeypatch.setattr(cycles, "RESOLVED_CONFIDENCE", 0.99)
    info = make_qc_project(tmp_path, artifacts=("misregistration", "cycle_dropout"))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    record = _record(started["session_id"])
    checks = [u for u in record["units"].values()
              if u["type"] == "check" and u["check"] == "registration"]
    assert checks and all(u["state"] == "skipped_not_applicable" for u in checks)
    assert all("not certain" in u.get("reason", "") for u in checks)
    skipped = {d["name"]: d["reason"] for d in
               record["scan"]["qcsynth"].get("skipped_detectors") or []}
    assert "not certain" in skipped.get("registration", "")
    assert "not certain" in skipped.get("tissue_loss", "")
    assert not _registration_candidates(started["session_id"])
    assert not [u for u in record["units"].values() if u["type"] == "candidate"
                and u.get("class_hint") in ("cross_cycle_registration_error",
                                            "cycle_specific_tissue_loss")]
