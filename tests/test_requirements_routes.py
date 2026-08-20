"""Asking for what a plugin needs, and never asking twice.

A plugin declares its requirements; core works out which are unmet, asks for
exactly those, and stores the answers on the project. The property that makes
the whole design worth having is the last one: an answer given for one plugin
is found already-answered by the next.
"""

import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server.models import centroid_tiles, data_model, database_model
from plexora.server.models.project import Project
from plexora.server.routes import import_routes, page_routes, project_routes

from tests.helpers import csv_spec, entry


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A project with an image and nothing else -- the shape the new import
    produces when the user supplies only an image."""
    config_path = tmp_path / "config.json"
    for module in (plexora, data_model, import_routes, database_model,
                   centroid_tiles, page_routes, project_routes):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", tmp_path)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", config_path)
    # A real image on disk: answering a requirement reloads the datasource, and
    # the reload opens it. A placeholder path would make every POST a 500 and
    # tell us nothing about the requirement plumbing.
    image = tmp_path / "image.ome.tif"
    # 256px, not smaller: load_datasource walks down to the last pyramid level
    # with every dimension >= 200, and a smaller image makes that walk raise.
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    config_path.write_text(
        json.dumps({"proj": entry("proj", dataset=None, src=str(image))}),
        encoding="utf-8",
    )
    return plexora.app.test_client()


def _csv(tmp_path, name="cells.csv"):
    path = tmp_path / name
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": np.linspace(0, 3, 4),
        # Deliberately not called "sample"/"imageid": the predictor recognises
        # those, and these tests need a column the image_id role is NOT
        # auto-assigned to so that it stays genuinely unanswered.
        "run_label": ["s1"] * 4,
    }).write_csv(path)
    return path


def _needs(client, tool="gating"):
    return client.get(f"/proj/tools/{tool}/requirements").get_json()


# --------------------------------------------------------------------------
# What gets asked for
# --------------------------------------------------------------------------

def test_an_image_only_project_is_asked_for_the_table_first(client):
    """Roles and markers are withheld until there is a table: "which column
    holds the cell id" is a question with no answers before there are
    columns, and the table requirement already covers it."""
    needs = _needs(client)

    assert [r["key"] for r in needs["missing"]] == ["table"]
    assert [r["key"] for r in needs["optional"]] == ["segmentation"]


def test_each_requirement_describes_itself_well_enough_to_render(client):
    """Core builds the form from these without knowing which plugin asked or
    what it wants them for, so kind and label carry the meaning."""
    requirement = _needs(client)["missing"][0]

    assert requirement["kind"] == "data"
    assert requirement["label"]
    assert requirement["optional"] is False


def test_a_satisfied_requirement_is_absent_rather_than_marked(client, tmp_path):
    """The user must never be asked to re-confirm something they already gave,
    so there is no "already done" state to render -- it simply is not listed."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})

    keys = [r["key"] for r in _needs(client)["missing"]]
    assert "table" not in keys


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------

def test_attaching_data_makes_the_next_questions_askable(client, tmp_path):
    """The reason the modal loops: naming the file is what makes the questions
    about its columns answerable at all."""
    response = client.post("/proj/requirements",
                           json={"tool": "gating", "data": str(_csv(tmp_path))})

    assert response.status_code == 200
    still = [r["key"] for r in response.get_json()["stillMissing"]]
    # The classifier ran at attach time, so the columns and roles now have
    # values -- but they are the predictor's, not the user's, so they come back
    # to be looked at rather than counting as settled.
    assert "table" not in still
    assert set(still) <= {"markers", "role:cell_id", "role:x", "role:y",
                          "role:image_id"}


def test_the_predictor_answers_most_of_it_without_asking(client, tmp_path):
    """A conventionally-named CSV should leave almost nothing *blocking* -- the
    point of running the same classifier at import that the screen shows. The
    values are still shown once for confirmation; what matters here is that the
    user is not made to supply them.

    image_id is the exception, and deliberately so: no classifier can tell
    whether a table covers one image or twenty without already knowing which
    column identifies the image, which is the question. Guessing it wrong draws
    several images' cells over one image with nothing said."""
    client.post("/proj/requirements", json={"tool": "gating", "data": str(_csv(tmp_path))})

    assert [r["key"] for r in _needs(client)["missing"]] == ["role:image_id"]
    project = Project.load("proj")
    assert project.roles.cell_id == "CellID"
    assert project.columns.markers == ("CD3",)


