"""Writing annotations into the user's own file, without damaging it.

The target here is somebody's measurements -- often the only copy, frequently on
a share. So the assertions are mostly about what did NOT change: X, obs, var and
any pre-existing `uns` entries have to come back byte-for-byte, and a store's
tables have to still be readable afterwards.

That last one is not hypothetical. anndata refuses to write into a zarr group
whose metadata is consolidated, and the workaround -- open without the index,
write, rebuild the index -- silently loses the write if the rebuild is skipped,
and silently drops every v2 table if the rebuild is aimed at the store root
instead of the group written. Real SpatialData stores mix v2 tables into a v3
root, so both mistakes are reachable with ordinary data.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora import api
from plexora.plugins.roi.server import adapters, schema
from plexora.plugins.roi.server.operations import apply_operations
from plexora.server.models.project import (
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    ImageSpec,
    Project,
    SegmentationSpec,
)

ad = pytest.importorskip("anndata")
pd = pytest.importorskip("pandas")

TRIANGLE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}
WITH_HOLE = {"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
    [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
]}


def annotations():
    state = schema.default_state(256, 256)
    state = apply_operations(state, [{
        "op": "category.create",
        "category": {"id": "c-1", "label": "Tumor", "color": "#e04c4c", "sort_order": 1}}])
    return apply_operations(state, [
        {"op": "roi.create", "image": "default",
         "feature": {"id": "r-1", "category_id": "c-1", "name": "Tumor 1",
                     "geometry": TRIANGLE}},
        {"op": "roi.create", "image": "default",
         "feature": {"id": "r-2", "category_id": "c-1", "name": "Tumor 2",
                     "geometry": WITH_HOLE}},
    ])


def register(tmp_path, monkeypatch, *, kind, src, table=None):
    """A project record pointing at `src`, without going through an import."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    image_path = tmp_path / "image.tif"
    if not image_path.exists():
        tifffile.imwrite(image_path, np.zeros((1, 256, 256), dtype=np.uint8))

    Project(
        name="proj",
        image=ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=({"name": "DNA", "fullname": "DNA", "src": "/generated/x/"},),
            width=256, height=256, max_level=1, tile_width=1024, tile_height=1024,
            num_channels=1),
        segmentation=SegmentationSpec(),
        dataset=DataSpec(
            type=kind, src=str(src), table=table,
            roles=ColumnRoles(cell_id="id", x="X", y="Y"),
            columns=ColumnGroups(markers=("marker_0",), metadata=("id",)),
            features={"source": "X"}, single_image=True, row_number_ids=True),
    ).save()
    return api.dataset("proj")


# -- AnnData -------------------------------------------------------------

@pytest.fixture
def h5ad(tmp_path):
    path = tmp_path / "cells.h5ad"
    rng = np.random.default_rng(0)
    adata = ad.AnnData(
        X=rng.random((6, 3)).astype(np.float32),
        obs=pd.DataFrame({"imageid": ["A"] * 6}, index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=[f"marker_{i}" for i in range(3)]),
    )
    adata.uns["something_else"] = "left alone"
    adata.write_h5ad(path)
    return path


def test_annotations_land_in_uns_and_read_back(tmp_path, monkeypatch, h5ad):
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    result = adapters.save_to_anndata(dataset, annotations(), "test-version")

    assert result["key"] == "uns/plexora/rois"
    assert result["n_rois"] == 2

    reopened = ad.read_h5ad(h5ad)
    document = json.loads(reopened.uns["plexora"]["rois"])
    assert [f["name"] for f in document["images"]["default"]["features"]] == ["Tumor 1", "Tumor 2"]
    assert [c["label"] for c in document["categories"]] == ["Tumor"]
    # The hole survives the trip, as it must -- flattening it would make the
    # region cover pixels its author excluded.
    assert len(document["images"]["default"]["features"][1]["geometry"]["coordinates"]) == 2


