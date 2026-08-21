"""The project record: one typed view of one config.json entry.

The record exists because a project used to be a bare dict written by four
functions with four slightly different key sets. Two properties make it safe to
write through, and both are pinned here: unknown keys survive a save, and every
change is a merge.
"""

import json

import pytest

from plexora.server.models.project import (
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    Project,
    all_projects,
)

from tests.helpers import anndata_spec, csv_spec, entry, project


def test_round_trip_preserves_every_modelled_field():
    original = project(
        "demo",
        dataset=anndata_spec("/data/cells.h5ad", coordinates={"source": "obsm", "obsm_key": "spatial"},
                             obs_id_field="barcode", cell_id="barcode",
                             markers=["CD3"], metadata=["barcode"]),
        segmentation="/seg.tif",
    )
    assert Project.from_entry("demo", original.to_entry()).to_entry() == original.to_entry()


def test_a_key_the_record_does_not_model_survives_a_save():
    """The property that makes writing through safe. Editing an AnnData project
    used to destroy it because the save rebuilt the entry from `{}` and lost
    every key it did not know about -- data_type and the read spec included."""
    stored = {**entry("demo", dataset=csv_spec("/c.csv")), "someFutureKey": {"a": 1}}

    saved = Project.from_entry("demo", stored).patch(dataset=None).to_entry()

    assert saved["someFutureKey"] == {"a": 1}
    assert saved["dataset"] is None


def test_patch_carries_across_everything_it_was_not_given():
    before = project("demo", dataset=csv_spec("/c.csv", markers=["CD3"]), segmentation="/seg.tif")

    after = before.patch(created_at="2026-01-01T00:00:00")

    assert after.created_at == "2026-01-01T00:00:00"
    assert after.dataset == before.dataset
    assert after.segmentation == before.segmentation
    assert after.image == before.image


def test_patch_does_not_mutate_the_original():
    before = project("demo", dataset=csv_spec("/c.csv", image_id=None))
    before.with_roles({"image_id": "sample"})
    assert before.roles.image_id is None


def test_an_uncollected_role_is_none_rather_than_an_error():
    """A role nobody has filled in is an ordinary state -- it is what a plugin
    declares in Requires so the host can ask for it."""
    p = project("demo", dataset=csv_spec("/c.csv", image_id=None))
    assert p.roles.image_id is None
    assert p.roles.missing(("cell_id", "image_id")) == ["image_id"]


def test_a_role_is_cleared_by_an_empty_string():
    """How the edit form says "unset this" -- an absent key means "leave it"."""
    p = project("demo", dataset=csv_spec("/c.csv", celltype="phenotype"))
    assert p.with_roles({"celltype": ""}).roles.celltype is None
    assert p.with_roles({}).roles.celltype == "phenotype"


def test_an_unknown_role_name_is_rejected():
    with pytest.raises(KeyError):
        ColumnRoles().get("cell_ids")


def test_columns_are_classified_only_once_there_are_markers():
    """Metadata alone does not count: a table with no markers has nothing to
    threshold, and calling that classified would let a marker-hungry plugin
    open onto an empty panel."""
    assert not ColumnGroups(metadata=("CellID",)).classified
    assert ColumnGroups(markers=("CD3",), metadata=("CellID",)).classified


def test_no_dataset_block_is_the_image_only_state():
    p = project("img_only", dataset=None)
    assert not p.has_table
    assert p.source_kind is None
    assert p.roles.cell_id is None
    assert p.columns.markers == ()


def test_obs_id_field_is_not_the_same_thing_as_the_cell_id_role():
    """They usually hold the same string and are still different facts: the
    role names a column in the table that comes OUT of the adapter, obs_id_field
    names an input to it. None means "number the rows", which is a different
    instruction from a role naming a column that happens to be called "id" --
    and real data has an obs column literally named "id"."""
    spec = DataSpec(type="anndata", src="/c.h5ad", obs_id_field=None,
                    roles=ColumnRoles(cell_id="id"))
    assert spec.obs_id_field is None
    assert spec.roles.cell_id == "id"
    assert DataSpec.from_dict(spec.to_dict()).obs_id_field is None


