"""The one reader: what a project has, and whether anybody said so.

Three things used to answer this question separately -- `api/plugin.py`'s
`_answered`, `project_routes._describe()["has"]`, and whatever the caller felt
like. These pin the rules once, and the last test in the file pins the thing
that actually matters: the plugin contract now gets its answers from here and
gets the same ones it used to.
"""

import pytest

from plexora.api import plugin as plugin_api
from plexora.server.models import manifest
from plexora.server.models.project import DataSpec
from tests import helpers


def _status(project, key):
    return manifest.status(project, key)["status"]


# --------------------------------------------------------------------------
# An image is a complete project
# --------------------------------------------------------------------------

def test_an_image_alone_is_a_valid_project():
    """The premise of the whole progressive model. Nothing about an image-only
    project is a defect, so nothing about it is flagged."""
    project = helpers.project("demo")

    assert _status(project, "image") == manifest.PRESENT
    assert _status(project, "table") == manifest.MISSING
    assert _status(project, "segmentation") == manifest.MISSING
    assert manifest.needs_setup(project) is False
    assert manifest.summary(project)["table"] == "missing"


def test_a_project_that_cannot_read_its_own_data_needs_setup():
    """The one state worth a badge: a file was named and something about it is
    still undecided, so the project silently opens as an image and there is no
    other way to find out why."""
    project = helpers.project("demo", dataset=DataSpec(
        type="spatialdata", src="/data/store.zarr", unresolved=("table",)))

    assert manifest.needs_setup(project) is True
    assert manifest.summary(project) == {
        "imageKind": "ome_tiff",
        "segmentation": "missing",
        "table": "unresolved",
        "tableType": "spatialdata",
        "unresolved": ["table"],
        "needsSetup": True,
    }


def test_an_unresolved_source_reports_the_path_it_already_has():
    """MISSING, because nothing can read it -- but carrying the path, so
    whoever asks prefills the field instead of asking for it again."""
    project = helpers.project("demo", dataset=DataSpec(
        type="spatialdata", src="/data/store.zarr", unresolved=("table",)))

    state = manifest.status(project, "table")

    assert state["status"] == manifest.MISSING
    assert state["value"] == {"src": "/data/store.zarr", "table": None,
                              "type": "spatialdata", "unresolved": ["table"]}


# --------------------------------------------------------------------------
# Guessed is not the same as given
# --------------------------------------------------------------------------

def test_a_predicted_column_is_a_guess_until_somebody_looks_at_it():
    project = helpers.project("demo", dataset=helpers.csv_spec("/t.csv"))

    assert _status(project, "role:cell_id") == manifest.GUESSED
    assert manifest.answered(project, "role:cell_id") is True


def test_a_confirmed_column_is_an_answer():
    project = helpers.project("demo", dataset=helpers.csv_spec("/t.csv"),
                              confirmed=("role:cell_id",))

    state = manifest.status(project, "role:cell_id")

    assert state["status"] == manifest.PRESENT
    assert state["confirmed"] is True
    assert state["value"] == "CellID"


def test_a_path_the_user_typed_is_never_shown_back_for_confirmation():
    """There is nothing to confirm about a file path: showing it back and
    asking "is this the file you chose?" is noise."""
    project = helpers.project("demo", dataset=helpers.csv_spec("/t.csv"),
                              segmentation="/m.tif")

    assert _status(project, "table") == manifest.PRESENT
    assert _status(project, "segmentation") == manifest.PRESENT
    assert _status(project, "image") == manifest.PRESENT


def test_a_structural_file_states_its_own_marker_split():
    """`var` is markers and `obs` is annotations. Putting that in a
    drag-and-drop box asks the user to confirm what the file already says."""
    project = helpers.project("demo", dataset=helpers.anndata_spec(
        "/t.h5ad", markers=("CD3",), metadata=("area",)))

    assert _status(project, "markers") == manifest.PRESENT

    csv = helpers.project("demo", dataset=helpers.csv_spec(
        "/t.csv", markers=("CD3",), metadata=("area",)))
    assert _status(csv, "markers") == manifest.GUESSED


# --------------------------------------------------------------------------
# Questions a format cannot be asked
# --------------------------------------------------------------------------

def test_x_and_y_do_not_exist_as_columns_for_a_structural_file():
    """The adapter builds X and Y from a read spec, and an obsm array supplies
    both axes at once -- which no pair of column selects can express. Not
    missing: unanswerable as put, which is why `coordinates` exists."""
    project = helpers.project("demo", dataset=helpers.anndata_spec(
        "/t.h5ad", coordinates={"source": "obsm", "obsm_key": "spatial"}))

    assert _status(project, "role:x") == manifest.NOT_APPLICABLE
    assert _status(project, "role:y") == manifest.NOT_APPLICABLE
    # The importer's proposal, which is a prefill and not a decision -- so it
    # is shown back once, like every other guess.
    assert _status(project, "coordinates") == manifest.GUESSED


