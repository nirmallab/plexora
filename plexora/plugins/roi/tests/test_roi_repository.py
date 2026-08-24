"""Annotations survive, and one session cannot silently overwrite another.

Exercised against a really-registered datasource rather than through the store
directly, so the JSON round trip, the project record and the image dimensions
are all covered -- the last of these being the thing the swapped-image guard
turns on.

Losing annotations is a silent failure by nature: the panel simply comes back
empty, and there is nothing on screen that says it should not have. So the tests
here are mostly about the states where a naive implementation loses them --
concurrent saves, an unreadable blob, a project whose image changed -- rather
than about the happy path.
"""

import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import data_model, database_model
from plexora.plugins.roi.server import schema
from plexora.plugins.roi.server.repository import (
    ConflictError,
    ImageMismatch,
    ROIRepository,
)

from tests.helpers import use_data_root

TRIANGLE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}

IMAGE_WIDTH = 256
IMAGE_HEIGHT = 256


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An image-only-shaped project: it has a CSV because register_datasource
    wants one, but nothing ROI does touches it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    tifffile.imwrite(image_path, np.zeros((2, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8))
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 3, 4, dtype=np.float32),
    }).write_csv(csv_path)

    use_data_root(monkeypatch, data_dir)

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="roi_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=None,
        data_dir=data_dir,
    )
    return "roi_sample", data_dir


CATEGORY = {"id": "c-1", "label": "Tumor", "color": "#e04c4c", "sort_order": 0}


def create(roi_id="r-1", category_id="c-1", geometry=None):
    """One region, and the category it belongs to.

    A project starts with no categories, so a bare `roi.create` has nowhere to
    put the shape. Written as a bulk_create carrying the category because that
    is still ONE operation -- the revision arithmetic these tests assert on is
    unchanged -- and because bulk_create leaves an existing category alone, so
    calling this repeatedly does what it looks like it does.
    """
    return {"op": "roi.bulk_create", "image": "default",
            "categories": [CATEGORY],
            "features": [{"id": roi_id, "category_id": category_id,
                          "geometry": geometry or TRIANGLE}]}


# -- the basic promise --------------------------------------------------

def test_a_project_with_no_rois_reads_as_empty_rather_than_erroring(project):
    name, _ = project
    state = ROIRepository(name).load()
    assert state["revision"] == 0
    assert state["images"]["default"]["features"] == []
    # No categories either: nothing is invented for a project nobody has
    # annotated, so the panel asks for the first name rather than assuming one.
    assert state["categories"] == []


def test_opening_the_panel_stores_no_annotations(project):
    """A project nobody has annotated should not acquire annotation state just
    because somebody opened the panel -- the default is returned, not saved.

    Asserted on rows rather than on the table existing: reading through the
    store creates the table it queries (see plexora/api/store.py's note on
    database_model.get), so an empty table is core's doing and says nothing
    about whether this plugin wrote."""
    name, data_dir = project
    ROIRepository(name).load()
    assert _rows(data_dir, name, "plugin_roi_state") == 0


def test_regions_survive_a_reload(project):
    name, _ = project
    revision = ROIRepository(name).apply(0, [create()])
    assert revision == 1

    state = ROIRepository(name).load()
    feature = state["images"]["default"]["features"][0]
    assert feature["id"] == "r-1"
    assert feature["geometry"]["coordinates"][0][1] == [10.0, 0.0]


def test_the_image_dimensions_travel_with_the_annotations(project):
    """Not decoration: this is what a swapped image is detected by."""
    name, _ = project
    ROIRepository(name).apply(0, [create()])
    space = ROIRepository(name).load()["images"]["default"]["coordinate_space"]
    assert (space["width"], space["height"]) == (IMAGE_WIDTH, IMAGE_HEIGHT)
    assert space["type"] == "image_pixels"
    assert space["origin"] == "top-left"


def test_saving_uses_the_namespaced_table(project):
    name, data_dir = project
    ROIRepository(name).apply(0, [create()])
    assert "plugin_roi_state" in _tables(data_dir, name)


# -- concurrency --------------------------------------------------------

def test_a_stale_writer_is_refused_rather_than_obeyed(project):
    """Two tabs, both holding a full copy, both autosaving. Without this the
    stale one's next save reinstates its whole world -- deleting every region
    drawn in the other, with nothing shown to either user."""
    name, _ = project
    first = ROIRepository(name)
    second = ROIRepository(name)

    first.apply(0, [create("r-1")])          # both read revision 0
    with pytest.raises(ConflictError) as caught:
        second.apply(0, [create("r-2")])

    assert caught.value.current_revision == 1
    assert [f["id"] for f in ROIRepository(name).load()["images"]["default"]["features"]] == ["r-1"]


def test_a_refused_write_leaves_the_revision_alone(project):
    name, _ = project
    ROIRepository(name).apply(0, [create()])
    with pytest.raises(ConflictError):
        ROIRepository(name).apply(0, [create("r-2")])
    assert ROIRepository(name).load()["revision"] == 1


def test_the_conflicted_writer_can_retry_once_it_catches_up(project):
    name, _ = project
    ROIRepository(name).apply(0, [create("r-1")])
    assert ROIRepository(name).apply(1, [create("r-2")]) == 2


def test_where_the_last_export_went_is_remembered(project):
    """So the panel opens with the name already in the field. Typing it
    correctly a second time is how two passes end up on top of each other."""
    name, _ = project
    repository = ROIRepository(name)
    assert repository.destination() == ""

    repository.apply(0, [create()])
    repository.remember_destination("rois_v2")
    assert ROIRepository(name).destination() == "rois_v2"


