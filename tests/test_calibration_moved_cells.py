"""A display calibration anchored on cells that have since moved is stale.

lsp35766_p193_2 (2026-11-21): the table's coordinates were first read in the
wrong space, so the calibration's anchor cells were glass. Fixing them left
the matrix alone, and the expression fingerprint alone kept that window --
every gating look was drawn overexposed until a forced recalibration."""

import polars as pl
import pytest

from plexora.agent import AgentSession, registry
from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_the_coordinates_digest_follows_the_cells():
    from plexora.agent.evidence import calibration

    class Roles(dict):
        pass

    class Record:
        roles = Roles(x="X", y="Y")

    class Session:
        def project(self, name):
            return Record()

    class Table:
        def __init__(self, xs, ys):
            self.frame = pl.DataFrame({"X": xs, "Y": ys})

        def geometry(self):
            return self.frame

    here = calibration._coordinates_fingerprint(Session(), "p", Table([10.0, 20.0], [5.0, 6.0]))
    same = calibration._coordinates_fingerprint(Session(), "p", Table([10.2, 19.9], [5.1, 5.8]))
    moved = calibration._coordinates_fingerprint(Session(), "p", Table([32.0, 64.0], [16.0, 19.0]))
    assert here and here == same and moved != here


def test_a_calibration_on_cells_that_moved_is_not_current(tmp_path, monkeypatch):
    from plexora.agent.evidence import calibration

    make_gating_project(tmp_path)
    session = AgentSession()
    calibration.calibrate(session, "gsynth")
    assert calibration.load_current(session, "gsynth") is not None
    assert "|xy:" in calibration.load("gsynth")["table"]

    monkeypatch.setattr(calibration, "_coordinates_fingerprint", lambda *a: "elsewhere")
    assert calibration.load_current(session, "gsynth") is None
    record, changed = calibration.calibrate(session, "gsynth")
    assert changed and record["table"].endswith("|xy:elsewhere")


def test_a_vision_choice_made_on_cells_that_moved_is_asked_again(tmp_path, monkeypatch):
    """A contrast check's choice records what it saw on the cells it was shown;
    once the cells move, it saw other places, so it is dropped and the check
    is asked again. A choice survives a recomputation that leaves the cells
    where they were (a QC change, say)."""
    from plexora.agent.evidence import calibration

    make_gating_project(tmp_path)
    session = AgentSession()
    calibration.calibrate(session, "gsynth")
    calibration.record_choices("gsynth", {"CD3": {
        "column": "c2", "gain": 0.8, "band": "negatives", "chosen_by": "vision"}})
    record, _ = calibration.calibrate(session, "gsynth", force=True)
    assert "CD3" in (record.get("choices") or {})

    monkeypatch.setattr(calibration, "_coordinates_fingerprint", lambda *a: "elsewhere")
    record, changed = calibration.calibrate(session, "gsynth")
    assert changed and not record.get("choices")
    assert calibration.needs_choice(record, "CD3")


def test_cells_moved_reads_only_the_coordinates_part():
    from plexora.agent.evidence.calibration import _cells_moved

    assert _cells_moved("X|1|xy:aa", "X|1|xy:bb")
    assert not _cells_moved("X|1|xy:aa", "Y|0|xy:aa")
    assert not _cells_moved("X|1", "X|1|xy:bb")