def test_save_leaves_every_other_project_untouched(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "other": entry("other", dataset=csv_spec("/other.csv")),
    }), encoding="utf-8")

    project("mine", dataset=csv_spec("/mine.csv")).save(tmp_path)

    names = {p.name for p in all_projects(tmp_path)}
    assert names == {"other", "mine"}


def test_mutate_is_read_modify_write(tmp_path):
    project("demo", dataset=csv_spec("/c.csv", image_id=None)).save(tmp_path)

    Project.mutate("demo", lambda p: p.with_roles({"image_id": "sample"}), tmp_path)

    assert Project.load("demo", tmp_path).roles.image_id == "sample"


def test_mutate_returns_none_when_the_project_vanished(tmp_path):
    """Really happens: the segmentation job outlives a delete."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert Project.mutate("gone", lambda p: p, tmp_path) is None


def test_delete_removes_only_that_project(tmp_path):
    project("keep", dataset=csv_spec("/a.csv")).save(tmp_path)
    project("drop", dataset=csv_spec("/b.csv")).save(tmp_path)

    Project.load("drop", tmp_path).delete(tmp_path)

    assert [p.name for p in all_projects(tmp_path)] == ["keep"]


def test_a_pending_mask_is_requested_but_not_yet_available():
    """The distinction the tool gate turns on: asking again for a mask the user
    already gave would be the wrong question, but the viewer cannot serve it
    until conversion finishes."""
    p = project("demo", dataset=None, segmentation="pending")
    assert p.segmentation.requested
    assert p.segmentation.pending
    assert not p.segmentation.available


# --------------------------------------------------------------------------
# The cell layer
# --------------------------------------------------------------------------

def test_a_project_with_a_mask_and_coordinates_can_draw_cells_either_way():
    p = project("demo", dataset=csv_spec("/a.csv"), segmentation="/tmp/mask.tif")
    # Segmentation first: it shows the real cell shape, and a user who supplied
    # a mask supplied it to be used.
    assert p.cell_layer_options == ["segmentation", "centroids"]


def test_coordinates_alone_leave_only_centroids():
    assert project("demo", dataset=csv_spec("/a.csv")).cell_layer_options == ["centroids"]


def test_a_project_with_neither_can_draw_no_cell_layer():
    """Nothing to choose between, so nothing asks. Whatever wanted to draw
    cells is missing something more basic -- a mask, or the coordinate roles."""
    assert project("demo", dataset=None).cell_layer_options == []


def test_a_mask_is_drawn_as_outlines_without_being_asked():
    """A user who supplied a mask supplied it to be used. This was a question
    the host put in front of them before a cell-drawing tool could open, which
    is a dialog with a foregone conclusion."""
    p = project("demo", dataset=csv_spec("/a.csv"), segmentation="/m.tif")
    assert p.cell_layer == "segmentation"


def test_coordinates_without_a_mask_fall_back_to_centroids():
    assert project("demo", dataset=csv_spec("/a.csv")).cell_layer == "centroids"


def test_a_project_that_can_draw_neither_has_no_cell_layer():
    assert project("demo", dataset=None).cell_layer is None


def test_an_override_wins_over_the_default():
    p = project("demo", dataset=csv_spec("/a.csv"), segmentation="/m.tif",
                cell_layer="centroids")
    assert p.cell_layer == "centroids"


def test_a_meaningless_cell_layer_is_not_stored():
    """The client sends this. A value the viewer cannot draw is worse than no
    value, which at least leaves the default to resolve."""
    p = project("demo", dataset=csv_spec("/a.csv")).with_cell_layer("outlines")
    assert p.cell_layer_choice is None


def test_an_override_can_be_taken_back():
    """"" and None both mean "no choice". Being able to say that is not a
    nicety: the choice outlives whatever made it look right, so a project
    pinned to centroids while its mask was still converting has no other way
    back to drawing the mask."""
    p = project("demo", dataset=csv_spec("/a.csv"), segmentation="/m.tif",
                cell_layer="centroids")
    assert p.with_cell_layer("").cell_layer_choice is None
    assert p.with_cell_layer("").cell_layer == "segmentation"
    assert p.with_cell_layer(None).cell_layer_choice is None


def test_an_override_the_project_cannot_draw_falls_back_to_what_it_can():
    """Stored answers outlive the thing they described: a project whose mask
    was removed still carries the choice of it."""
    p = project("demo", dataset=csv_spec("/a.csv"), cell_layer="segmentation")
    assert p.cell_layer == "centroids"


def test_the_override_round_trips_and_the_default_is_not_frozen_into_the_file():
    """Writing the resolved default would leave a project that later gained a
    mask still drawing centroids."""
    chosen = project("demo", dataset=csv_spec("/a.csv"), cell_layer="centroids")
    assert Project.from_entry("demo", chosen.to_entry()).cell_layer_choice == "centroids"

    defaulted = project("demo", dataset=csv_spec("/a.csv"), segmentation="/m.tif")
    assert defaulted.to_entry().get("cellLayer") is None


# --------------------------------------------------------------------------
# Confirmed answers
# --------------------------------------------------------------------------

def test_confirming_is_additive():
    """The modal posts only the keys it showed, so confirming one thing must
    never un-confirm another."""
    p = project("demo").with_confirmed(["markers"]).with_confirmed(["role:x"])
    assert set(p.confirmed) == {"markers", "role:x"}


def test_confirming_the_same_key_twice_does_not_duplicate_it():
    p = project("demo").with_confirmed(["markers"]).with_confirmed(["markers"])
    assert p.confirmed == ("markers",)


def test_unconfirmed_reports_in_the_order_asked():
    p = project("demo").with_confirmed(["role:x"])
    assert p.unconfirmed(["markers", "role:x", "role:y"]) == ["markers", "role:y"]


def test_confirmations_round_trip():
    p = project("demo", confirmed=("markers", "role:cell_id"))
    assert Project.from_entry("demo", p.to_entry()).confirmed == ("markers", "role:cell_id")


def test_replacing_the_table_forgets_what_was_confirmed_about_it():
    """The roles and the marker split were confirmed against columns that may
    not exist in the new file, so those answers have to go back in front of the
    user. The mask and the cell layer are unaffected -- neither describes the
    table."""
    p = project("demo", dataset=csv_spec("/a.csv"), segmentation="/m.tif",
                cell_layer="centroids",
                confirmed=("markers", "role:cell_id", "table", "segmentation"))

    after = p.forget_table_answers()

    assert set(after.confirmed) == {"segmentation"}
    assert after.cell_layer == "centroids"


def test_confirming_a_batch_that_names_a_key_twice_stores_it_once():
    """The modal's `confirm` list and the answers it carries name the same
    keys, and both reach with_confirmed in one call."""
    p = project("demo").with_confirmed(["markers", "role:x", "markers"])
    assert p.confirmed == ("markers", "role:x")


def test_a_project_whose_role_answers_were_lost_asks_again():
    """`role:undefined` is the trace of a modal that could not name the role it
    was asking about: it posted every answer under that one key, so the answers
    were dropped and the questions were marked answered anyway. The project
    kept its guessed roles with nothing left to correct them -- for an AnnData
    import that means the synthesized row number stays the cell id, and every
    segmentation outline lands on the wrong cell. Reading the record back has
    to reopen the role questions, or the real answer can never be given."""
    stored = project("demo", confirmed=(
        "features", "role:cell_id", "role:x", "role:y", "role:undefined",
    )).to_entry()

    reloaded = Project.from_entry("demo", stored)

    assert reloaded.confirmed == ("features",)
    assert reloaded.unconfirmed(["role:cell_id"]) == ["role:cell_id"]


def test_role_confirmations_survive_when_no_answer_was_lost():
    """The repair above is scoped to records carrying the marker. A project
    whose roles were answered properly must not be re-asked."""
    stored = project("demo", confirmed=("features", "role:cell_id")).to_entry()

    assert Project.from_entry("demo", stored).confirmed == ("features", "role:cell_id")


# --------------------------------------------------------------------------
# Which matrix the values come from, and what is done to them
# --------------------------------------------------------------------------

def test_the_main_matrix_is_always_one_of_the_choices():
    """`X` is a real answer, not the absence of one, so it is offered rather
    than being what you get by declining to pick a layer."""
    p = project(dataset=anndata_spec("/tmp/a.h5ad"))

    assert [o["value"] for o in p.feature_options] == ["X"]
    assert p.feature_source == "X"


def test_every_layer_the_file_carries_is_offered_beside_it():
    p = project(dataset=anndata_spec("/tmp/a.h5ad")).with_layers(["log1p", "scaled"])

    assert [o["value"] for o in p.feature_options] == ["X", "layer:log1p", "layer:scaled"]
    assert all(o["label"] for o in p.feature_options)


def test_a_csv_has_no_matrix_to_choose_between():
    """One table of numbers. A picker here would be a question with a foregone
    answer, which is the same reason a CSV's marker split IS asked about and an
    AnnData's is not."""
    assert project(dataset=csv_spec("/tmp/cells.csv")).feature_options == []


