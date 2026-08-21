"""Editing a project without destroying it.

The regression this file exists for: the old edit page read every project as if
its source file were a CSV, rendered the CSV column-matching screen, and posted
to a handler that rebuilt the config entry from `{}`. That discarded the data
type and the AnnData read spec and repointed the source at a CSV that did not
exist -- so editing an AnnData project bricked it, and reported success with
HTTP 200 while doing so.

The tests below pin the three properties that stop it happening again: the page
is generated from the record rather than assuming a format, saving merges, and
a failure is an error the user can see.
"""

import json

import numpy as np
import pandas as pd
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import centroid_tiles, data_model, database_model
from plexora.server.models.project import Project
from plexora.server.routes import import_routes, page_routes, project_routes


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    for module in (plexora, data_model, import_routes, database_model,
                   centroid_tiles, page_routes, project_routes):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", tmp_path)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", config_path)
    config_path.write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def _image(tmp_path):
    path = tmp_path / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _csv(tmp_path, name="cells.csv"):
    path = tmp_path / name
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": np.linspace(0, 3, 4),
    }).write_csv(path)
    return path


def _h5ad(tmp_path, name="cells.h5ad"):
    import anndata

    path = tmp_path / name
    adata = anndata.AnnData(
        X=np.random.default_rng(0).random((6, 3)).astype(np.float32),
        obs=pd.DataFrame({"region_note": ["a"] * 6},
                         index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=["MarkerA", "MarkerB", "MarkerC"]),
    )
    adata.obsm["spatial"] = (
        np.random.default_rng(1).random((6, 2)).astype(np.float32) * 100)
    adata.write_h5ad(path)
    return path


def _h5ad_with_real_obs(tmp_path, name="annotated.h5ad"):
    """An .h5ad whose obs carries the columns a user would actually want to
    pick: the segmentation mask's own label values, and coordinates in microns
    alongside the obsm centroids the importer auto-detects."""
    import anndata

    path = tmp_path / name
    adata = anndata.AnnData(
        X=np.random.default_rng(0).random((6, 3)).astype(np.float32),
        obs=pd.DataFrame(
            {
                # Deliberately not 1..n: a positional row index would silently
                # "work" against a mask whose labels happened to start at 0.
                "MaskLabel": np.arange(101, 107, dtype=np.int32),
                "X_um": np.linspace(10.0, 60.0, 6),
                "Y_um": np.linspace(20.0, 70.0, 6),
                # A column literally called "id" occurs in real exemplar data
                # and collides with one the adapter synthesizes -- offered like
                # any other, and refused when actually chosen.
                "id": np.arange(6, dtype=np.int32),
                # Not "sample": that name makes the importer treat the file as
                # spanning several images and demand a subset choice.
                "run_label": ["r1"] * 6,
            },
            index=[f"cell_{i}" for i in range(6)],
        ),
        var=pd.DataFrame(index=["MarkerA", "MarkerB", "MarkerC"]),
    )
    adata.obsm["spatial"] = (
        np.random.default_rng(1).random((6, 2)).astype(np.float32) * 100)
    # A second (n, 2) float32 array, indistinguishable from the first by shape
    # or dtype -- the shape a real store has, where `spatial` sits beside
    # `X_umap`. Values are recognisable so a test can tell which one was read.
    adata.obsm["elsewhere"] = np.stack([
        np.linspace(100.0, 600.0, 6), np.linspace(700.0, 1200.0, 6)],
        axis=1).astype(np.float32)
    adata.write_h5ad(path)
    return path


def _import(client, tmp_path, name, data=None, mask=None):
    form = {"name": name, "image_file": str(_image(tmp_path))}
    if data:
        form["data_file"] = str(data)
    if mask:
        form["label_file"] = str(mask)
    response = client.post("/import", data=form)
    assert response.status_code == 302, response.get_data(as_text=True)
    return Project.load(name)