def test_remembering_a_destination_is_not_an_edit(project):
    """It says where a file write went, not what the annotations are.

    Bumping the revision would greet every other open tab with a conflict
    banner because somebody chose a filename -- and would strand whichever tab
    did the export, since its own next save would then be stale.
    """
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create()])

    repository.remember_destination("rois_v2")

    assert repository.load()["revision"] == 1
    # The proof that matters: a client still writing against revision 1 -- which
    # is every tab that was open a moment ago -- is not now stale.
    assert repository.apply(1, [create("r-2")]) == 2
    assert ROIRepository(name).destination() == "rois_v2"


def test_revisions_only_ever_go_forwards(project):
    """Undo is a new edit at a new revision, never a rewind -- the whole
    conflict check depends on the number being monotonic."""
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create("r-1")])
    repository.apply(1, [{"op": "roi.delete", "image": "default", "id": "r-1"}])
    repository.apply(2, [create("r-1")])
    assert repository.load()["revision"] == 3


@pytest.mark.parametrize("bad", ["1", None, 1.5, True])
def test_a_write_without_a_real_base_revision_is_refused(project, bad):
    """A client that omits it, or sends something coercible, must not end up
    with an accidental force-write."""
    name, _ = project
    with pytest.raises(ValueError, match="base_revision"):
        ROIRepository(name).apply(bad, [create()])


def test_a_rejected_operation_does_not_consume_a_revision(project):
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create()])
    with pytest.raises(ValueError):
        repository.apply(1, [create()])          # duplicate id
    assert repository.load()["revision"] == 1


# -- the image underneath changing --------------------------------------

def test_annotations_from_a_different_image_are_reported_not_drawn(project):
    """The dangerous case: the same datasource name now pointing at a different
    slide. The old regions render perfectly plausibly in the wrong places, and
    nothing about the numbers says otherwise."""
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create()])
    _rewrite_stored_size(repository, 9999, 8888)

    report = ROIRepository(name).status()
    assert report["dimension_mismatch"] is True
    assert report["stored_image_size"] == [9999, 8888]
    assert report["image_size"] == [IMAGE_WIDTH, IMAGE_HEIGHT]


def test_editing_is_refused_while_the_image_does_not_match(project):
    """Refused rather than allowed-with-a-warning: an edit here would attach new
    geometry to annotations that are already on the wrong image."""
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create()])
    _rewrite_stored_size(repository, 9999, 8888)

    with pytest.raises(ImageMismatch):
        ROIRepository(name).apply(1, [create("r-2")])


def test_a_project_with_no_regions_never_reports_a_mismatch(project):
    """There is nothing to be on the wrong image, so a resized project simply
    starts annotating -- rather than opening on a warning about zero regions."""
    name, _ = project
    repository = ROIRepository(name)
    repository.apply(0, [create()])
    repository.apply(1, [{"op": "roi.delete", "image": "default", "id": "r-1"}])
    _rewrite_stored_size(repository, 9999, 8888)
    assert ROIRepository(name).status()["dimension_mismatch"] is False


# -- damaged storage ----------------------------------------------------

def test_an_unreadable_blob_is_reported_rather_than_read_as_empty(project):
    """The alternative presents "your annotations cannot be read" as "this
    project has no annotations" -- and the next autosave then makes that true."""
    name, _ = project
    ROIRepository(name).apply(0, [create()])
    from plexora import api
    api.store(name, "roi").put_state(b"{not json at all")

    with pytest.raises(ValueError, match="could not be read"):
        ROIRepository(name).load()


def test_state_written_by_a_newer_plexora_is_refused(project):
    """Reading it with today's rules would mean quietly dropping whatever the
    newer schema added -- and then writing that loss back on the next save."""
    name, _ = project
    from plexora import api
    api.store(name, "roi").put_state(json.dumps({
        "schema_version": schema.SCHEMA_VERSION + 5, "revision": 3,
        "categories": [], "images": {},
    }).encode())

    with pytest.raises(ValueError, match="newer version"):
        ROIRepository(name).load()


def test_a_partially_unrecognisable_document_keeps_what_it_can(project):
    """Entries that cannot be understood are dropped; the ones that can are
    kept. Losing one malformed region is better than losing the file."""
    name, _ = project
    from plexora import api
    api.store(name, "roi").put_state(json.dumps({
        "schema_version": 1, "revision": 7,
        "categories": [{"id": "c-1", "label": "Tumor"}, {"label": "no id"}],
        "images": {"default": {"coordinate_space": {"width": IMAGE_WIDTH,
                                                    "height": IMAGE_HEIGHT},
                               "features": [
                                   {"id": "r-1", "category_id": "c-1",
                                    "geometry": TRIANGLE},
                                   {"category_id": "c-1", "geometry": TRIANGLE},
                               ]}},
    }).encode())

    state = ROIRepository(name).load()
    assert state["revision"] == 7
    assert [f["id"] for f in state["images"]["default"]["features"]] == ["r-1"]
    # The category with an id survives; the one without is dropped, and nothing
    # is substituted for it.
    assert [c["id"] for c in state["categories"]] == ["c-1"]


# -- helpers ------------------------------------------------------------

def _rewrite_stored_size(repository, width, height):
    """Swap the recorded dimensions, which is what replacing the image under a
    datasource looks like from this side."""
    state = repository.load()
    state["images"]["default"]["coordinate_space"].update(width=width, height=height)
    repository._write(state)


def _connect(data_dir, name):
    import sqlite3

    db_file = data_dir / name / f"{name}.db"
    return sqlite3.connect(str(db_file)) if db_file.exists() else None


def _tables(data_dir, name):
    conn = _connect(data_dir, name)
    if conn is None:
        return []
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


def _rows(data_dir, name, table):
    conn = _connect(data_dir, name)
    if conn is None or table not in _tables(data_dir, name):
        return 0
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()