def test_choosing_a_layer_rewrites_the_read_spec():
    p = (project(dataset=anndata_spec("/tmp/a.h5ad"))
         .with_layers(["log1p"])
         .with_feature_source("layer:log1p"))

    assert p.dataset.features == {"source": "layer", "layer": "log1p"}
    assert p.feature_source == "layer:log1p"


def test_a_layer_the_file_does_not_have_is_refused_rather_than_stored():
    """Storing it would leave a project that no longer opens at all -- a worse
    outcome than the question the user was trying to answer."""
    p = project(dataset=anndata_spec("/tmp/a.h5ad")).with_layers(["log1p"])

    with pytest.raises(ValueError, match="nope"):
        p.with_feature_source("layer:nope")


def test_a_layer_named_x_cannot_be_confused_with_the_main_matrix():
    """anndata permits it, so the picker's vocabulary is prefixed rather than
    bare -- "X" and layers["X"] are different matrices."""
    p = project(dataset=anndata_spec("/tmp/a.h5ad")).with_layers(["X"])

    p = p.with_feature_source("layer:X")

    assert p.dataset.features == {"source": "layer", "layer": "X"}
    assert p.feature_source == "layer:X"


def test_the_log_switch_composes_with_the_matrix_rather_than_replacing_it():
    """One says which numbers, the other says what to do with them on the way
    in. A file may carry raw counts and no log layer, and then the switch is the
    only answer available."""
    p = (project(dataset=anndata_spec("/tmp/a.h5ad"))
         .with_layers(["log1p"])
         .with_feature_source("layer:log1p")
         .with_log_transform(True))

    assert p.dataset.features == {"source": "layer", "layer": "log1p"}
    assert p.log_transformed is True
    assert p.with_log_transform(False).log_transformed is False