def _element_attrs(body, element_id):
    """The attributes a browser would read off one element.

    Parsed rather than pattern-matched on purpose: the failure this guards
    against is invisible to a regex over the source, because the bytes are all
    there -- it is the parser that stops early.
    """
    from html.parser import HTMLParser

    found = {}

    class _Find(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") == element_id:
                found.update(attrs)

    parser = _Find()
    parser.feed(body.decode("utf-8"))
    assert found, f"no element with id={element_id!r} in the page"
    return found


# --------------------------------------------------------------------------
# The regression
# --------------------------------------------------------------------------

def test_saving_an_anndata_project_unchanged_leaves_it_loadable(isolate, tmp_path):
    """The exact shape of the old bug. Open the edit page, save it without
    touching anything, and the project must still be an AnnData project that
    loads -- not a CSV entry pointing at a file that was never written."""
    before = _import(isolate, tmp_path, "ann_ds", data=_h5ad(tmp_path))
    assert before.dataset.type == "anndata"

    assert isolate.get("/edit_config/ann_ds").status_code == 200
    response = isolate.post("/project/ann_ds", json={})

    assert response.status_code == 200
    after = Project.load("ann_ds")
    assert after.dataset.type == "anndata"
    assert after.dataset.src == before.dataset.src
    assert after.dataset.coordinates == before.dataset.coordinates
    assert after.image.kind == before.image.kind
    # And it still loads, which is the thing the user actually lost.
    data_model.load_datasource("ann_ds", reload=True)
    assert data_model.datasource is not None


def test_saving_an_image_only_project_does_not_500(isolate, tmp_path):
    """The old loader indexed featureData[0] with no guard, so opening the edit
    page for a project registered from an image alone was an IndexError."""
    _import(isolate, tmp_path, "img_ds")

    assert isolate.get("/edit_config/img_ds").status_code == 200
    assert isolate.post("/project/img_ds", json={}).status_code == 200
    assert Project.load("img_ds").dataset is None


def test_a_csv_project_survives_an_unchanged_save(isolate, tmp_path):
    before = _import(isolate, tmp_path, "csv_ds", data=_csv(tmp_path))

    isolate.post("/project/csv_ds", json={})

    after = Project.load("csv_ds")
    assert after.dataset.type == "csv"
    assert after.dataset.src == before.dataset.src


# --------------------------------------------------------------------------
# The cell layer: a default until somebody actually chooses
# --------------------------------------------------------------------------

def _mask(tmp_path, name="mask.tif"):
    path = tmp_path / name
    tifffile.imwrite(path, np.arange(256 * 256, dtype=np.uint32).reshape(256, 256))
    return path


def test_an_unchanged_save_does_not_record_a_cell_layer(isolate, tmp_path):
    """The bug, exactly. Every other field on the edit page is sent only when
    it differs; this one was sent whenever it had a value, so saving the page
    for any reason at all wrote down an override. The moment that mattered was
    attaching a mask from this page: the select was rendered before the mask
    existed, so it could only offer centroids, and the save that attached the
    mask froze centroids in the same breath. Every tool that colours cells then
    went on drawing points, and nothing said why."""
    _import(isolate, tmp_path, "layered", data=_csv(tmp_path), mask=_mask(tmp_path))
    before = Project.load("layered")
    assert before.cell_layer_choice is None
    assert before.cell_layer == "segmentation"

    assert isolate.post("/project/layered", json={}).status_code == 200

    after = Project.load("layered")
    assert after.cell_layer_choice is None, "an untouched select must not become a choice"
    assert after.cell_layer == "segmentation"


def test_a_chosen_cell_layer_is_recorded_and_can_be_taken_back(isolate, tmp_path):
    """Choosing still works, and -- the half that did not exist -- so does
    un-choosing. An override outlives the state it was made in, so without a
    way back a project pinned to centroids stays pinned however good its mask
    later becomes."""
    _import(isolate, tmp_path, "layered", data=_csv(tmp_path), mask=_mask(tmp_path))

    assert isolate.post("/project/layered", json={"cellLayer": "centroids"}).status_code == 200
    assert Project.load("layered").cell_layer_choice == "centroids"
    assert Project.load("layered").cell_layer == "centroids"

    assert isolate.post("/project/layered", json={"cellLayer": ""}).status_code == 200
    assert Project.load("layered").cell_layer_choice is None
    assert Project.load("layered").cell_layer == "segmentation"


def test_the_page_says_whether_the_layer_was_chosen_or_defaulted(isolate, tmp_path):
    """Two different facts, and the select needs both: what the project
    resolves to, and whether that was a decision. Without the second there is
    no option for "leave it alone" and no way to tell an untouched form from a
    deliberate one."""
    _import(isolate, tmp_path, "layered", data=_csv(tmp_path), mask=_mask(tmp_path))
    body = isolate.get("/edit_config/layered").data

    select = _element_attrs(body, "edit_cell_layer")
    assert select["data-stored"] == "", "nothing has been chosen yet"
    assert b"Automatic" in body

    isolate.post("/project/layered", json={"cellLayer": "centroids"})
    select = _element_attrs(isolate.get("/edit_config/layered").data, "edit_cell_layer")
    assert select["data-stored"] == "centroids"


# --------------------------------------------------------------------------
# The page is generated from the record
# --------------------------------------------------------------------------

def test_the_project_record_reaches_the_browser_whole(isolate, tmp_path):
    """Save did nothing at all, and this is why.

    `tojson` escapes < > & and ' -- not the double quotes JSON is built out of.
    In a double-quoted attribute the value therefore ended at the record's
    first key and the rest spilled into the tag as garbage attributes. `id`
    came earlier and still parsed, and every section is rendered by Jinja, so
    the page looked completely normal; projectEdit.js threw on
    JSON.parse("{"), and the Save button never got a click handler.

    Parsed here the way a browser parses it, because the raw bytes of a broken
    page and a working one differ by two characters.
    """
    _import(isolate, tmp_path, "ann_ds", data=_h5ad(tmp_path))

    attrs = _element_attrs(isolate.get("/edit_config/ann_ds").data, "project-edit")
    record = json.loads(attrs["data-project"])

    assert record["name"] == "ann_ds"
    assert record["has"]["data"] is True
    # The keys projectEdit.js reads before it wires the button. Any of them
    # missing is the same class of silent failure.
    assert {"roleLabels", "editableRoles", "coordinateOptions"} <= set(record)


def test_a_tools_requirement_list_reaches_the_browser_whole(isolate, tmp_path):
    """The same attribute, on the path that carries the no-JavaScript twin of
    the requirements modal. It is written by the same filter and was broken the
    same way."""
    _import(isolate, tmp_path, "csv_ds", data=_csv(tmp_path))

    body = isolate.get("/edit_config/csv_ds?needs=gating").data
    attrs = _element_attrs(body, "project-edit")
    if "data-needs" not in attrs:
        pytest.skip("no gating plugin on the path")
    assert json.loads(attrs["data-needs"])["tool"] == "gating"


def test_the_page_offers_the_classifier_only_for_csv(isolate, tmp_path):
    _import(isolate, tmp_path, "csv_ds", data=_csv(tmp_path))
    _import(isolate, tmp_path, "ann_ds", data=_h5ad(tmp_path))

    assert b'id="edit_classifier"' in isolate.get("/edit_config/csv_ds").data
    assert b'id="edit_classifier"' not in isolate.get("/edit_config/ann_ds").data


def test_an_image_only_project_is_not_shown_column_controls(isolate, tmp_path):
    """"If the user supplied only an image, do not populate the page with
    unrelated segmentation or plugin-specific fields."" """
    _import(isolate, tmp_path, "img_ds")

    body = isolate.get("/edit_config/img_ds").data
    assert b'id="edit_classifier"' not in body
    assert b'id="edit_roles"' not in body
    # The two things it can still be given are offered. The data field is a
    # mount point rather than a bare input -- the shared control fills it in,
    # because a path alone does not name a table inside a .zarr store.
    assert b'id="edit_data_field"' in body
    assert b'id="edit_segmentation"' in body


def test_an_anndata_project_answers_its_roles_with_the_files_own_obs_columns(isolate, tmp_path):
    """The id and coordinate columns are created by the adapter, so naming one
    in the role alone would point it at a column the loaded table does not
    have. The question is asked about obs and answered into the read spec,
    which is what lets a user say the mask's label values live in this column."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad(tmp_path))
    _import(isolate, tmp_path, "csv_ds", data=_csv(tmp_path))

    ann = project_routes._describe(Project.load("ann_ds"))
    # Every role is reassignable now, and the options are obs, unfiltered.
    assert {"cell_id", "x", "y", "image_id"} <= set(ann["editableRoles"])
    assert ann["roleColumns"] == list(Project.load("ann_ds").dataset.obs_columns)
    assert ann["roleColumns"]
    # Not the adapter's own synthesized columns, which is all the old list held.
    assert "id" not in ann["roleColumns"]

    # For a CSV the roles name table columns directly, so the list is those.
    csv = project_routes._describe(Project.load("csv_ds"))
    assert "cell_id" in csv["editableRoles"]
    assert "CellID" in csv["roleColumns"]


# --------------------------------------------------------------------------
# Saving merges, and says so when it cannot
# --------------------------------------------------------------------------

def test_a_role_can_be_reassigned_without_touching_anything_else(isolate, tmp_path):
    before = _import(isolate, tmp_path, "csv_ds", data=_csv(tmp_path))

    isolate.post("/project/csv_ds", json={"roles": {"celltype": "CD3"}})

    after = Project.load("csv_ds")
    assert after.roles.celltype == "CD3"
    assert after.roles.cell_id == before.roles.cell_id
    assert after.dataset.src == before.dataset.src
    assert after.columns.markers == before.columns.markers


def test_naming_an_obs_column_as_the_cell_id_reaches_the_loaded_table(isolate, tmp_path):
    """The answer has to go into the read spec, not just the role: the adapter
    builds the table, so a role naming an obs column would otherwise point at a
    column that is not in it. This is the only way to say "the segmentation
    mask's label values live here", and without it an overlay cannot line up."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={"roles": {"cell_id": "MaskLabel"}})

    assert response.status_code == 200, response.get_json()
    after = Project.load("ann_ds")
    assert after.dataset.obs_id_field == "MaskLabel"
    assert after.roles.cell_id == "MaskLabel"
    # And the column is really there, still numeric -- the centroid cache packs
    # the cell id into a uint32, which a stringified integer only survives by
    # being parsed back out.
    data_model.load_datasource("ann_ds", reload=True)
    frame = data_model.get_datasource_df()
    assert frame["MaskLabel"].to_list() == list(range(101, 107))


def test_a_fresh_import_has_not_answered_the_cell_id_question(isolate, tmp_path):
    """What a newly imported AnnData project owes. The importer records the
    adapter's positional "id" as the cell_id role because that is the column
    the table comes out with -- and reading that back as an answer is what let
    a project open gating with a row-number cell id nobody chose, while the
    mask it drew on was labelled from obs."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))
    project = Project.load("ann_ds")

    assert project.roles.cell_id == "id"
    assert project.dataset.obs_id_field is None
    assert project.dataset.row_number_ids is False
    needs = isolate.get("/ann_ds/tools/gating/requirements").get_json()
    assert "role:cell_id" in [r["key"] for r in needs["missing"]]


def test_choosing_to_number_the_rows_answers_the_cell_id_question(isolate, tmp_path):
    """The answer for a file with no id column, and it has to be storable as
    something. Left as a blank role it is indistinguishable from the importer's
    default, so the question would either block forever or be treated as
    settled by a value nobody looked at."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={"row_number_ids": True,
                                                     "roles": {"cell_id": None}})

    assert response.status_code == 200, response.get_json()
    after = Project.load("ann_ds")
    assert after.dataset.row_number_ids is True
    assert after.dataset.obs_id_field is None
    needs = isolate.get("/ann_ds/tools/gating/requirements").get_json()
    assert "role:cell_id" not in [r["key"] for r in needs["missing"]]