def test_a_csv_answers_x_and_y_as_ordinary_columns():
    project = helpers.project("demo", dataset=helpers.csv_spec("/t.csv"))

    assert _status(project, "role:x") == manifest.GUESSED
    assert manifest.status(project, "role:x")["value"] == "X_centroid"
    assert _status(project, "coordinates") == manifest.NOT_APPLICABLE


def test_nothing_about_columns_is_asked_before_there_is_a_table():
    project = helpers.project("demo")

    for key in ("markers", "features", "coordinates", "role:cell_id", "role:x"):
        assert _status(project, key) == manifest.MISSING, key
    assert manifest.open_questions(project) == ["segmentation", "table"]


# --------------------------------------------------------------------------
# The states that are answers without naming a column
# --------------------------------------------------------------------------

def test_numbering_the_rows_is_an_answer_to_the_cell_id_question():
    """An AnnData's `roles.cell_id` is the adapter's own positional "id",
    written the moment a table loads -- reading it as the answer reported every
    such project as having answered a question nobody was asked."""
    unasked = helpers.project("demo", dataset=helpers.anndata_spec(
        "/t.h5ad", row_number_ids=False))
    assert _status(unasked, "role:cell_id") == manifest.MISSING

    numbered = helpers.project("demo", dataset=helpers.anndata_spec("/t.h5ad"))
    assert manifest.status(numbered, "role:cell_id")["value"] == "Row number"

    named = helpers.project("demo", dataset=helpers.anndata_spec(
        "/t.h5ad", obs_id_field="CellID"))
    assert manifest.status(named, "role:cell_id")["value"] == "CellID"


def test_one_image_is_an_answer_to_the_image_id_question():
    covered = helpers.project("demo", dataset=helpers.csv_spec("/t.csv"))
    assert manifest.status(covered, "role:image_id")["value"] == "Single image"

    open_question = helpers.project("demo", dataset=helpers.csv_spec(
        "/t.csv", single_image=False))
    assert _status(open_question, "role:image_id") == manifest.MISSING

    named = helpers.project("demo", dataset=helpers.csv_spec(
        "/t.csv", image_id="Sample"))
    assert manifest.status(named, "role:image_id")["value"] == "Sample"


def test_expression_values_are_never_absent_only_unexamined():
    """A table is always read from some matrix, so this reaches the user
    through confirmation and never as a gap."""
    project = helpers.project("demo", dataset=helpers.anndata_spec("/t.h5ad"))

    assert _status(project, "features") == manifest.GUESSED
    assert manifest.answered(project, "features") is True


# --------------------------------------------------------------------------
# The shape of the thing
# --------------------------------------------------------------------------

def test_every_key_is_reported():
    project = helpers.project("demo")

    assert set(manifest.manifest(project)) == set(manifest.KEYS)
    assert list(manifest.manifest(project)) == list(manifest.KEYS)


def test_an_unknown_key_is_an_error_rather_than_a_false_negative():
    """A typo that quietly reports "missing" would have a surface silently ask
    a question core cannot record the answer to."""
    with pytest.raises(KeyError):
        manifest.status(helpers.project("demo"), "role:nonsense")


def test_every_key_has_a_label():
    assert set(manifest.LABELS) == set(manifest.KEYS)


def test_open_questions_can_be_narrowed_and_keep_ask_order():
    project = helpers.project("demo")

    assert manifest.open_questions(project, ["table", "segmentation"]) == [
        "segmentation", "table"]
    assert manifest.open_questions(project, ["role:x"]) == []


# --------------------------------------------------------------------------
# The move itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("build", [
    lambda: helpers.project("demo"),
    lambda: helpers.project("demo", segmentation="/m.tif"),
    lambda: helpers.project("demo", dataset=helpers.csv_spec("/t.csv")),
    lambda: helpers.project("demo", dataset=helpers.csv_spec(
        "/t.csv", single_image=False)),
    lambda: helpers.project("demo", dataset=helpers.anndata_spec("/t.h5ad")),
    lambda: helpers.project("demo", dataset=helpers.anndata_spec(
        "/t.h5ad", obs_id_field="CellID", coordinates={})),
    lambda: helpers.project("demo", dataset=DataSpec(
        type="spatialdata", src="/s.zarr", unresolved=("table",))),
    lambda: helpers.project("demo", kind="rgb"),
])
def test_the_plugin_contract_asks_this_module_and_nothing_else(build):
    """`api/plugin.py::_answered` is one line now. This is what says the forty
    it replaced said the same thing."""
    project = build()

    for key in manifest.KEYS:
        if key == "image":
            continue  # not a requirement key -- every plugin gets the image
        assert plugin_api._answered(project, key) == manifest.answered(project, key), key