def test_how_cells_are_drawn_is_not_asked(client, tmp_path):
    """It used to be the one thing a fully-predicted CSV still stopped for.
    A project with coordinates and no mask has exactly one way to draw cells,
    and a project with a mask wants the mask."""
    client.post("/proj/requirements", json={"tool": "gating", "data": str(_csv(tmp_path))})
    needs = _needs(client)

    keys = [r["key"] for r in needs["missing"] + needs["confirm"] + needs["optional"]]
    assert "cell_layer" not in keys
    assert Project.load("proj").cell_layer == "centroids"


def test_a_guessed_value_is_offered_for_confirmation_not_treated_as_settled(client, tmp_path):
    """The predictor filling something in is not the user answering it. A
    conventionally-named table leaves nothing missing, and without this tier the
    tool would open silently on five guesses -- which is exactly what a
    well-named SpatialData import did."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})

    confirm = [r["key"] for r in _needs(client)["confirm"]]

    assert "markers" in confirm
    assert "role:cell_id" in confirm
    # The data file is not in there: a path the user typed is not a guess.
    assert "table" not in confirm


def test_confirming_once_is_enough(client, tmp_path):
    """The whole point of recording the answer. After the modal has been
    through, opening the tool again must ask nothing at all."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    needs = _needs(client)
    keys = [r["key"] for r in needs["missing"] + needs["confirm"] + needs["optional"]]

    # image_id carries a real answer rather than only a confirmation: it is
    # blocking, and a blocking question is satisfied by what was answered, not
    # by having been shown.
    client.post("/proj/requirements", json={
        "tool": "gating", "confirm": keys, "single_image": True})

    after = _needs(client)
    assert after["missing"] == []
    assert after["confirm"] == []


def test_a_field_the_user_declined_to_fill_is_not_asked_again(client, tmp_path):
    """A segmentation mask is optional and this project has none. Having been
    offered and left blank, it must not reappear every time a tool opens --
    being asked the same unanswerable question forever is worse than not
    being asked."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    assert "segmentation" in [r["key"] for r in _needs(client)["optional"]]

    client.post("/proj/requirements", json={"tool": "gating", "confirm": ["segmentation"]})

    assert Project.load("proj").segmentation.requested is False
    assert "segmentation" not in [r["key"] for r in _needs(client)["optional"]]


def test_a_role_is_recorded_centrally_and_not_asked_again(client, tmp_path):
    """The property the whole design turns on. `image_id` blocks gating until
    it is answered; once given it must be gone from every later ask, including
    a different plugin's."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    assert "role:image_id" in [r["key"] for r in _needs(client)["missing"]]

    client.post("/proj/requirements", json={"roles": {"image_id": "run_label"}})

    assert Project.load("proj").roles.image_id == "run_label"
    assert "role:image_id" not in [r["key"] for r in _needs(client)["missing"]]


def test_saying_there_is_only_one_image_answers_the_question(client, tmp_path):
    """The other answer, and the only true one for a table with no image
    column. It has to be recorded as something -- left as a blank role it would
    be indistinguishable from never having been asked, and the question would
    block forever on data that has nothing to answer it with."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    assert "role:image_id" in [r["key"] for r in _needs(client)["missing"]]

    client.post("/proj/requirements", json={"tool": "gating", "single_image": True})

    project = Project.load("proj")
    assert project.dataset.single_image is True
    assert project.roles.image_id is None
    assert "role:image_id" not in [r["key"] for r in _needs(client)["missing"]]


def test_naming_a_column_retracts_only_one_image(client, tmp_path):
    """Two answers to one question, so the second has to displace the first --
    a project claiming both would report a single image while also naming the
    column that says otherwise."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    client.post("/proj/requirements", json={"tool": "gating", "single_image": True})

    client.post("/proj/requirements", json={"roles": {"image_id": "run_label"}})

    project = Project.load("proj")
    assert project.roles.image_id == "run_label"
    assert project.dataset.single_image is False