def test_naming_an_id_column_retracts_numbering_the_rows(isolate, tmp_path):
    """Two answers to one question; the later one has to displace the earlier,
    or the project would number its rows while naming the column it reads the
    identifier from."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))
    isolate.post("/project/ann_ds", json={"row_number_ids": True})

    isolate.post("/project/ann_ds", json={"roles": {"cell_id": "MaskLabel"}})

    after = Project.load("ann_ds")
    assert after.dataset.obs_id_field == "MaskLabel"
    assert after.dataset.row_number_ids is False


def test_naming_obs_coordinate_columns_repoints_the_read_spec(isolate, tmp_path):
    """x/y keep naming the adapter's own output columns, because that is what
    it emits whichever obs columns it read them from."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))
    assert Project.load("ann_ds").dataset.coordinates["obsm_key"] == "spatial"

    isolate.post("/project/ann_ds", json={"roles": {"x": "X_um", "y": "Y_um"}})

    after = Project.load("ann_ds")
    assert after.dataset.coordinates == {
        "source": "obs", "x_column": "X_um", "y_column": "Y_um"}
    assert (after.roles.x, after.roles.y) == ("X", "Y")
    data_model.load_datasource("ann_ds", reload=True)
    assert data_model.get_datasource_df()["X"].to_list() == pytest.approx(
        [10.0, 20.0, 30.0, 40.0, 50.0, 60.0])


