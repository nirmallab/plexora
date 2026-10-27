"""What a drop on the window does, decided against what is open.

`drop_plan.inspect_drop` composes the import engine's answer (what a file IS)
with the open project (what it is FOR). These tests hold the confidence
policy: what is done without asking, what is asked, what is refused. The
rules that matter most are the ones that protect data -- a mask whose size
differs from the image is never attached on its name alone, an existing mask
is never replaced without asking, a file already attached is not attached
twice.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import drop_plan, import_proposal, layer_jobs
from plexora.server.models.project import Project

from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    rng = np.random.default_rng(0)
    tifffile.imwrite(source / "slide.ome.tif",
                     rng.integers(0, 4000, (2, 128, 128)).astype(np.uint16))
    tifffile.imwrite(source / "slide_cellmask.tif", _cells(128))
    tifffile.imwrite(source / "slide_nuclei_mask.tif", _cells(128))
    tifffile.imwrite(source / "slide_small_mask.tif", _cells(64))
    tifffile.imwrite(source / "unrelated_mask.tif", _cells(128))
    (source / "slide.csv").write_text(
        "CellID,X_centroid,Y_centroid,CD3\n1,10,10,5\n2,30,25,3\n", encoding="utf-8")
    (source / "plain.csv").write_text(
        "CellID,CD3,CD8\n1,5,1\n2,3,2\n", encoding="utf-8")
    (source / "markers.csv").write_text(
        "channel_number,cycle_number,marker_name\n1,1,DNA\n2,1,CD3\n",
        encoding="utf-8")
    (source / "markers_three.csv").write_text(
        "channel_number,cycle_number,marker_name\n1,1,DNA\n2,1,CD3\n3,1,CD8\n",
        encoding="utf-8")
    (source / "notes.xyz").write_text("nothing", encoding="utf-8")
    layer_jobs.forget()
    drop_plan.forget()
    test_client = plexora.app.test_client()
    yield test_client
    layer_jobs.forget()
    drop_plan.forget()


def _cells(size):
    labels = np.zeros((size, size), dtype=np.uint32)
    for index in range(1, 6):
        top = index * (size // 7)
        labels[top:top + size // 10, top:top + size // 10] = index
    return labels


def _source():
    from plexora import paths

    return paths.data_root() / "source"


def _path(name):
    return str(_source() / name)


def _register(client, *names, name=None):
    payload = {"paths": [_path(n) for n in names]}
    if name:
        payload["name"] = name
    response = client.post("/import/sample", json=payload)
    assert response.status_code == 200, response.get_json()
    drop_plan.forget()
    return response.get_json()["name"]


def _plan(client, *names, datasource=None, target="viewer", paths=None):
    response = client.post("/drop/inspect", json={
        "paths": paths if paths is not None else [_path(n) for n in names],
        "datasource": datasource, "target": target})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _only(plan):
    assert len(plan["actions"]) == 1, plan["actions"]
    return plan["actions"][0]


# -- masks -------------------------------------------------------------------

def test_a_mask_of_the_images_size_and_name_attaches_without_asking(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide_cellmask.tif", datasource=sample))
    assert action["kind"] == "attach_mask"
    assert action["status"] == "auto"
    assert action["route"]["path"] == "import/layers"
    assert action["route"]["json"]["sample"] == sample
    assert action["route"]["json"]["paths"] == [_path("slide_cellmask.tif")]
    assert action["effects"]["reload"] is True
    assert action["fit"]["compatible"] is True
    assert action["fit"]["nameMatch"] == "exact"


def test_a_mask_of_another_size_is_asked_about_even_with_the_right_name(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide_small_mask.tif", datasource=sample))
    assert action["status"] == "needs_confirm"
    assert action["reason"] == "size_mismatch"
    assert action["fit"]["size"] == [64, 64]
    assert action["fit"]["imageSize"] == [128, 128]
    assert action["fit"]["compatible"] is False
    # Attaching anyway is offered, never the default.
    assert action["default"] != "attach"
    assert {c["value"] for c in action["choices"]} >= {"attach", "layer"}


def test_a_mask_of_the_right_size_and_another_name_is_asked_about(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "unrelated_mask.tif", datasource=sample))
    assert action["status"] == "needs_confirm"
    assert action["reason"] == "name_mismatch"
    assert action["route"]["path"] == "import/layers"


def test_an_existing_mask_is_never_replaced_without_asking(client):
    sample = _register(client, "slide.ome.tif", "slide_cellmask.tif")
    action = _only(_plan(client, "slide_nuclei_mask.tif", datasource=sample))
    assert action["status"] == "needs_confirm"
    assert action["reason"] == "mask_exists"
    replace = next(c for c in action["choices"] if c["value"] == "replace")
    assert replace["tone"] == "danger"
    assert action["default"] is None


def test_the_same_mask_twice_is_already_attached_and_not_attached_again(client):
    sample = _register(client, "slide.ome.tif", "slide_cellmask.tif")
    action = _only(_plan(client, "slide_cellmask.tif", datasource=sample))
    assert action["kind"] == "noop_already_attached"
    assert action["route"] is None
    # And the route itself, posted again, adds nothing.
    proposal = import_proposal.inspect_paths(
        [_path("slide_cellmask.tif")], sample=sample)
    assert proposal.samples[0].layers == []


def test_a_mask_attached_to_another_project_offers_that_project_first(client):
    other = _register(client, "slide.ome.tif", "slide_cellmask.tif", name="first")
    sample = _register(client, "slide.ome.tif", name="second")
    action = _only(_plan(client, "slide_cellmask.tif", datasource=sample))
    assert action["reason"] == "attached_elsewhere"
    assert action["default"] == "open"
    opened = next(c for c in action["choices"] if c["value"] == "open")
    assert opened["sample"] == other
    assert opened["route"] is None


def test_a_mask_with_nothing_open_is_offered_to_the_samples_it_fits(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide_cellmask.tif", target="home"))
    assert action["kind"] == "attach_mask"
    assert action["reason"] == "choose_target"
    assert action["status"] == "needs_confirm"
    assert action["default"] == sample
    assert [c["value"] for c in action["choices"]] == [sample]


def test_a_mask_that_fits_nothing_goes_to_the_import_dialog(client):
    _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide_small_mask.tif", target="home"))
    assert action["kind"] == "open_dialog"
    assert action["reason"] == "mask_needs_image"


# -- cell tables ---------------------------------------------------------------

def test_a_cell_table_attaches_without_asking(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide.csv", datasource=sample))
    assert action["kind"] == "attach_table"
    assert action["status"] == "auto"
    assert action["route"]["json"]["paths"] == [_path("slide.csv")]


def test_a_table_without_coordinates_is_asked_about(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "plain.csv", datasource=sample))
    assert action["reason"] == "no_coordinates"
    assert action["status"] == "needs_confirm"


def test_a_second_table_asks_before_replacing_the_first(client):
    sample = _register(client, "slide.ome.tif", "slide.csv")
    (_source() / "again.csv").write_text(
        "CellID,X_centroid,Y_centroid\n1,1,1\n", encoding="utf-8")
    action = _only(_plan(client, "again.csv", datasource=sample))
    assert action["reason"] == "table_exists"


def test_the_attached_table_again_is_already_attached(client):
    sample = _register(client, "slide.ome.tif", "slide.csv")
    action = _only(_plan(client, "slide.csv", datasource=sample))
    assert action["kind"] == "noop_already_attached"
    # The project holds a COPY of a flat table; the original posted to the
    # route again is still recognised, and adds nothing.
    proposal = import_proposal.inspect_paths([_path("slide.csv")], sample=sample)
    assert proposal.samples[0].layers == []


# -- channel names -------------------------------------------------------------

def test_markers_csv_names_a_sample_whose_channels_have_no_names(client):
    sample = _register(client, "slide.ome.tif")
    plan = _plan(client, "markers.csv", datasource=sample)
    assert plan["unrecognised"] == []
    action = _only(plan)
    assert action["kind"] == "apply_channel_names"
    form = action["route"]["form"]
    assert form == {"datasource": sample, "path": _path("markers.csv"),
                    "column": "2", "has_header": "true"}
    assert action["route"]["path"] == "upload_channels"
    expected = "auto" if plan["open"]["genericNames"] else "needs_confirm"
    assert action["status"] == expected


def test_markers_csv_of_the_wrong_length_says_so(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "markers_three.csv", datasource=sample))
    assert action["reason"] == "count_mismatch"
    assert action["fit"]["markerCount"] == 3
    assert action["fit"]["channelCount"] == 2
    # No column: the route's own picker/mismatch answer takes it from there.
    assert "column" not in action["route"]["form"]


def test_markers_csv_over_named_channels_asks_first(client):
    sample = _register(client, "slide.ome.tif")
    client.post("/upload_channels", data={
        "datasource": sample, "path": _path("markers.csv"),
        "column": "2", "has_header": "true"})
    drop_plan.forget()
    (_source() / "renamed.csv").write_text(
        "marker_name\nHoechst\nCD4\n", encoding="utf-8")
    action = _only(_plan(client, "renamed.csv", datasource=sample))
    assert action["reason"] == "names_present"
    assert action["fit"]["current"] == ["DNA", "CD3"]


def test_markers_csv_with_nothing_open_is_refused(client):
    action = _only(_plan(client, "markers.csv", target="home"))
    assert action["status"] == "blocked"
    assert action["reason"] == "needs_open_sample"


def test_markers_csv_with_images_names_the_new_sample_not_the_open_one(client):
    sample = _register(client, "slide.ome.tif")
    (_source() / "fresh.ome.tif").write_bytes(
        (_source() / "slide.ome.tif").read_bytes())
    tifffile.imwrite(_source() / "fresh.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    plan = _plan(client, "fresh.ome.tif", "markers.csv", datasource=sample)
    assert not [a for a in plan["actions"] if a["kind"] == "apply_channel_names"]


# -- images and projects -------------------------------------------------------

def test_the_open_image_dropped_again_is_already_open(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide.ome.tif", datasource=sample))
    assert action["kind"] == "noop_already_open"


def test_an_image_of_another_project_opens_that_project(client):
    other = _register(client, "slide.ome.tif")
    tifffile.imwrite(_source() / "second.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    sample = _register(client, "second.ome.tif")
    action = _only(_plan(client, "slide.ome.tif", datasource=sample))
    assert action["kind"] == "open_project"
    assert action["sample"] == other
    assert action["effects"]["navigate"] is True
    # Navigation by name, never a request that could import a copy.
    assert action["route"] is None


def test_a_new_unrelated_image_opens_as_a_new_sample_in_one_press(client):
    sample = _register(client, "slide.ome.tif")
    tifffile.imwrite(_source() / "tonsil.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    plan = _plan(client, "tonsil.ome.tif", datasource=sample)
    action = _only(plan)
    assert action["kind"] == "import_new"
    assert action["one_press"] is True
    assert action["route"]["path"] == "import/sample"
    assert plan["summary"]["hint"] == "Open image"


def test_a_new_image_named_like_the_open_one_asks_layer_or_sample(client):
    sample = _register(client, "slide.ome.tif")
    tifffile.imwrite(_source() / "slide_he.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    action = _only(_plan(client, "slide_he.ome.tif", datasource=sample))
    assert action["reason"] == "related_to_open"
    assert action["default"] == "layer"
    assert [c["value"] for c in action["choices"]] == ["layer", "new"]


def test_a_project_folder_opens_the_project(client):
    from plexora import paths

    sample = _register(client, "slide.ome.tif")
    folder = str(paths.project_dir(sample))
    action = _only(_plan(client, paths=[folder], target="home"))
    assert action["kind"] == "open_project"
    assert action["sample"] == sample
    assert _only(_plan(client, paths=[folder], datasource=sample))["kind"] == \
        "noop_already_open"


def test_an_existing_image_with_its_mask_opens_and_then_attaches(client):
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "slide.ome.tif", "slide_cellmask.tif",
                         target="home"))
    assert action["kind"] == "open_project"
    assert action["sample"] == sample
    assert action["status"] == "needs_confirm"
    assert [t["kind"] for t in action["then"]] == ["attach_mask"]
    assert action["then"][0]["route"]["json"]["sample"] == sample


def test_image_mask_and_table_with_nothing_open_import_together(client):
    plan = _plan(client, "slide.ome.tif", "slide_cellmask.tif", "slide.csv",
                 target="home")
    action = _only(plan)
    assert action["kind"] == "import_new"
    assert action["route"]["json"]["paths"] == [
        _path("slide.ome.tif"), _path("slide_cellmask.tif"), _path("slide.csv")]
    assert plan["proposal"]["samples"][0]["name"] == "slide"


def test_two_samples_go_to_the_dialog(client):
    tifffile.imwrite(_source() / "tonsil.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    plan = _plan(client, "slide.ome.tif", "tonsil.ome.tif", target="home")
    action = _only(plan)
    assert action["kind"] == "open_dialog"
    assert plan["summary"]["needs_dialog"] is True
    assert len(plan["proposal"]["samples"]) == 2


# -- what cannot be read -------------------------------------------------------

def test_a_missing_file_is_reported_and_the_rest_keep_their_positions(client):
    sample = _register(client, "slide.ome.tif")
    plan = _plan(client, paths=[_path("gone.tif"), _path("slide_cellmask.tif")],
                 datasource=sample)
    assert plan["unrecognised"][0]["pick"] == 0
    assert plan["unrecognised"][0]["code"] == "missing"
    action = _only(plan)
    assert action["picks"] == [1]
    assert action["kind"] == "attach_mask"


def test_an_unsupported_file_is_listed_without_blocking_the_others(client):
    sample = _register(client, "slide.ome.tif")
    plan = _plan(client, "notes.xyz", "slide.csv", datasource=sample)
    assert [u["pick"] for u in plan["unrecognised"]] == [0]
    assert [a["kind"] for a in plan["actions"]] == ["attach_table"]


def test_actions_run_names_then_cells_then_mask(client):
    sample = _register(client, "slide.ome.tif")
    plan = _plan(client, "slide_cellmask.tif", "slide.csv", "markers.csv",
                 datasource=sample)
    assert [a["kind"] for a in plan["actions"]] == [
        "apply_channel_names", "attach_table", "attach_mask"]
    assert [a["id"] for a in plan["actions"]] == ["a0", "a1", "a2"]


# -- plugins and the route ----------------------------------------------------

def test_a_plugin_sniffer_claims_its_own_sheet_before_the_engine(client, monkeypatch):
    monkeypatch.setattr(drop_plan, "_SNIFFERS", [])
    (_source() / "gates.csv").write_text(
        "channel,gate_start,gate_end\nCD3,1,2\n", encoding="utf-8")

    def gates(path, context):
        if path.endswith("gates.csv"):
            return {"kind": "gates", "label": "Gates",
                    "action": {"kind": "upload_gates", "status": "auto",
                               "message": "Load gates"}}
        return None

    drop_plan.register_sniffer(gates)
    sample = _register(client, "slide.ome.tif")
    action = _only(_plan(client, "gates.csv", datasource=sample))
    assert action["kind"] == "upload_gates"
    assert action["status"] == "auto"
    assert action["paths"] == [_path("gates.csv")]


def test_the_route_refuses_an_unknown_sample_and_an_empty_drop(client):
    assert client.post("/drop/inspect", json={
        "paths": [_path("slide.csv")], "datasource": "nope"}).status_code == 404
    assert client.post("/drop/inspect", json={"paths": []}).status_code == 400


# -- the policy, without files -----------------------------------------------

OPEN = {"name": "s", "title": "s", "width": 10, "height": 10, "channelCount": 2,
        "channelNames": ["a", "b"], "genericNames": False, "hasMask": False,
        "hasTable": False, "_mask": None, "_table": None, "_images": set(),
        "_layers": set(), "_names": ["s"]}


def _mask(**fit):
    base = {"compatible": True, "nameMatch": "exact", "alreadyAttached": False,
            "attachedTo": None, "targets": [], "size": [10, 10],
            "imageSize": [10, 10]}
    return {"pick": 0, "path": "/x/s_mask.tif", "kind": "mask",
            "label": "s_mask.tif", "fit": {**base, **fit}}


@pytest.mark.parametrize("fit, status, reason", [
    ({}, "auto", None),
    ({"nameMatch": "prefix"}, "auto", None),
    ({"nameMatch": "none"}, "needs_confirm", "name_mismatch"),
    ({"compatible": None}, "auto", None),
    ({"compatible": None, "nameMatch": "prefix"}, "needs_confirm", "size_unknown"),
    ({"compatible": False}, "needs_confirm", "size_mismatch"),
    ({"compatible": False, "nameMatch": "exact"}, "needs_confirm", "size_mismatch"),
    ({"alreadyAttached": True}, "auto", None),
    ({"attachedTo": "t"}, "needs_confirm", "attached_elsewhere"),
])
def test_decide_mask(fit, status, reason):
    action = drop_plan.decide(_mask(**fit), OPEN)
    assert (action["status"], action["reason"]) == (status, reason)


def test_decide_never_replaces_a_mask_on_its_own():
    action = drop_plan.decide(_mask(), {**OPEN, "hasMask": True})
    assert (action["status"], action["reason"]) == ("needs_confirm", "mask_exists")


@pytest.mark.parametrize("path, names, expected", [
    ("/d/LSP11385_cellmask.tif", ["LSP11385"], "exact"),
    ("/d/LSP11385-nuclei_labels.tif", ["LSP11385"], "exact"),
    ("/d/LSP11385--unmicst_cell.tif", ["LSP11385"], "prefix"),
    ("/d/unmicst-LSP11385/cell.ome.tif", ["LSP11385"], "prefix"),
    ("/d/LSP11386_mask.tif", ["LSP11385"], "none"),
    ("/d/cell.ome.tif", ["LSP11385"], "none"),
])
def test_name_match(path, names, expected):
    assert drop_plan.name_match(path, names) == expected


# -- AnnData spanning several images -----------------------------------------

def _h5ad(name, images):
    anndata = pytest.importorskip("anndata")
    import pandas as pd

    count = 6 * len(images)
    obs = pd.DataFrame({"imageid": [images[i % len(images)] for i in range(count)]},
                       index=[str(i) for i in range(count)])
    adata = anndata.AnnData(X=np.ones((count, 2), dtype=np.float32), obs=obs)
    adata.var_names = ["CD3", "CD8"]
    adata.obsm["spatial"] = np.ones((count, 2), dtype=np.float32)
    path = _source() / name
    adata.write_h5ad(path)
    return str(path)


def test_a_table_of_several_images_attaches_the_open_ones_cells(client):
    sample = _register(client, "slide.ome.tif")
    _h5ad("cohort.h5ad", ["slide", "tonsil", "lung"])
    action = _only(_plan(client, "cohort.h5ad", datasource=sample))
    assert action["kind"] == "attach_table"
    assert action["status"] == "auto"
    assert action["fit"]["multiImage"]["suggested"] == "slide"
    assert action["route"]["json"]["answers"] == {f"subset:{sample}": "slide"}


def test_a_table_of_several_images_none_of_them_this_one_opens_the_dialog(client):
    sample = _register(client, "slide.ome.tif")
    _h5ad("others.h5ad", ["tonsil", "lung"])
    action = _only(_plan(client, "others.h5ad", datasource=sample))
    assert action["kind"] == "open_dialog"
    assert action["reason"] == "subset_unknown"
    assert action["sample"] == sample


# -- a SpatialData store with no image of its own -------------------------------

def _imageless_store(name, tables=("cells",), images=()):
    """Cells (and nothing to draw them in) as SpatialData: what a pipeline
    writes beside the slide it segmented. `images` puts pictures beside them,
    which makes it no longer image-less -- for the tests that need a store
    of several rows."""
    anndata = pytest.importorskip("anndata")
    spatialdata = pytest.importorskip("spatialdata")
    import pandas as pd
    from spatialdata.models import Image2DModel

    def table(seed):
        adata = anndata.AnnData(
            X=np.ones((6, 2), dtype=np.float32),
            obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
            var=pd.DataFrame(index=["CD3", "CD8"]))
        adata.obsm["spatial"] = np.random.default_rng(seed).random(
            (6, 2)).astype(np.float32) * 100
        return adata

    pictures = {i: Image2DModel.parse(np.ones((2, 128, 128), dtype=np.uint16),
                                      dims=("c", "y", "x")) for i in images}
    path = _source() / name
    spatialdata.SpatialData(
        images=pictures,
        tables={t: table(i) for i, t in enumerate(tables)}).write(path)
    return str(path)


def test_a_store_with_no_image_is_offered_to_the_open_sample(client):
    sample = _register(client, "slide.ome.tif")
    store = _imageless_store("cells.zarr")
    plan = _plan(client, paths=[store], datasource=sample)
    action = _only(plan)
    assert action["kind"] == "add_layer"
    assert action["status"] == "needs_confirm"
    assert action["reason"] == "bundle_to_open"
    assert action["route"] == {"method": "POST", "path": "import/layers",
                               "json": {"sample": sample, "paths": [store],
                                        "answers": {}}}
    assert plan["summary"]["hint"] == f"Add as a layer to {sample}"

    # And the route it names attaches the store's cells to that sample.
    response = client.post("/import/layers", json=action["route"]["json"])
    assert response.status_code == 200, response.get_json()
    dataset = Project.load(sample).dataset
    assert (dataset.src, dataset.table) == (store, "cells")


def test_a_store_of_several_tables_leaves_the_choice_to_the_dialog(client):
    sample = _register(client, "slide.ome.tif")
    store = _imageless_store("many.zarr", tables=("cells", "tiles"))
    action = _only(_plan(client, paths=[store], datasource=sample))
    assert action["reason"] == "bundle_to_open"
    # What `addToSample` inspects before posting: a table with no answer is
    # a need, and a need opens the dialog on this sample.
    scoped = import_proposal.inspect_paths([store], sample=sample).samples[0]
    assert [l.role for l in scoped.layers if l.needs] == ["table"]


def test_one_row_of_a_store_is_left_out_and_the_rest_added(client):
    """The ✕ on one row of a store takes that row, not the store: the dialog
    sends `left-out:<pick>`, and the import reads the store without it."""
    sample = _register(client, "slide.ome.tif")
    store = _imageless_store("both.zarr", images=("he",))
    rows = import_proposal.inspect_paths([store], sample=sample).samples[0].layers
    image = next(r for r in rows if r.kind == "image")
    table = next(r for r in rows if r.role == "table")
    assert len(rows) == 2

    answers = {f"left-out:{store}": [image.src]}
    left = import_proposal.inspect_paths(
        [store], sample=sample, answers=answers).samples[0].layers
    assert [r.src for r in left] == [table.src]

    response = client.post("/import/layers", json={
        "sample": sample, "paths": [store], "answers": answers})
    assert response.status_code == 200, response.get_json()
    project = Project.load(sample)
    assert project.dataset.src == store
    assert [l.src for l in project.spatial_layers] == []


def test_leaving_out_a_stores_table_takes_its_question_with_it(client):
    store = _imageless_store("rows.zarr", tables=("cells", "tiles"), images=("he",))
    whole = import_proposal.inspect_paths([store]).samples[0]
    table = next(r for r in whole.layers if r.role == "table")
    assert any(q.scope == f"layer:{table.id}" for q in whole.questions)

    left = import_proposal.inspect_paths(
        [store], answers={f"left-out:{store}": [table.src]}).samples[0]
    assert [r.role for r in left.layers] == ["image"]
    assert not [q for q in left.questions if q.scope == f"layer:{table.id}"]


def test_a_stores_reference_image_is_never_left_out_on_its_own(client):
    store = _imageless_store("ref.zarr", images=("he",))
    reference = next(r for r in import_proposal.inspect_paths([store])
                     .samples[0].layers if r.reference)
    left = import_proposal.inspect_paths(
        [store], answers={f"left-out:{store}": [reference.src]}).samples[0]
    assert reference.src in [r.src for r in left.layers]


def _bundle(**fit):
    base = {"existing": None, "withImage": False, "brings": ["table"],
            "questions": 0, "onePress": True}
    return {"pick": 0, "path": "/x/s.zarr", "kind": "bundle",
            "label": "s.zarr", "fit": {**base, **fit}}


@pytest.mark.parametrize("fit, open_, kind, reason", [
    ({}, OPEN, "add_layer", "bundle_to_open"),
    ({"questions": 1}, OPEN, "add_layer", "bundle_to_open"),
    ({}, {**OPEN, "hasTable": True}, "add_layer", "bundle_replaces"),
    ({"brings": ["mask"]}, {**OPEN, "hasMask": True}, "add_layer", "bundle_replaces"),
    ({"brings": ["mask"]}, {**OPEN, "hasTable": True}, "add_layer", "bundle_to_open"),
    # A store with its own image is a sample of its own, as before.
    ({"withImage": True}, OPEN, "import_new", None),
    # With nothing open there is nothing to add it to.
    ({}, None, "import_new", None),
])
def test_decide_imageless_bundle(fit, open_, kind, reason):
    action = drop_plan.decide(_bundle(**fit), open_)
    assert (action["kind"], action["reason"]) == (kind, reason)
    if kind == "add_layer":
        assert action["status"] == "needs_confirm"
        assert action["effects"]["reload"] is ("mask" in _bundle(**fit)["fit"]["brings"])


# -- a failure part-way ---------------------------------------------------------

def test_a_table_that_fails_puts_back_the_mask_attached_with_it(client, monkeypatch):
    """`register_layers` attaches the mask, then the table. A table refused
    after the mask went in must not leave the sample with a new mask and its
    old cells."""
    from plexora.server.models import import_sample
    from plexora.server.routes import import_routes

    sample = _register(client, "slide.ome.tif", "slide_cellmask.tif")
    before = Project.load(sample).segmentation.source

    def refuse(*args, **kwargs):
        raise ValueError("refused")

    monkeypatch.setattr(import_routes, "replace_project_data", refuse)
    with pytest.raises(ValueError):
        import_sample.add_layers(sample, [_path("slide_nuclei_mask.tif"), _path("slide.csv")],
                                 answers={})
    assert Project.load(sample).segmentation.source == before