def test_the_measurements_are_not_touched(tmp_path, monkeypatch, h5ad):
    """The whole reason this writes one subtree rather than round-tripping the
    file: a read-and-write-back would rebuild X, obs and var from whatever
    anndata's current version thinks they should look like."""
    before = ad.read_h5ad(h5ad)
    x_before, obs_before, var_before = before.X.copy(), before.obs.copy(), before.var.copy()

    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")

    after = ad.read_h5ad(h5ad)
    np.testing.assert_array_equal(after.X, x_before)
    pd.testing.assert_frame_equal(after.obs, obs_before)
    pd.testing.assert_frame_equal(after.var, var_before)


def test_other_uns_entries_survive(tmp_path, monkeypatch, h5ad):
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")
    assert ad.read_h5ad(h5ad).uns["something_else"] == "left alone"


def test_a_sibling_under_uns_plexora_is_kept(tmp_path, monkeypatch, h5ad):
    """Plexora owns one key in a namespace it may share with another of its own
    features, so writing annotations must not clear the rest of it."""
    adata = ad.read_h5ad(h5ad)
    adata.uns["plexora"] = {"gates": "somebody else's"}
    adata.write_h5ad(h5ad)

    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")

    reopened = ad.read_h5ad(h5ad).uns["plexora"]
    assert reopened["gates"] == "somebody else's"
    assert "rois" in reopened


def test_foreign_content_under_uns_plexora_is_refused_not_replaced(tmp_path, monkeypatch, h5ad):
    """Whatever is there belongs to somebody, and this plugin cannot tell what
    would be lost. Refused BEFORE anything is written, so the file is untouched."""
    adata = ad.read_h5ad(h5ad)
    adata.uns["plexora"] = "not a mapping at all"
    adata.write_h5ad(h5ad)

    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    with pytest.raises(ValueError, match="not a mapping"):
        adapters.save_to_anndata(dataset, annotations(), "test-version")

    assert ad.read_h5ad(h5ad).uns["plexora"] == "not a mapping at all"


def test_the_revision_is_not_written_to_the_file(tmp_path, monkeypatch, h5ad):
    """It is a fact about this project's store -- who last wrote, and whether a
    client is stale -- and means nothing in a file somebody else opens."""
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")
    document = json.loads(ad.read_h5ad(h5ad).uns["plexora"]["rois"])
    assert "revision" not in document


def test_a_csv_project_has_nowhere_to_write(tmp_path, monkeypatch):
    dataset = register(tmp_path, monkeypatch, kind="csv", src=tmp_path / "cells.csv")
    with pytest.raises(ValueError, match="did not come from an AnnData"):
        adapters.save_to_anndata(dataset, annotations(), "test-version")


# -- naming the entry ----------------------------------------------------
#
# One file, several annotation passes. The name is what keeps them apart, and
# what makes it possible to land one on top of another -- so everything below
# is about the second half of that.


def test_a_second_pass_keeps_its_own_name(tmp_path, monkeypatch, h5ad):
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")

    second = annotations()
    second["images"]["default"]["features"] = second["images"]["default"]["features"][:1]
    result = adapters.save_to_anndata(dataset, second, "test-version", key="rois_v2")

    assert result["key"] == "uns/plexora/rois_v2"
    stored = ad.read_h5ad(h5ad).uns["plexora"]
    # Both, side by side. The whole point: a second read of the same slide is
    # not a correction of the first one.
    assert sorted(stored) == ["rois", "rois_v2"]
    assert len(json.loads(stored["rois"])["images"]["default"]["features"]) == 2
    assert len(json.loads(stored["rois_v2"])["images"]["default"]["features"]) == 1