def test_half_a_coordinate_is_rejected_rather_than_half_applied(isolate, tmp_path):
    """One axis from obs and the other from obsm is not a position."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={"roles": {"x": "X_um"}})

    assert response.status_code == 400
    assert "Y" in response.get_json()["error"]
    assert Project.load("ann_ds").dataset.coordinates["obsm_key"] == "spatial"


def test_choosing_an_obsm_array_reaches_the_loaded_table(isolate, tmp_path):
    """The answer the two column selects could never express. Picking the array
    has to change which numbers the viewer places cells at -- storing it without
    that is the failure the coordinate question exists to end."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={
        "coordinates": {"source": "obsm", "obsm_key": "elsewhere"}})

    assert response.status_code == 200, response.get_json()
    after = Project.load("ann_ds")
    assert after.dataset.coordinates == {"source": "obsm", "obsm_key": "elsewhere"}
    data_model.load_datasource("ann_ds", reload=True)
    assert data_model.get_datasource_df()["X"].to_list() == pytest.approx(
        [100.0, 200.0, 300.0, 400.0, 500.0, 600.0])


def test_the_coordinate_question_offers_what_the_file_carries(isolate, tmp_path):
    """Both arrays and every obs column, so the choice is the user's. The obsm
    list used to be unreachable entirely -- the page could only show the
    detected key as a label on a blank option."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    options = Project.load("ann_ds").coordinate_options

    assert {e["name"] for e in options["obsm"]} == {"spatial", "elsewhere"}
    assert "X_um" in options["obs"] and "Y_um" in options["obs"]
    assert options["current"] == {"source": "obsm", "obsm_key": "spatial"}


def test_an_obsm_array_the_file_lacks_is_refused_by_the_route(isolate, tmp_path):
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={
        "coordinates": {"source": "obsm", "obsm_key": "X_umap"}})

    assert response.status_code == 400
    assert "X_umap" in response.get_json()["error"]
    assert Project.load("ann_ds").dataset.coordinates["obsm_key"] == "spatial"


def test_an_answer_the_file_cannot_be_read_with_is_put_back(isolate, tmp_path):
    """An answer is only accepted once the file loads with it. Storing one the
    adapter refuses would leave a project that no longer opens at all -- a far
    worse outcome than the question the user was trying to answer."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))

    response = isolate.post("/project/ann_ds", json={"roles": {"cell_id": "id"}})

    assert response.status_code == 400
    assert "id" in response.get_json()["error"]
    after = Project.load("ann_ds")
    assert after.dataset.obs_id_field is None
    # And it still loads, which is the thing a bad pick used to cost.
    data_model.load_datasource("ann_ds", reload=True)
    assert data_model.get_datasource_df() is not None