def test_a_column_classification_is_stored_where_every_plugin_reads_it(client, tmp_path):
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})

    client.post("/proj/requirements", json={
        "columns": {"markers": ["CD3", "X_centroid"], "metadata": ["CellID", "Y_centroid"]},
    })

    assert Project.load("proj").columns.markers == ("CD3", "X_centroid")


def test_a_partial_answer_is_accepted_and_the_rest_reported(client, tmp_path):
    """The modal posts once with whatever the user filled in; the reply says
    what is still outstanding rather than rejecting the lot."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    Project.mutate("proj", lambda p: p.with_roles({"cell_id": "", "x": ""}))

    result = client.post("/proj/requirements",
                         json={"tool": "gating", "roles": {"cell_id": "CellID"}}).get_json()

    assert result["success"] is True
    assert "role:x" in [r["key"] for r in result["stillMissing"]]


def _store_with_two_tables(tmp_path, name="many.zarr"):
    import anndata
    import pandas as pd
    import spatialdata

    def one(seed):
        counts = np.random.default_rng(seed).integers(
            1, 50, (6, 3)).astype(np.float32)
        a = anndata.AnnData(
            X=counts,
            obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
            var=pd.DataFrame(index=["CD3", "CD8", "DNA"]),
        )
        a.obsm["spatial"] = (
            np.random.default_rng(seed + 1).random((6, 2)).astype(np.float32) * 100)
        return a

    path = tmp_path / name
    spatialdata.SpatialData(tables={"cells": one(0), "other": one(5)}).write(path)
    return path


def test_naming_the_table_is_part_of_answering_the_data_question(client, tmp_path):
    """A path is not always enough to read a file by.

    A .zarr store with several tables cannot be loaded until one is picked, and
    the modal used to offer a path input and nothing else -- so choosing such a
    store posted an answer the server could only refuse, with a message asking
    the user to choose and nowhere to choose from. The picker is client-side
    (dataSourceField.js); what is pinned here is the contract it posts against:
    `table` travels with `data` and decides which table is read.
    """
    store = _store_with_two_tables(tmp_path)

    refused = client.post("/proj/requirements", json={"data": str(store)})
    assert refused.status_code == 400
    assert "several tables" in refused.get_json()["error"]

    answered = client.post("/proj/requirements",
                           json={"tool": "gating", "data": str(store),
                                 "table": "other"})

    assert answered.status_code == 200
    assert answered.get_json()["success"] is True
    assert Project.load("proj").dataset.table == "other"


def test_a_bad_path_is_the_users_to_fix_and_says_which(client):
    response = client.post("/proj/requirements", json={"data": "/nope/missing.csv"})

    assert response.status_code == 400
    assert "missing.csv" in response.get_json()["error"]


def test_an_unknown_datasource_is_a_404(client):
    assert client.post("/gone/requirements", json={}).status_code == 404


# --------------------------------------------------------------------------
# Opening the tool
# --------------------------------------------------------------------------

def test_the_tool_opens_once_everything_has_been_answered(client, tmp_path):
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    needs = _needs(client)
    client.post("/proj/requirements", json={
        "tool": "gating",
        "single_image": True,
        "confirm": [r["key"] for r in
                    needs["missing"] + needs["confirm"] + needs["optional"]],
    })

    payload = client.get("/proj/tools/gating/panel").get_json()

    assert "needs" not in payload
    assert payload["fragments"]
    assert payload["scripts"]


def test_the_tool_does_not_open_on_the_predictors_word_alone(client, tmp_path):
    """The bug this tier exists for. Import a well-named table and every
    requirement has a value, so the tool used to open with the user never having
    seen which column it decided was the cell id."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})

    payload = client.get("/proj/tools/gating/panel").get_json()

    assert "needs" in payload
    assert [r["key"] for r in payload["needs"]["confirm"]]


