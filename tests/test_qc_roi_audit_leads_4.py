"""QC and ROI leads from the Nov 2026 audit (docs/internal/AUDIT_2026-11-14.md
§7), batch 4, each pinned by the symptom it had:

- r2-qc-writers-5: run_artifact_check's params.pixel_um was never stored, so
  the detector called itself unavailable (no_pixel_size) right after running
  and basic QC skipped it;
- r2-qc-writers-6: get_qc_results named, as the active result, a manual
  result its own read had made in memory (a new random id on every read)
  for a region drawn in a QC category and not yet taken in;
- the client leads (pg-qc-static-4/6/7, pg-roi-11):
  tests/js/qc_roi_audit_leads_4_probe.mjs, run below.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- r2-qc-writers-5 ------------------------------------------------------------------------


def test_a_pixel_size_passed_to_the_artifact_check_is_kept(tmp_path):
    from tests.qc_fixtures import make_qc_project

    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import artifacts

    make_qc_project(tmp_path, calibrated=False, size=256, grid=10, levels=2,
                    artifacts=("fold",))
    session = AgentSession()
    assert artifacts.availability(session, "qcsynth")["reason"] == "no_pixel_size"
    summary, _ = artifacts.load_or_run(session, "qcsynth", params={"pixel_um": 1.0})
    assert summary["pixel_um_source"] == "params"
    status = artifacts.public_status(session, "qcsynth")
    assert status["available"] is True, status["available_reason"]
    assert status["results"]["stale"] is False
    # A later run without the parameter (basic QC's) reuses the same result.
    _again, reused = artifacts.load_or_run(session, "qcsynth")
    assert reused


# -- r2-qc-writers-6 ------------------------------------------------------------------------


def test_get_qc_results_never_reports_an_unsaved_result_id(tmp_path):
    """A region drawn in a QC category through the ROI panel is taken in at
    the next write; a read before then must not name a result that exists only
    in that read's memory (a new random id every call)."""
    import json

    import plexora
    from plexora.agent import AgentSession, invoke, registry
    from plexora.plugins.qc.server import results
    from plexora.plugins.roi.server import service
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    client = plexora.app.test_client()
    made = client.post("/plugins/qc/categories", data=json.dumps(
        {"datasource": "qcsynth", "category": "tissue_acquisition"})).get_json()
    session = AgentSession()
    service.create_roi(session.image_data("qcsynth"), category=made["label"],
                       points=[[50, 50], [250, 50], [250, 250], [50, 250]])
    stored = results.load("qcsynth").get("active_result_id")
    first = invoke(session, "get_qc_results", {"project": "qcsynth"})
    second = invoke(session, "get_qc_results", {"project": "qcsynth"})
    assert first["ok"] and second["ok"]
    for answer in (first["result"], second["result"]):
        assert answer["active_result_id"] == stored
        assert answer["sync"]["adopted"]
        if answer.get("summary"):
            assert answer["summary"]["result_id"] == stored


# -- the client leads -----------------------------------------------------------------------


node = shutil.which("node")

PROBE = REPO_ROOT / "tests" / "js" / "qc_roi_audit_leads_4_probe.mjs"

CHECKS = (
    "pg-qc-static-4: revalidate drops a cell_at answer in flight",
    "pg-qc-static-6: blur QC's poll stops when the tool closes",
    "pg-qc-static-6: the artifact detector's poll stops when the tool closes",
    "pg-qc-static-6: segmentation QC's poll stops when the tool closes",
    "pg-qc-static-6: stack QC's poll stops when the tool closes",
    "pg-qc-static-6: the session follow stops when the tool closes",
    "pg-qc-static-7: the detector's objects are fetched once while in flight",
    "pg-qc-static-7: segmentation QC's cells are fetched once while in flight",
    "pg-qc-static-7: a blur map and mask are fetched once while in flight",
    "pg-roi-11: the tree walks the features once, not once per category",
    "pg-roi-11: a region's bounds are measured once, not every frame",
)


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_client_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines()
             if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