def test_the_role_answers_are_reported_in_the_vocabulary_they_are_asked_in(isolate, tmp_path):
    """A form prefills from these. Reporting the adapter's synthesized 'id' and
    'X' back is what made the old modal ask which of Plexora's own columns held
    the cell id -- a question with one possible answer, and never the one the
    user wanted."""
    _import(isolate, tmp_path, "ann_ds", data=_h5ad_with_real_obs(tmp_path))
    project = Project.load("ann_ds")

    # Nothing chosen yet, and what happens instead is named rather than blank.
    assert project.role_answers.get("cell_id") is None
    assert project.role_defaults["cell_id"].startswith("Row number")
    # x/y are not role questions for these formats -- an obsm array holds both
    # axes, so they are asked as one coordinate question with its own answer
    # shape. Reporting them here as two blank column roles is what let the
    # obsm choice be shown only as a label on an option nobody could pick.
    assert "x" not in project.role_answers
    assert "x" not in project.role_defaults

    isolate.post("/project/ann_ds", json={"roles": {"cell_id": "MaskLabel"}})

    assert Project.load("ann_ds").role_answers["cell_id"] == "MaskLabel"


def test_swapping_the_data_file_re_detects_its_format(isolate, tmp_path):
    """Swapping a CSV for an .h5ad is an ordinary thing to want, and is exactly
    what the old path silently corrupted."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    response = isolate.post("/project/proj", json={"data": str(_h5ad(tmp_path))})

    assert response.status_code == 200
    after = Project.load("proj")
    assert after.dataset.type == "anndata"
    assert after.dataset.coordinates.get("obsm_key") == "spatial"


def test_clearing_the_data_file_leaves_the_image(isolate, tmp_path):
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    isolate.post("/project/proj", json={"data": ""})

    after = Project.load("proj")
    assert after.dataset is None
    assert after.image.src


def test_a_bad_path_is_reported_as_an_error_the_client_can_see(isolate, tmp_path):
    """The old handler swallowed the exception and returned success=False with
    HTTP 200 and no message, and the client navigated away regardless."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    response = isolate.post("/project/proj", json={"data": "/nope/missing.h5ad"})

    assert response.status_code == 400
    assert "missing.h5ad" in response.get_json()["error"]
    # And the project is untouched.
    assert Project.load("proj").dataset.type == "csv"


