"""A QC region reshaped in the ROI panel, then a category switch:
gating must leave out the cells the region covers now, not the old outline's.

`set_qc_categories` re-derives the cells' calls without propagating the
regions again (`calls.write_for_active(refresh_regions=False)`). Those calls
must not be stamped as current for the ROI document as it is now, or the
staleness check gating relies on (`server/exclusions.py`) never re-derives
them and the region's old outline keeps excluding cells.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, cell_exclusions, invoke, registry
from tests.test_gating_qc_exclusion import _box, _fold_project

NEW_BOX = (700, 700, 960, 960)


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["roi", "qc", "gating"])
    cell_exclusions._reset_for_tests()
    yield
    cell_exclusions._reset_for_tests()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _draw_fold(session):
    drawn = ok(invoke(session, "create_roi", {"project": "gsynth",
                                              "category": "QC: Tissue fold",
                                              "geometry": _box(0, 0, 330, 330)}))
    first = cell_exclusions.current(session.data("gsynth"))
    assert first is not None and len(first.whole) > 0
    return drawn, first


def test_a_reshaped_region_then_a_category_switch_leaves_out_its_new_cells_only(tmp_path):
    _fold_project(tmp_path)
    session = AgentSession()
    drawn, first = _draw_fold(session)
    # Reshaped in the ROI panel (no refresh_qc): off the fold, onto a corner.
    ok(invoke(session, "update_roi", {"project": "gsynth", "roi_id": drawn["roi"]["id"],
                                      "geometry": _box(*NEW_BOX)}))
    # A category switch unrelated to the region.
    ok(invoke(session, "set_qc_categories", {"project": "gsynth",
                                             "disabled_categories": ["blur_focus"]}))
    after = cell_exclusions.current(session.data("gsynth"))
    old_outline = np.intersect1d(after.whole, first.whole)
    assert old_outline.size == 0, (
        f"{old_outline.size} cells of the region's old outline are still left out "
        f"({len(after.whole)} left out in all)")
    assert len(after.whole) > 0  # the new outline's cells are left out