def test_swapping_the_data_file_reopens_the_matrix_question():
    """The confirmation named a matrix in a file that is no longer the file."""
    p = project(dataset=anndata_spec("/tmp/a.h5ad"),
                confirmed=("table", "features", "role:x"))

    assert "features" not in p.forget_table_answers().confirmed


# --------------------------------------------------------------------------
# The coordinate question
#
# For AnnData and SpatialData "where are the coordinates" is one question with
# two answer shapes, not two column roles: an obsm array holds both axes at
# once. It used to be askable only as a pair of obs-column selects, so the obsm
# case could be shown only as a label pinned to the blank option -- visible,
# unpickable, and never confirmed.
# --------------------------------------------------------------------------

def test_the_coordinate_question_offers_both_places_the_answer_can_live():
    spec = anndata_spec("/a.h5ad",
                        obs_columns=("X_centroid", "Y_centroid"),
                        obsm=({"name": "spatial", "shape": [6, 2]},
                              {"name": "X_umap", "shape": [6, 2]}),
                        coordinates={"source": "obsm", "obsm_key": "spatial"})

    options = project("demo", dataset=spec).coordinate_options

    assert [e["name"] for e in options["obsm"]] == ["spatial", "X_umap"]
    assert options["obs"] == ["X_centroid", "Y_centroid"]
    assert options["current"] == {"source": "obsm", "obsm_key": "spatial"}