def test_an_unknown_project_is_a_404(isolate):
    assert isolate.post("/project/gone", json={}).status_code == 404


def test_the_edit_page_redirects_rather_than_erroring_on_a_stale_link(isolate):
    response = isolate.get("/edit_config/gone")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/open_project")


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

def test_delete_removes_the_project_and_its_directory(isolate, tmp_path):
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))
    assert (tmp_path / "proj").is_dir()

    assert isolate.post("/project/proj/delete").status_code == 200

    assert Project.find("proj") is None
    assert not (tmp_path / "proj").exists()


def test_delete_is_not_reachable_by_a_get(isolate, tmp_path):
    """It used to be a link to GET /delete/<name> -- an irreversible rmtree any
    crawler, prefetcher or stale bookmark could follow."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    assert isolate.get("/project/proj/delete").status_code == 405
    assert Project.find("proj") is not None


# --------------------------------------------------------------------------
# Which expression matrix
# --------------------------------------------------------------------------

def _h5ad_with_layers(tmp_path, name="layered.h5ad"):
    """The ordinary shape of a processed .h5ad: raw counts in one matrix and a
    log-transformed copy in another."""
    import anndata

    path = tmp_path / name
    counts = np.random.default_rng(2).integers(0, 500, (6, 3)).astype(np.float32)
    adata = anndata.AnnData(
        X=counts,
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=["MarkerA", "MarkerB", "MarkerC"]),
        layers={"log1p": np.log1p(counts)},
    )
    adata.obsm["spatial"] = (
        np.random.default_rng(3).random((6, 2)).astype(np.float32) * 100)
    adata.write_h5ad(path)
    return path


def test_the_inspection_reports_which_matrices_the_file_holds(isolate, tmp_path):
    """The import form does not ask which matrix to read -- that is a plugin's
    requirement -- but the inspection still has to report them, because this is
    where they get recorded onto the project. Without that the modal would have
    to reopen the file to offer the choice."""
    layered = isolate.post("/inspect_data",
                           json={"path": str(_h5ad_with_layers(tmp_path))}).get_json()
    plain = isolate.post("/inspect_data",
                         json={"path": str(_h5ad(tmp_path))}).get_json()

    assert layered["layers"] == ["log1p"]
    assert plain["layers"] == []


def test_a_chosen_layer_is_what_gets_read(isolate, tmp_path):
    """Not a preference recorded and ignored: the read spec is what the adapter
    opens, so this is the difference between thresholding raw counts and
    thresholding log values."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))
    isolate.post("/project/proj", json={"data": str(_h5ad_with_layers(tmp_path)),
                                        "features_layer": "layer:log1p"})

    assert Project.load("proj").dataset.features == {"source": "layer", "layer": "log1p"}


def test_naming_a_layer_the_file_does_not_have_is_an_error(isolate, tmp_path):
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    response = isolate.post("/project/proj", json={
        "data": str(_h5ad_with_layers(tmp_path)), "features_layer": "layer:nope"})

    assert response.status_code == 400
    assert "nope" in response.get_json()["error"]
    assert Project.load("proj").dataset.type == "csv"