def test_the_mid_session_ask_reaches_a_field_that_was_skipped(client, tmp_path):
    """A plugin asking for something it needs right now must get it even when
    the user was offered that field earlier and left it blank -- which is the
    ordinary case for image_id, since gating only needs one at the moment
    someone writes gates back to the source file."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    client.post("/proj/requirements", json={"confirm": ["role:image_id"]})
    assert "role:image_id" not in [r["key"] for r in _needs(client)["optional"]]

    asked = client.get("/proj/tools/gating/requirements?keys=role:image_id").get_json()

    assert [r["key"] for r in asked["requested"]] == ["role:image_id"]


def test_a_plugin_cannot_ask_for_something_it_never_declared(client, tmp_path):
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})

    asked = client.get("/proj/tools/gating/requirements?keys=role:celltype").get_json()

    assert asked["requested"] == []


def test_a_tool_that_cannot_ever_apply_is_not_offered_a_form(client, tmp_path, monkeypatch):
    """No upload gives a flat RGB image channels, so this one is hidden rather
    than asked about."""
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": entry("proj", dataset=None, kind="rgb")}), encoding="utf-8")


    response = client.get("/proj/tools/gating/panel")

    assert response.status_code == 400
    assert "needs" not in response.get_json()


# --------------------------------------------------------------------------
# Which numbers the tool is thresholding
#
# The one question the modal used to skip. A processed .h5ad routinely carries
# raw counts in X and a log-transformed copy in a layer, and the import form
# only asks about that when the file forces a table choice first -- so a
# project could reach Thresholding reading raw counts with nothing having said
# so. A threshold is a number on an axis; set against the wrong matrix it is
# not approximately right, it is meaningless.
# --------------------------------------------------------------------------

def _layered_h5ad(tmp_path, name="layered.h5ad", with_layer=True):
    """The ordinary shape of a processed .h5ad: raw counts in X, and (usually)
    a log-transformed copy beside them."""
    import anndata
    import pandas as pd

    path = tmp_path / name
    counts = np.random.default_rng(2).integers(20, 500, (6, 3)).astype(np.float32)
    adata = anndata.AnnData(
        X=counts,
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=["CD3", "CD8", "DNA"]),
        layers={"log1p": np.log1p(counts)} if with_layer else None,
    )
    adata.obsm["spatial"] = (
        np.random.default_rng(3).random((6, 2)).astype(np.float32) * 100)
    adata.write_h5ad(path)
    return path


def _cd3_max(client):
    """The largest CD3 value in the table as it is actually loaded. Raw counts
    run to the hundreds here and log values to single digits, so this tells the
    two apart without pinning an exact number."""
    data_model.load_datasource("proj", reload=True)
    return float(data_model.get_datasource_df()["CD3"].max())


def test_which_matrix_to_read_is_asked_before_a_tool_opens(client, tmp_path):
    """Never `missing` -- some matrix is always being read, so there is no gap,
    only a value nobody has looked at. That is the `confirm` tier exactly, and
    it still stops the tool from opening silently on the wrong one."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})
    needs = _needs(client)

    assert "features" not in [r["key"] for r in needs["missing"]]
    field = next(r for r in needs["confirm"] if r["key"] == "features")
    assert field["kind"] == "features"
    assert field["optional"] is False


def test_the_field_offers_every_matrix_the_file_carries(client, tmp_path):
    """Rendered from the project rather than assembled client-side, so the
    modal and the edit page cannot drift about what a file holds."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})
    needs = _needs(client)

    assert [o["value"] for o in needs["featureOptions"]] == ["X", "layer:log1p"]
    assert needs["featureSource"] == "X"
    assert needs["featureLog"] is False


def test_a_csv_is_never_asked_which_matrix_to_read(client, tmp_path):
    """A CSV has one table of numbers. Offering a choice between it and nothing
    is a dialog with a foregone answer -- the mirror image of why the marker
    split is not put up for confirmation on an AnnData."""
    client.post("/proj/requirements", json={"data": str(_csv(tmp_path))})
    needs = _needs(client)

    keys = [r["key"] for r in needs["missing"] + needs["confirm"] + needs["optional"]]
    assert "features" not in keys
    assert needs["featureOptions"] == []


def test_a_file_with_one_matrix_is_still_asked_about_the_log_switch(client, tmp_path):
    """The other half of the question. A file may carry raw counts and nothing
    else, and there is no layer to pick -- but the values still have to be
    transformed before a threshold on them reads sensibly, and only the user
    knows whether they already are."""
    client.post("/proj/requirements",
                json={"data": str(_layered_h5ad(tmp_path, with_layer=False))})
    needs = _needs(client)

    assert "features" in [r["key"] for r in needs["confirm"]]
    assert [o["value"] for o in needs["featureOptions"]] == ["X"]


def test_naming_a_matrix_is_what_gets_read(client, tmp_path):
    """Not a preference recorded and ignored: the answer rewrites the read spec,
    so it is the difference between thresholding counts and thresholding logs."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})
    assert _cd3_max(client) > 20  # raw counts, as imported

    response = client.post("/proj/requirements",
                           json={"tool": "gating", "features_layer": "layer:log1p",
                                 "confirm": ["features"]})

    assert response.status_code == 200
    assert Project.load("proj").dataset.features == {"source": "layer", "layer": "log1p"}
    assert _cd3_max(client) < 10