def test_every_obsm_array_is_offered_with_its_shape():
    """Shape travels with the name because it is the only other thing on offer.
    It cannot separate a position from an embedding -- `spatial` and `X_umap`
    are routinely both (n, 2) float32 -- which is exactly why the list is not
    filtered down to what "looks spatial" and the user picks instead."""
    spec = anndata_spec("/a.h5ad", obsm=({"name": "spatial", "shape": [6, 2]},
                                         {"name": "X_umap", "shape": [6, 2]}))

    obsm = project("demo", dataset=spec).coordinate_options["obsm"]

    assert obsm == [{"name": "spatial", "shape": [6, 2]},
                    {"name": "X_umap", "shape": [6, 2]}]


def test_choosing_an_obsm_array_is_recordable_at_all():
    """The answer the old two-select shape had no way to express."""
    spec = anndata_spec("/a.h5ad", obsm=({"name": "spatial", "shape": [6, 2]},))

    after = project("demo", dataset=spec).with_coordinates(
        {"source": "obsm", "obsm_key": "spatial"})

    assert after.dataset.coordinates == {"source": "obsm", "obsm_key": "spatial"}
    # The adapter emits its coordinates under these names whichever source it
    # read them from, so the roles still point at the loaded table.
    assert (after.roles.x, after.roles.y) == ("X", "Y")


def test_choosing_obs_columns_is_recorded_as_a_read_spec():
    spec = anndata_spec("/a.h5ad", obs_columns=("X_um", "Y_um"))

    after = project("demo", dataset=spec).with_coordinates(
        {"source": "obs", "x_column": "X_um", "y_column": "Y_um"})

    assert after.dataset.coordinates == {
        "source": "obs", "x_column": "X_um", "y_column": "Y_um"}


def test_an_obsm_array_the_file_does_not_have_is_refused():
    """Stored and discovered later by the adapter means a project that no
    longer opens, which is worse than the answer being rejected now."""
    spec = anndata_spec("/a.h5ad", obsm=({"name": "spatial", "shape": [6, 2]},))

    with pytest.raises(ValueError, match="no obsm array named"):
        project("demo", dataset=spec).with_coordinates(
            {"source": "obsm", "obsm_key": "X_umap"})


def test_naming_neither_source_is_refused_rather_than_guessed():
    """The whole point: an unanswered coordinate question stays unanswered."""
    with pytest.raises(ValueError, match="obs or obsm"):
        project("demo", dataset=anndata_spec("/a.h5ad")).with_coordinates({})


def test_half_a_coordinate_is_still_half_a_coordinate():
    with pytest.raises(ValueError, match="both an X and a Y"):
        project("demo", dataset=anndata_spec("/a.h5ad", obs_columns=("X_um", "Y_um"))
                ).with_coordinates({"source": "obs", "x_column": "X_um"})


