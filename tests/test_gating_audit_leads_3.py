"""Audit leads (Nov 2026), gating batch 3.

- pg-gating-rest-11: a gates file named `.CSV` is uploaded, as the drop
  sniffer (`/\\.csv$/i`) already accepts it.
- pg-gating-rest-8 / -9: undoing a reset keeps a gate the user locked after
  the reset -- its value and its provenance row -- and puts the snapshot's
  provenance back under the provenance lock.
- r2-gating-writers-8: contrast_session_start(redo=true) puts the earlier
  contrast choices back when the start is refused.
- pg-gating-static-5/6/7 are client leads: tests/js/audit_leads_3_probe.mjs,
  driven at the bottom of this file.
"""

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.gating.tests.test_gating_export import client, project  # noqa: F401
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import HARD, ok

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "tests" / "js" / "audit_leads_3_probe.mjs"


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


# -- pg-gating-rest-11 ----------------------------------------------------------------


def test_an_upper_case_csv_is_taken_as_the_drop_sniffer_takes_it(client):  # noqa: F811
    name, app_client = client
    body = b"channel,gate_start,gate_end,thresholded\nMarkerA,2.0,5.0,TRUE\n"
    upload = app_client.post("/plugins/gating/upload_gates", data={
        "datasource": name, "file": (io.BytesIO(body), "GATES.CSV")},
        content_type="multipart/form-data")
    assert upload.status_code == 200
    rows = {row["channel"]: row for row in app_client.get(
        f"/plugins/gating/get_uploaded_gating_csv_values?datasource={name}").get_json()}
    assert rows["MarkerA"]["thresholded"] is True


def test_a_file_that_is_not_a_csv_is_still_refused(client):  # noqa: F811
    name, app_client = client
    upload = app_client.post("/plugins/gating/upload_gates", data={
        "datasource": name, "file": (io.BytesIO(b"{}"), "gates.json")},
        content_type="multipart/form-data")
    assert upload.status_code == 422


# -- pg-gating-rest-8 / -9 ------------------------------------------------------------


@pytest.mark.pro
def test_undoing_a_reset_keeps_a_gate_locked_after_it(tmp_path):
    from plexora.plugins.gating.server import model, provenance

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD4", "low": 150.0}))
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD8", "low": 120.0}))
    reset = ok(invoke(session, "reset_gates", {"project": "gsynth"}))
    assert {"CD4", "CD8"} <= set(reset["reset"])

    # The user sets CD4 by hand after the reset and locks it there.
    ds = session.data("gsynth")
    gate = model.get_gate(ds, "CD4")
    model.set_gate(ds, "CD4", 300.0, gate["high"])
    provenance.set_status("gsynth", "CD4", "locked", principal="user",
                          current=(300.0, gate["high"]))

    ok(invoke(session, "restore_gates", {"project": "gsynth",
                                         "snapshot": reset["receipt"]["operation_id"]}))
    ds = session.data("gsynth")
    assert model.get_gate(ds, "CD4")["low"] == pytest.approx(300.0)
    assert provenance.read("gsynth")["CD4"]["status"] == "locked"
    # The rest of the snapshot is still put back.
    assert model.get_gate(ds, "CD8")["low"] == pytest.approx(120.0)
    assert provenance.read("gsynth")["CD8"]["method"] is not None


# -- r2-gating-writers-8 --------------------------------------------------------------


@pytest.mark.pro
def test_a_refused_contrast_redo_keeps_the_earlier_choices(tmp_path, monkeypatch):
    from plexora.agent.errors import AgentError
    from plexora.agent.evidence import calibration
    from plexora.plugins.gating import capabilities_session

    make_gating_project(tmp_path, grid=16, size=768, markers=("CD3", "CD8"))
    record = {"version": calibration.VERSION, "channels": {},
              "choices": {"CD3": {"column": 2}, "CD8": {"column": 3}}}
    calibration.save("gsynth", record)

    def refuse(call, inp):
        raise AgentError("invalid_input", "refused for the test")

    monkeypatch.setattr(capabilities_session, "start", refuse)
    result = invoke(AgentSession(), "contrast_session_start",
                    {"scope": "project", "project": "gsynth", "redo": True, "check": "rule"})
    assert not result["ok"]
    assert set(calibration.load("gsynth")["choices"]) == {"CD3", "CD8"}


# -- pg-gating-static-5 / -6 / -7 (client) --------------------------------------------

CHECKS = [
    "an agent's colour-by keeps a change the debounce has not saved yet",
    "a table change keeps a change the debounce has not saved yet",
    "a column shown with persist:false is not saved as the project's selection",
    "a column the user picks is saved as the project's selection",
    "a classifier run's poll stops painting once the tool is removed",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO, timeout=120)


def test_the_client_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_client_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_client_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
