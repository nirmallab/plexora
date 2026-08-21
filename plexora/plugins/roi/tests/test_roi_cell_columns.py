"""Writing ROI labels onto the user's cells.

Kept apart from test_roi_adapters.py because the failure modes are different.
Those tests are about not damaging the file; these are about landing the labels
on the RIGHT ROWS, which is a failure nothing about the resulting file makes
visible -- an .h5ad with every label shifted by one image's worth of cells reads
perfectly and is wrong forever.

So the cases here are all about alignment: a table that is a subset of its
file's obs, a file holding two images' cells, an identifier column that is not
in file order, and the length check that refuses when the file has moved on.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora import api
from plexora.plugins.roi.server import adapters, mapping, schema
from plexora.plugins.roi.server.operations import apply_operations
from plexora.server.models import data_model, database_model
from plexora.server.models.project import (
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    ImageSpec,
    Project,
    SegmentationSpec,
)

#: The datasource data_model keeps in module globals. Every one of these has to
#: go through monkeypatch so pytest unwinds it: a test that loads a project
#: leaves `config` pointing at its own tmp config.json, and the next test to
#: read `data_model.config` for a project of its own gets the previous test's
#: dict. That is not hypothetical -- it is what made test_segmentation_mapping
#: fail only when the plugin suites ran before it.
_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def isolate_data_model(monkeypatch):
    """Take ownership of data_model's globals for the duration of one test."""
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)


ad = pytest.importorskip("anndata")
pd = pytest.importorskip("pandas")
pytest.importorskip("shapely")