def test_choosing_the_main_matrix_is_recorded_as_such(isolate, tmp_path):
    """"X" is a real answer to the picker, not the absence of one."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))
    isolate.post("/project/proj", json={"data": str(_h5ad_with_layers(tmp_path)),
                                        "features_layer": "X"})

    assert Project.load("proj").dataset.features == {"source": "X"}


def _store_with_two_tables(tmp_path, name="many.zarr"):
    import anndata
    import spatialdata

    def one(seed):
        counts = np.random.default_rng(seed).integers(1, 50, (6, 3)).astype(np.float32)
        a = anndata.AnnData(
            X=counts,
            obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
            var=pd.DataFrame(index=["CD3", "CD8", "DNA"]),
            layers={"log1p": np.log1p(counts)},
        )
        a.obsm["spatial"] = (
            np.random.default_rng(seed + 1).random((6, 2)).astype(np.float32) * 100)
        return a

    path = tmp_path / name
    spatialdata.SpatialData(tables={"cells": one(0), "other": one(5)}).write(path)
    return path


def test_a_multi_table_store_reports_nothing_about_a_table_until_one_is_named(
        isolate, tmp_path):
    """The regression. Everything below the table picker describes a table, so a
    store with several could report none of it -- and the form, having shown the
    table picker, never re-inspected. The subset question went unasked (a store
    spanning several images loaded all of them at once), and the chosen table's
    layers and obs columns were never recorded, which is what the requirements
    modal later needs to ask about at all."""
    store = _store_with_two_tables(tmp_path)

    before = isolate.post("/inspect_data", json={"path": str(store)}).get_json()
    after = isolate.post("/inspect_data",
                         json={"path": str(store), "table": "cells"}).get_json()

    assert [t["name"] for t in before["tables"]] == ["cells", "other"]
    assert before["layers"] == []      # nothing to say until a table is named
    assert after["layers"] == ["log1p"]


def test_a_table_the_store_does_not_have_is_not_taken_at_its_word(isolate, tmp_path):
    """The table arrives from a form post, so a name that is not in the store
    falls back to asking rather than being handed to the reader."""
    store = _store_with_two_tables(tmp_path)

    result = isolate.post("/inspect_data",
                          json={"path": str(store), "table": "nope"}).get_json()

    assert result["ok"] is True
    assert result["layers"] == []


def test_a_project_reading_the_wrong_matrix_can_be_repointed_without_reimporting(
        isolate, tmp_path):
    """What the bug above left behind: projects already imported on X. The edit
    page has to offer the choice even though nothing recorded which matrices the
    file holds, or the one control that fixes them is the one they never show."""
    from dataclasses import replace as dc_replace

    store = _store_with_two_tables(tmp_path)
    isolate.post("/import", data={"name": "raw", "image_file": str(_image(tmp_path)),
                                  "data_file": str(store), "data_table": "cells"})
    # An entry as it was written before the matrices were recorded.
    Project.mutate("raw", lambda p: p.patch(dataset=dc_replace(p.dataset, layers=())))

    described = project_routes._describe(Project.load("raw"))
    assert described["has"]["features"] is True
    assert described["data"]["layers"] == ["log1p"]
    assert described["featureSource"] == "X"

    assert isolate.post("/project/raw",
                        json={"features_layer": "layer:log1p"}).status_code == 200

    after = Project.load("raw")
    assert after.dataset.features == {"source": "layer", "layer": "log1p"}
    # Backfilled on the way through, so the next save validates against a real
    # list rather than an empty one.
    assert after.dataset.layers == ("log1p",)


def test_repointing_at_a_matrix_the_file_lacks_is_refused(isolate, tmp_path):
    store = _store_with_two_tables(tmp_path)
    isolate.post("/import", data={"name": "raw", "image_file": str(_image(tmp_path)),
                                  "data_file": str(store), "data_table": "cells"})

    response = isolate.post("/project/raw", json={"features_layer": "layer:nope"})

    assert response.status_code == 400
    assert "nope" in response.get_json()["error"]
    assert Project.load("raw").dataset.features == {"source": "X"}


# --------------------------------------------------------------------------
# Log-transforming what is read
# --------------------------------------------------------------------------

def _cd3_max(name="proj"):
    data_model.load_datasource(name, reload=True)
    return float(data_model.get_datasource_df()["CD3"].max())


def _counts_h5ad(tmp_path, name="counts.h5ad"):
    """Raw counts and nothing else -- no layer to switch to, which is exactly
    the file the log switch exists for."""
    import anndata

    path = tmp_path / name
    counts = np.random.default_rng(4).integers(20, 500, (6, 3)).astype(np.float32)
    adata = anndata.AnnData(
        X=counts,
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=["CD3", "CD8", "DNA"]),
    )
    adata.obsm["spatial"] = (
        np.random.default_rng(5).random((6, 2)).astype(np.float32) * 100)
    adata.write_h5ad(path)
    return path


def test_the_log_switch_is_offered_even_when_there_is_no_layer_to_pick(
        isolate, tmp_path):
    """Two halves of one question, and they are not offered together. A file
    with a single matrix has nothing to choose between, but whether its values
    are counts is still unanswered and still decides what a threshold means."""
    _import(isolate, tmp_path, "proj", data=_counts_h5ad(tmp_path))

    described = project_routes._describe(Project.load("proj"))

    assert described["has"]["features"] is True
    assert described["data"]["layers"] == []
    assert described["featureLog"] is False


def test_a_csv_project_is_offered_the_log_switch_but_no_matrix_list(
        isolate, tmp_path):
    """A CSV has one table of numbers, so there is nothing to pick between --
    but a quantification CSV is the format most likely to arrive as raw
    intensities, and that question is as open here as anywhere. The section
    used to be withheld from CSV entirely, which left the transform
    unreachable: not on this page, and not in the requirements modal either."""
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))

    described = project_routes._describe(Project.load("proj"))

    assert described["has"]["features"] is True
    assert described["featureOptions"] == []
    assert described["data"]["layers"] == []


def test_turning_on_the_log_transform_changes_the_values_that_are_read(
        isolate, tmp_path):
    """Recorded and applied, not recorded and ignored: is_transformed is what
    the adapter reads to decide whether to log1p on the way in."""
    _import(isolate, tmp_path, "proj", data=_counts_h5ad(tmp_path))
    assert _cd3_max() > 20

    response = isolate.post("/project/proj", json={"features_log": True})

    assert response.status_code == 200
    assert Project.load("proj").dataset.is_transformed is True
    assert _cd3_max() < 10


def test_turning_the_log_transform_back_off_restores_the_raw_values(
        isolate, tmp_path):
    """False has to be an answer rather than an absent key, or the switch would
    be one-way -- a user who logged already-logged data could never undo it."""
    _import(isolate, tmp_path, "proj", data=_counts_h5ad(tmp_path))
    isolate.post("/project/proj", json={"features_log": True})

    isolate.post("/project/proj", json={"features_log": False})

    assert Project.load("proj").dataset.is_transformed is False
    assert _cd3_max() > 20


def test_the_matrix_and_the_log_switch_compose(isolate, tmp_path):
    """Neither replaces the other: one says which numbers, the other says what
    to do with them on the way in."""
    _import(isolate, tmp_path, "proj", data=_h5ad_with_layers(tmp_path))

    isolate.post("/project/proj",
                 json={"features_layer": "layer:log1p", "features_log": True})

    project = Project.load("proj")
    assert project.dataset.features == {"source": "layer", "layer": "log1p"}
    assert project.dataset.is_transformed is True


def test_swapping_a_csv_for_an_h5ad_rebuilds_the_spatial_index_from_the_new_table(
        isolate, tmp_path):
    """The reload used to build the KD tree BEFORE re-reading the table, from
    whichever table was loaded before it. A same-name reload skips
    load_ball_tree's own refresh (`source` already matches), so it indexed the
    table it was in the middle of replacing -- and a CSV's X_centroid/Y_centroid
    do not exist in the .h5ad replacing it, so the save died on ColumnNotFound
    rather than saving. Every path that changes what a project reads is a
    same-name reload, which is what made this reachable from the edit page, the
    requirements modal and the matrix picker alike.
    """
    _import(isolate, tmp_path, "proj", data=_csv(tmp_path))
    # The precondition, made explicit rather than left to whatever a previous
    # test left in this module's globals: the CSV is the loaded table and
    # `source` already names this project, which is what makes load_ball_tree
    # skip its own refresh below.
    data_model.load_datasource("proj", reload=True)
    assert "X_centroid" in data_model.get_datasource_df().columns

    response = isolate.post("/project/proj",
                            json={"data": str(_h5ad_with_layers(tmp_path))})

    assert response.status_code == 200, response.get_json()
    assert Project.load("proj").roles.x == "X"
    # Built, and built from the new table: the .h5ad's coordinates run to ~100
    # and the CSV's to 4, so a tree still holding the old points could not
    # return a neighbour anywhere near here.
    tree = data_model.get_current_ball_tree()
    assert tree is not None
    assert tree.data.shape[0] == 6