def test_an_existing_key_is_refused_and_nothing_is_written(tmp_path, monkeypatch, h5ad):
    """The dangerous case, and the reason the check sits before `del uns[...]`.

    Somebody else's annotation pass is under that name. Refusing after the
    group has been unlinked would leave the file with neither theirs nor ours.
    """
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")
    before = ad.read_h5ad(h5ad).uns["plexora"]["rois"]

    fewer = annotations()
    fewer["images"]["default"]["features"] = []
    with pytest.raises(adapters.KeyExists) as caught:
        adapters.save_to_anndata(dataset, fewer, "test-version", key="rois")

    assert caught.value.existing == ["rois"]
    assert caught.value.suggestion == "rois_2"
    assert ad.read_h5ad(h5ad).uns["plexora"]["rois"] == before


def test_replacing_takes_saying_so(tmp_path, monkeypatch, h5ad):
    """`replace` is the user's answer to having been asked, and the only thing
    that lets a write land on a name already in the file.

    Given it, the write replaces rather than accumulates -- the panel sends it
    automatically for the name this project last saved to, which is what keeps
    the ordinary draw-more-then-save-again loop free of dialogs.
    """
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")

    fewer = annotations()
    fewer["images"]["default"]["features"] = fewer["images"]["default"]["features"][:1]
    adapters.save_to_anndata(dataset, fewer, "test-version", key="rois", replace=True)

    stored = ad.read_h5ad(h5ad).uns["plexora"]
    assert sorted(stored) == ["rois"]
    assert len(json.loads(stored["rois"])["images"]["default"]["features"]) == 1


def test_a_free_name_needs_no_permission(tmp_path, monkeypatch, h5ad):
    """`replace` guards names that are taken, not the act of writing."""
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    adapters.save_to_anndata(dataset, annotations(), "test-version")
    adapters.save_to_anndata(dataset, annotations(), "test-version", key="second_read")
    assert sorted(ad.read_h5ad(h5ad).uns["plexora"]) == ["rois", "second_read"]


@pytest.mark.parametrize("key", ["../escape", ".hidden", "nested/name"])
def test_key_names_that_are_paths_are_refused(tmp_path, monkeypatch, h5ad, key):
    """It becomes a group under `uns/plexora`, so it has to be a name."""
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    with pytest.raises(ValueError, match="invalid name"):
        adapters.save_to_anndata(dataset, annotations(), "test-version", key=key)


@pytest.mark.parametrize("key", ["", None, "   "])
def test_no_key_given_means_the_default_one(tmp_path, monkeypatch, h5ad, key):
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    result = adapters.save_to_anndata(dataset, annotations(), "test-version", key=key)
    assert result["name"] == adapters.DEFAULT_UNS_KEY


def test_the_keys_already_in_the_file_can_be_listed(tmp_path, monkeypatch, h5ad):
    """So a collision is visible in the panel before it is typed."""
    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    assert adapters.existing_anndata_keys(dataset) == []

    adapters.save_to_anndata(dataset, annotations(), "test-version")
    adapters.save_to_anndata(dataset, annotations(), "test-version", key="rois_v2")
    assert adapters.existing_anndata_keys(dataset) == ["rois", "rois_v2"]


def test_listing_keys_never_costs_the_user_their_panel(tmp_path, monkeypatch, h5ad):
    """Best-effort by design: this runs on every panel open, and a file that
    cannot be read right now is not a reason to refuse to draw. The write path
    checks again, for real, and refuses there."""
    adata = ad.read_h5ad(h5ad)
    adata.uns["plexora"] = "not a mapping at all"
    adata.write_h5ad(h5ad)

    dataset = register(tmp_path, monkeypatch, kind="anndata", src=h5ad)
    assert adapters.existing_anndata_keys(dataset) == []


def test_a_csv_project_has_no_keys_to_list(tmp_path, monkeypatch):
    dataset = register(tmp_path, monkeypatch, kind="csv", src=tmp_path / "cells.csv")
    assert adapters.existing_anndata_keys(dataset) == []