#: Bottom-left quadrant of a 256x256 image.
CORNER = {"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}


def annotations():
    state = schema.default_state(256, 256)
    state = apply_operations(state, [{
        "op": "category.create",
        "category": {"id": "c-1", "label": "Tumor", "color": "#e04c4c", "sort_order": 1}}])
    return apply_operations(state, [
        {"op": "roi.create", "image": "default",
         "feature": {"id": "r-1", "category_id": "c-1", "name": "Tumor 1",
                     "geometry": CORNER}}])


def register(tmp_path, monkeypatch, *, src, roles, **spec):
    """A project record pointing at `src`, without going through an import.

    Every module that captured `data_path`/`config_json_path` at import time has
    to be redirected, not just `plexora` -- these tests read the table through
    `dataset.table.frame()`, which goes all the way down to data_model's own
    copy of the config path. Patching one and not the others gives a project
    that saves fine and then cannot be found.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for module in (plexora, data_model, database_model):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", data_dir)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", data_dir / "config.json")
    # data_model keeps the loaded datasource in module globals, so a second
    # project of the same name in one session would otherwise be served the
    # first one's table -- which is exactly what the two-image cases below
    # depend on not happening. Two guards read two different names:
    # `_ensure_loaded` compares `source`, `load_datasource` compares
    # `_loaded_source`, and leaving either set serves the stale table.
    isolate_data_model(monkeypatch)

    # Two channels and 256px square, both load-bearing: a single-channel write
    # comes back as a 2D array and data_model indexes shape[2], and the pyramid
    # walk stops at the last level with every dimension >= 200.
    image_path = tmp_path / "image.tif"
    if not image_path.exists():
        tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))

    Project(
        name="proj",
        image=ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=({"name": "DNA", "fullname": "DNA", "src": "/generated/x/"},
                      {"name": "CD3", "fullname": "CD3", "src": "/generated/y/"}),
            width=256, height=256, max_level=1, tile_width=1024, tile_height=1024,
            num_channels=2),
        segmentation=SegmentationSpec(),
        dataset=DataSpec(
            type="anndata", src=str(src), roles=roles,
            columns=ColumnGroups(markers=("marker_0",), metadata=("id",)),
            features={"source": "X"}, **spec),
    ).save()
    return api.dataset("proj")


def cells(image_ids, xs, ys, names=None):
    """An .h5ad whose obs carries an image id and coordinates in obsm."""
    count = len(image_ids)
    index = names or [f"cell_{i}" for i in range(count)]
    adata = ad.AnnData(
        X=np.zeros((count, 3), dtype=np.float32),
        obs=pd.DataFrame({"imageid": list(image_ids), "cellid": list(index)},
                         index=[str(i) for i in index]),
        var=pd.DataFrame(index=[f"marker_{i}" for i in range(3)]),
    )
    adata.obsm["spatial"] = np.column_stack(
        [np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)])
    return adata


@pytest.fixture
def one_image(tmp_path):
    path = tmp_path / "one.h5ad"
    # Two cells inside the corner ROI, two outside it.
    cells(["A"] * 4, [10, 20, 200, 210], [10, 20, 200, 210]).write_h5ad(path)
    return path


@pytest.fixture
def two_images(tmp_path):
    path = tmp_path / "two.h5ad"
    # Image A's cells are all inside the ROI; image B's sit at the same
    # coordinates, so a mapping that ignores the image id annotates both.
    cells(["A", "A", "B", "B"], [10, 20, 10, 20], [10, 20, 10, 20],
          names=["a0", "a1", "b0", "b1"]).write_h5ad(path)
    return path


def spec(**overrides):
    base = dict(
        roles=ColumnRoles(cell_id="cellid", x="X", y="Y", image_id="imageid"),
        obs_id_field="cellid",
        coordinates={"source": "obsm", "obsm_key": "spatial"},
    )
    base.update(overrides)
    return base


def mapped(dataset, state, **kwargs):
    """Run the whole action the route runs, and hand back the reopened obs."""
    entry = state["images"][schema.DEFAULT_IMAGE]
    frame = dataset.table.frame()
    labels, names = mapping.assign(
        entry["features"], state["categories"],
        frame[dataset.schema.x].to_list(), frame[dataset.schema.y].to_list())
    result = adapters.write_cell_columns(dataset, labels, names, **kwargs)
    return result, ad.read_h5ad(dataset.table.source.path).obs


# -- the ordinary case ---------------------------------------------------

def test_cells_inside_the_region_are_labelled_and_the_rest_are_blank(
        tmp_path, monkeypatch, one_image):
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    result, obs = mapped(dataset, annotations())

    assert result["columns"] == ["rois_category", "rois_name"]
    assert result["n_assigned"] == 2
    # Blank, not null: these cells were tested and fell in no region. Null is
    # reserved for cells that were never tested -- see the two-image cases.
    assert list(obs["rois_name"].astype(object)) == ["Tumor 1", "Tumor 1", "", ""]
    assert list(obs["rois_category"].astype(object)) == ["Tumor", "Tumor", "", ""]


def test_the_column_names_follow_the_save_name(tmp_path, monkeypatch, one_image):
    """One name to keep track of, not two: the columns are derived from the same
    destination name the file save uses."""
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    result, obs = mapped(dataset, annotations(), prefix="pass2")
    assert result["columns"] == ["pass2_category", "pass2_name"]
    assert "pass2_name" in obs.columns
    assert "rois_name" not in obs.columns


def test_the_category_column_is_a_categorical(tmp_path, monkeypatch, one_image):
    """What obs columns of labels are everywhere else, and what makes scanpy
    plot it without being asked twice. Names are free text and stay strings."""
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    _, obs = mapped(dataset, annotations())
    assert isinstance(obs["rois_category"].dtype, pd.CategoricalDtype)


# -- the measurements are not touched ------------------------------------

def test_x_and_var_and_uns_are_untouched(tmp_path, monkeypatch, one_image):
    """The reason this rewrites obs and nothing else. A read-and-write-back of
    the whole file would rebuild X from whatever anndata currently thinks it
    should look like."""
    adata = ad.read_h5ad(one_image)
    adata.uns["something_else"] = "left alone"
    adata.write_h5ad(one_image)
    before = ad.read_h5ad(one_image)
    x_before, var_before = before.X.copy(), before.var.copy()

    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    mapped(dataset, annotations())

    after = ad.read_h5ad(one_image)
    np.testing.assert_array_equal(after.X, x_before)
    pd.testing.assert_frame_equal(after.var, var_before)
    assert after.uns["something_else"] == "left alone"
    # The obs columns that were already there come back unchanged; only the two
    # new ones are added.
    assert list(after.obs["imageid"]) == list(before.obs["imageid"])


# -- the image-id guard --------------------------------------------------

def test_only_the_current_image_is_annotated(tmp_path, monkeypatch, two_images):
    """The requirement that makes this safe to run once per image against one
    shared file. Image B's cells sit at the same coordinates as image A's, so
    an implementation that ignores the image id labels all four."""
    dataset = register(tmp_path, monkeypatch, src=two_images,
                       **spec(subset={"column": "imageid", "value": "A"}))
    _, obs = mapped(dataset, annotations())

    # Image B's two rows are null -- never tested -- while image A's cells,
    # which were, carry the label. A blank there would say "tested, no region".
    assert list(obs.index) == ["a0", "a1", "b0", "b1"]
    assert list(obs["rois_name"][:2]) == ["Tumor 1", "Tumor 1"]
    assert obs["rois_name"][2:].isna().all()
    assert obs["rois_category"][2:].isna().all()


def test_a_second_pass_for_the_other_image_leaves_the_first_alone(
        tmp_path, monkeypatch, two_images):
    """The point of the guard, stated as the workflow it enables."""
    dataset = register(tmp_path, monkeypatch, src=two_images,
                       **spec(subset={"column": "imageid", "value": "A"}))
    mapped(dataset, annotations())

    dataset = register(tmp_path, monkeypatch, src=two_images,
                       **spec(subset={"column": "imageid", "value": "B"}))
    _, obs = mapped(dataset, annotations(), replace=True)

    assert list(obs["rois_name"].astype(object)) == [
        "Tumor 1", "Tumor 1", "Tumor 1", "Tumor 1"]


def test_the_image_id_is_resolved_from_the_project_not_guessed(
        tmp_path, monkeypatch, two_images):
    dataset = register(tmp_path, monkeypatch, src=two_images,
                       **spec(subset={"column": "imageid", "value": "B"}))
    assert mapping.current_image_id(dataset) == "B"


def test_a_single_image_project_has_no_image_id_and_that_is_fine(
        tmp_path, monkeypatch, one_image):
    """'This table covers one image' is an answer, not an absence of one."""
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    assert mapping.current_image_id(dataset) is None


# -- alignment -----------------------------------------------------------

def test_labels_are_aligned_by_cell_id_not_by_position(tmp_path, monkeypatch, tmp_path_factory):
    """The subset case, which is where a positional write goes wrong silently.
    Image B's rows come FIRST in the file, so a write that starts at row 0 puts
    image A's labels on image B's cells."""
    path = tmp_path / "reordered.h5ad"
    cells(["B", "B", "A", "A"], [200, 210, 10, 20], [200, 210, 10, 20],
          names=["b0", "b1", "a0", "a1"]).write_h5ad(path)

    dataset = register(tmp_path, monkeypatch, src=path,
                       **spec(subset={"column": "imageid", "value": "A"}))
    _, obs = mapped(dataset, annotations())

    assert obs["rois_name"][:2].isna().all()
    assert list(obs["rois_name"][2:]) == ["Tumor 1", "Tumor 1"]


def test_a_file_that_has_changed_underneath_is_refused(tmp_path, monkeypatch, one_image):
    """Rather than writing labels that are off by however many rows moved. This
    is invisible in the resulting file, so refusing is the only safe answer."""
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    with pytest.raises(ValueError, match="reopen the project"):
        adapters.write_cell_columns(dataset, ["Tumor"], ["Tumor 1"])


# -- never overwrite unasked ---------------------------------------------

def test_an_existing_column_is_refused_and_a_free_name_offered(
        tmp_path, monkeypatch, one_image):
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    mapped(dataset, annotations())

    with pytest.raises(adapters.ColumnExists) as caught:
        mapped(dataset, annotations())
    assert "rois_category" in caught.value.existing
    assert caught.value.suggestion == "rois_2"


def test_replace_is_the_users_answer_and_it_works(tmp_path, monkeypatch, one_image):
    dataset = register(tmp_path, monkeypatch, src=one_image,
                       **spec(single_image=True,
                              roles=ColumnRoles(cell_id="cellid", x="X", y="Y")))
    mapped(dataset, annotations())
    _, obs = mapped(dataset, annotations(), replace=True)
    assert list(obs["rois_name"].astype(object)) == ["Tumor 1", "Tumor 1", "", ""]


# -- CSV -----------------------------------------------------------------

def test_a_csv_gets_the_same_two_columns(tmp_path, monkeypatch):
    """A CSV has no subtree, so the whole file is rewritten -- via a temp file
    and a rename, so a reader never sees it half-written."""
    import polars as pl

    path = tmp_path / "cells.csv"
    # Not called "id": the CSV adapter synthesizes a positional `id` of its own,
    # and a file that already has one makes the loaded frame refuse to build.
    pl.DataFrame({
        "CellID": [0, 1, 2, 3],
        "X": [10.0, 20.0, 200.0, 210.0],
        "Y": [10.0, 20.0, 200.0, 210.0],
        "marker_0": [1.0, 2.0, 3.0, 4.0],
    }).write_csv(path)

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(plexora, "data_path", data_dir, raising=False)
    monkeypatch.setattr(plexora, "config_json_path", data_dir / "config.json", raising=False)
    for module in (data_model, database_model):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", data_dir)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", data_dir / "config.json")
    # Both, because two different guards read them: `_ensure_loaded` compares
    # `source`, `load_datasource` compares `_loaded_source`. Leaving either set
    # serves the previous test's table for a project of the same name -- which
    # is exactly what the two-image cases below would otherwise silently pass on.
    isolate_data_model(monkeypatch)
    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    Project(
        name="proj",
        image=ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=({"name": "DNA", "fullname": "DNA", "src": "/generated/x/"},
                      {"name": "CD3", "fullname": "CD3", "src": "/generated/y/"}),
            width=256, height=256, max_level=1, tile_width=1024, tile_height=1024,
            num_channels=2),
        segmentation=SegmentationSpec(),
        dataset=DataSpec(
            type="csv", src=str(path),
            roles=ColumnRoles(cell_id="CellID", x="X", y="Y"),
            columns=ColumnGroups(markers=("marker_0",), metadata=("CellID",)),
            single_image=True),
    ).save()
    dataset = api.dataset("proj")

    state = annotations()
    entry = state["images"][schema.DEFAULT_IMAGE]
    frame = dataset.table.frame()
    labels, names = mapping.assign(
        entry["features"], state["categories"],
        frame["X"].to_list(), frame["Y"].to_list())
    result = adapters.write_cell_columns(dataset, labels, names)

    assert result["columns"] == ["rois_category", "rois_name"]
    written = pl.read_csv(path)
    assert written["rois_name"].to_list() == ["Tumor 1", "Tumor 1", "", ""]
    # The original columns survive, in their original order.
    assert written.columns[:4] == ["CellID", "X", "Y", "marker_0"]
