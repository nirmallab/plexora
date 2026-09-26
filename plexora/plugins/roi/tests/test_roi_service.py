"""ROI annotations through the headless service an agent uses."""

import pytest

from plexora.plugins.roi.server import service
from plexora.plugins.roi.server.repository import ConflictError
from plexora.server.models import data_model
from tests.agent_fixtures import local_handles, make_synthetic_project


@pytest.fixture
def ds(tmp_path):
    info = make_synthetic_project(tmp_path)
    return local_handles(info["name"])


SQUARE = [[0, 0], [130, 0], [130, 130], [0, 130]]


def test_create_list_get_update_delete(ds):
    before, after, created = service.create_roi(ds, category="Tumor", points=SQUARE,
                                                name="corner")
    assert (before, after) == (0, 1)
    assert created["category"] == "Tumor" and created["name"] == "corner"
    assert created["id"].startswith("roi_")
    assert created["bounds"] == [0.0, 0.0, 130.0, 130.0]

    # A second region reuses the category, whatever case it is written in.
    _, _, second = service.create_roi(ds, category="tumor", points=SQUARE)
    assert second["category_id"] == created["category_id"]

    revision, listed = service.list_rois(ds)
    assert revision == 2 and len(listed) == 2

    _, after, old, new = service.update_roi(ds, created["id"], name="renamed",
                                            category="Stroma")
    assert old["name"] == "corner" and new["name"] == "renamed"
    assert new["category"] == "Stroma"

    _, _, deleted = service.delete_roi(ds, second["id"])
    assert deleted["id"] == second["id"]
    assert [r["id"] for r in service.list_rois(ds)[1]] == [created["id"]]


def test_a_stale_revision_is_a_conflict(ds):
    service.create_roi(ds, category="Tumor", points=SQUARE)
    with pytest.raises(ConflictError):
        service.create_roi(ds, category="Tumor", points=SQUARE, base_revision=0)


def test_cells_in_roi_counts_centroids_inside(ds):
    _, _, roi = service.create_roi(ds, category="Tumor", points=SQUARE)
    result = service.cells_in_roi(ds, roi["id"])
    # The 8x8 grid on 512 px puts centroids at 32, 96, 160...: a 130 px square
    # holds the 2x2 in the corner.
    assert result["n_cells"] == 4
    assert sorted(result["cell_ids"]) == [1, 2, 9, 10]
    assert data_model._loaded_source is None


def test_unknown_region_is_a_key_error(ds):
    with pytest.raises(KeyError):
        service.get_roi(ds, "roi_missing")