# -- SpatialData ---------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    spatialdata = pytest.importorskip("spatialdata")
    from spatialdata.models import Image2DModel

    path = tmp_path / "example.zarr"
    sdata = spatialdata.SpatialData(images={
        "morphology": Image2DModel.parse(
            np.zeros((3, 64, 64), dtype=np.uint8), dims=("c", "y", "x")),
    })
    sdata.write(path)
    return path


def test_regions_become_a_shapes_element(tmp_path, monkeypatch, store):
    spatialdata = pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")

    result = adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")
    assert result["element"] == "shapes/plexora_rois"
    assert result["n_rois"] == 2

    reopened = spatialdata.read_zarr(store)
    frame = reopened.shapes["plexora_rois"]
    assert len(frame) == 2
    assert list(frame["category"]) == ["Tumor", "Tumor"]
    assert list(frame["roi_id"]) == ["r-1", "r-2"]
    # Coordinates go in untransformed: this project's shapes were drawn on the
    # image's own pixel grid, so pixel coordinates ARE the element's coordinates.
    assert frame.geometry.iloc[0].bounds == (0.0, 0.0, 10.0, 10.0)


def test_an_imported_hole_survives_as_a_hole(tmp_path, monkeypatch, store):
    spatialdata = pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")

    frame = spatialdata.read_zarr(store).shapes["plexora_rois"]
    doughnut = frame.geometry.iloc[1]
    assert len(doughnut.interiors) == 1
    assert doughnut.area == pytest.approx(100 * 100 - 20 * 20)


def test_the_store_s_other_elements_still_read(tmp_path, monkeypatch, store):
    """The consolidated-metadata trap: a rebuild aimed at the store root drops
    every v2 element from a v3 index, leaving a store whose contents have
    silently vanished."""
    spatialdata = pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")

    reopened = spatialdata.read_zarr(store)
    assert "morphology" in reopened.images
    assert reopened.images["morphology"].shape == (3, 64, 64)


def test_an_existing_element_is_never_overwritten(tmp_path, monkeypatch, store):
    """`shapes["roi"]` may be somebody's segmentation boundaries. Colliding asks
    for another name rather than replacing a layer this plugin cannot inspect."""
    pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")

    with pytest.raises(adapters.ElementExists) as caught:
        adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")

    assert "plexora_rois" in caught.value.existing
    assert caught.value.suggestion == "plexora_rois_2"


def test_existing_shapes_are_listed_before_the_user_commits_to_a_name(tmp_path, monkeypatch, store):
    pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    assert adapters.existing_shapes(dataset) == []
    adapters.save_to_spatialdata(dataset, annotations(), "plexora_rois")
    assert adapters.existing_shapes(dataset) == ["plexora_rois"]


def test_exporting_nothing_is_refused(tmp_path, monkeypatch, store):
    """An empty shapes element is not a useful thing to have written."""
    pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    with pytest.raises(ValueError, match="no ROIs"):
        adapters.save_to_spatialdata(dataset, schema.default_state(256, 256), "plexora_rois")


@pytest.mark.parametrize("name", ["../escape", ".hidden", "nested/name"])
def test_element_names_that_would_escape_the_store_are_refused(tmp_path, monkeypatch, store, name):
    """The name becomes a directory inside the store, so it has to be a name and
    not a path."""
    pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    with pytest.raises(ValueError, match="invalid name"):
        adapters.save_to_spatialdata(dataset, annotations(), name)


@pytest.mark.parametrize("name", ["", None, "   "])
def test_no_name_given_means_the_default_one(tmp_path, monkeypatch, store, name):
    """A user who presses OK on the name prompt without typing anything means
    "whatever you were going to call it", not an error."""
    pytest.importorskip("spatialdata")
    dataset = register(tmp_path, monkeypatch, kind="spatialdata", src=store, table="cells")
    result = adapters.save_to_spatialdata(dataset, annotations(), name)
    assert result["element"] == f"shapes/{adapters.DEFAULT_ELEMENT}"