def test_the_log_switch_transforms_the_values_that_are_read(client, tmp_path):
    """Composes with the matrix choice rather than replacing it: this is the
    only way to say "what I have are counts" about a file with no log layer in
    it, short of going back and editing the file."""
    client.post("/proj/requirements",
                json={"data": str(_layered_h5ad(tmp_path, with_layer=False))})
    assert _cd3_max(client) > 20

    client.post("/proj/requirements",
                json={"tool": "gating", "features_layer": "X", "features_log": True,
                      "confirm": ["features"]})

    assert Project.load("proj").dataset.is_transformed is True
    assert _cd3_max(client) < 10


def test_naming_a_matrix_the_file_lacks_is_refused(client, tmp_path):
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})

    response = client.post("/proj/requirements",
                           json={"tool": "gating", "features_layer": "layer:nope"})

    assert response.status_code == 400
    assert "nope" in response.get_json()["error"]
    assert Project.load("proj").dataset.features == {"source": "X"}


def test_the_matrix_question_is_asked_once(client, tmp_path):
    """Including when the answer was to leave it exactly as it was -- the modal
    posts the keys it showed, and being shown something is being asked."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})
    client.post("/proj/requirements", json={"tool": "gating", "confirm": ["features"]})

    assert "features" not in [r["key"] for r in _needs(client)["confirm"]]


def test_swapping_the_data_file_puts_the_matrix_question_back(client, tmp_path):
    """The answer named a matrix in a file that is no longer the file. A layer
    the old one had may not exist in the new one, so a confirmation carried
    across would be a claim about something nobody looked at."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})
    client.post("/proj/requirements", json={"tool": "gating", "confirm": ["features"]})

    client.post("/proj/requirements",
                json={"data": str(_layered_h5ad(tmp_path, name="other.h5ad"))})

    assert "features" in [r["key"] for r in _needs(client)["confirm"]]


def test_answering_reports_that_the_datasource_was_re_read(client, tmp_path):
    """The page holds the table's per-column statistics from load time, and a
    tool's panel is drawn from that snapshot -- so an answer that changes which
    numbers are read leaves Thresholding showing the old ones unless the client
    is told to re-fetch. `reloaded` is that signal, and it has to distinguish an
    answer that changed the numbers from one that did not."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})

    changed = client.post("/proj/requirements",
                          json={"tool": "gating", "features_layer": "layer:log1p",
                                "confirm": ["features"]}).get_json()
    unchanged = client.post("/proj/requirements",
                            json={"tool": "gating",
                                  "confirm": ["role:image_id"]}).get_json()

    assert changed["reloaded"] is True
    assert unchanged["reloaded"] is False


def test_the_statistics_a_panel_is_drawn_from_follow_the_chosen_matrix(client, tmp_path):
    """The end of the chain the bug ran through: the read spec changed, but the
    per-column description the panel builds its histograms and ranges from is
    computed server-side and cached, so it has to be invalidated too. Raw counts
    here run to the hundreds and log values to single digits."""
    client.post("/proj/requirements", json={"data": str(_layered_h5ad(tmp_path))})

    def cd3_max():
        return client.get("/get_database_description?datasource=proj").get_json()["CD3"]["max"]

    before = cd3_max()
    client.post("/proj/requirements",
                json={"tool": "gating", "features_layer": "layer:log1p",
                      "confirm": ["features"]})

    assert before > 20
    assert cd3_max() < 10