def test_a_project_imported_before_obsm_was_recorded_can_be_backfilled():
    spec = anndata_spec("/a.h5ad")
    assert project("demo", dataset=spec).coordinate_options["obsm"] == []

    after = project("demo", dataset=spec).with_obsm(
        [{"name": "spatial", "shape": [6, 2]}])

    assert after.dataset.obsm == ({"name": "spatial", "shape": [6, 2]},)


def test_the_coordinate_answer_round_trips():
    spec = anndata_spec("/a.h5ad", obsm=({"name": "spatial", "shape": [6, 2]},),
                        coordinates={"source": "obsm", "obsm_key": "spatial"})
    stored = project("demo", dataset=spec).to_entry()

    reloaded = Project.from_entry("demo", stored)

    assert reloaded.dataset.obsm == ({"name": "spatial", "shape": [6, 2]},)
    assert reloaded.dataset.coordinates == {"source": "obsm", "obsm_key": "spatial"}


# --------------------------------------------------------------------------
# "This table covers one image"
# --------------------------------------------------------------------------

def test_one_image_is_an_answer_rather_than_an_absent_role():
    """A blank role and "I looked, there is one image" are different states,
    and only one of them should stop a tool asking. Storing the second as the
    first would make a single-image project answer the question forever."""
    after = project("demo", dataset=csv_spec("/a.csv", single_image=False)
                    ).with_single_image(True)

    assert after.dataset.single_image is True
    assert after.roles.image_id is None
    assert Project.from_entry("demo", after.to_entry()).dataset.single_image is True


def test_naming_a_column_retracts_one_image():
    """Two answers to one question, so the later one displaces the earlier --
    a project claiming both would report one image while naming the column
    that says otherwise."""
    after = (project("demo", dataset=csv_spec("/a.csv", single_image=False))
             .with_single_image(True)
             .with_role_answers({"image_id": "sample"}))

    assert after.roles.image_id == "sample"
    assert after.dataset.single_image is False


# --------------------------------------------------------------------------
# "There is no id column -- number the rows"
# --------------------------------------------------------------------------

def test_numbering_the_rows_is_an_answer_rather_than_an_unset_read_spec():
    """The same distinction `single_image` draws. An unset `obs_id_field` is
    also the state of a project nobody has asked, and the importer leaves it
    unset on every AnnData it registers -- so without somewhere to record the
    answer, "number the rows" could only be said by leaving the question
    blank."""
    after = project("demo", dataset=anndata_spec("/a.h5ad", obs_id_field="barcode",
                                                 cell_id="barcode")
                    ).with_row_number_ids(True)

    assert after.dataset.row_number_ids is True
    # The read spec is cleared: the two are alternative answers, and a project
    # holding both would number the rows while naming a column it reads from.
    assert after.dataset.obs_id_field is None
    assert after.roles.cell_id == "id"
    assert Project.from_entry("demo", after.to_entry()).dataset.row_number_ids is True


def test_naming_an_id_column_retracts_numbering_the_rows():
    after = (project("demo", dataset=anndata_spec("/a.h5ad"))
             .with_row_number_ids(True)
             .with_role_answers({"cell_id": "MaskLabel"}))

    assert after.dataset.obs_id_field == "MaskLabel"
    assert after.dataset.row_number_ids is False


def test_a_csv_has_no_row_number_answer_to_record():
    """Its cell id is one of the columns the user classified, and its adapter
    has no positional fallback to fall back to."""
    after = project("demo", dataset=csv_spec("/a.csv")).with_row_number_ids(True)

    assert after.dataset.row_number_ids is False


def test_replacing_the_data_file_forgets_the_answers_that_named_no_column():
    """Both were said about the old table. Left standing they would keep their
    questions answered, so neither would be asked about the file that replaced
    it -- and unlike a role, there is no column name to notice has gone."""
    after = (project("demo", dataset=anndata_spec("/a.h5ad", single_image=True))
             .with_row_number_ids(True)
             .forget_table_answers())

    assert after.dataset.row_number_ids is False
    assert after.dataset.single_image is False
