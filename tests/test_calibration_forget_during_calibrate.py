"""A contrast redo's `forget_choices` that lands while `calibrate` computes
stays forgotten (audit ai-agent-evidence-5). `calibrate` used to merge the
choices it read BEFORE computing with the latest ones, so a choice taken off
meanwhile came straight back."""

import pytest

from plexora.agent import AgentSession, registry
from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_a_choice_forgotten_during_a_calibration_stays_forgotten(tmp_path, monkeypatch):
    from plexora.agent.evidence import calibration

    make_gating_project(tmp_path)
    session = AgentSession()
    calibration.calibrate(session, "gsynth")
    calibration.record_choices("gsynth", {"CD3": {
        "column": "c2", "gain": 0.8, "band": "negatives", "chosen_by": "vision"}})
    assert "CD3" in calibration.load("gsynth")["choices"]

    real = calibration.compute

    def compute_while_redo(*args, **kwargs):
        calibration.forget_choices("gsynth", ["CD3"])
        return real(*args, **kwargs)

    monkeypatch.setattr(calibration, "compute", compute_while_redo)
    record, _changed = calibration.calibrate(session, "gsynth", force=True)

    assert "CD3" not in (record.get("choices") or {})
    assert "CD3" not in (calibration.load("gsynth").get("choices") or {})
    assert record["channels"]["CD3"]["chosen_by"] == "rule"
