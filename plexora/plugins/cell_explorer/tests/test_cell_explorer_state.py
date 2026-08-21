"""Stored display preferences: what survives, what is refused, and what is kept.

Almost everything here is about a document that has been sitting on disk across
upgrades and now has to be handed to a renderer. The bias is towards keeping
what the user did:

*Nothing is filtered against the current table.* A column that is temporarily
absent -- a data file swapped, a different image open -- comes back, and
dropping its colours the one time it was missing throws away work somebody did
on purpose.

*Unknown fields survive a round trip.* A newer Plexora writes a field this
version does not model; the first save from an older tab must not strip it.

*A newer schema is refused rather than overwritten.* Starting fresh destroys
work to avoid showing a banner.

And one thing is refused rather than kept: a colour that is not a hex triple. It
reaches a canvas fillStyle, where an unrecognised string is silently ignored and
the cell keeps whatever colour was set last -- a wrong picture, not a missing
one.
"""

import pytest

from plexora.plugins.cell_explorer.server import state


def normalized(**fields):
    return state.normalize({**state.default_state(), **fields})


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def test_a_fresh_project_starts_with_nothing_chosen():
    fresh = state.default_state()
    assert fresh["selected"] is None
    assert fresh["revision"] == 0
    assert fresh["categorical"] == {} and fresh["continuous"] == {}


def test_the_default_opacity_matches_the_viewers():
    """Two files hold this number -- imageViewer.js for the renderer and this
    for the stored default. A project that has never been saved and one that
    has must not open at different strengths."""
    viewer = (__import__("pathlib").Path(__file__).resolve().parents[4]
              / "plexora" / "client" / "src" / "js" / "views" / "imageViewer.js")
    source = viewer.read_text(encoding="utf-8")
    assert f"DEFAULT_CELL_LAYER_OPACITY = {state.DEFAULT_OPACITY}" in source


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def test_a_hex_colour_survives():
    result = normalized(categorical={"phenotype": {"colors": {"Tumor": "#ff0000"}}})
    assert result["categorical"]["phenotype"]["colors"] == {"Tumor": "#ff0000"}


@pytest.mark.parametrize("color", ["red", "#f00", "#gggggg", "", None, 42,
                                   "javascript:alert(1)"])
def test_anything_that_is_not_a_hex_triple_is_dropped(color):
    result = normalized(categorical={"p": {"colors": {"Tumor": color}}})
    assert result["categorical"]["p"]["colors"] == {}


def test_hidden_categories_survive_as_a_stable_list():
    result = normalized(categorical={"p": {"hidden": ["B", "A", "A"]}})
    assert result["categorical"]["p"]["hidden"] == ["A", "B"]


def test_a_hidden_numeric_overlay_is_remembered():
    """A boolean here where a categorical entry's `hidden` is a list of labels,
    because a ramp has no rows to hide one of -- the column is the only unit
    there is. Worth its own test because these per-column entries are rebuilt
    key by key rather than passed through, so a field nobody listed is dropped
    by the first save rather than by anything that looks like a decision."""
    assert normalized(continuous={"c": {"hidden": True}})["continuous"]["c"]["hidden"] is True
    assert normalized(continuous={"c": {}})["continuous"]["c"]["hidden"] is False


def test_an_unknown_palette_falls_back_rather_than_reaching_the_renderer():
    result = normalized(continuous={"c": {"palette": "jet"}})
    assert result["continuous"]["c"]["palette"] == state.DEFAULT_PALETTE


def test_a_known_palette_survives():
    result = normalized(continuous={"c": {"palette": "magma"}})
    assert result["continuous"]["c"]["palette"] == "magma"


def test_a_manual_range_survives():
    result = normalized(continuous={
        "c": {"range": {"mode": "manual", "min": 0.1, "max": 0.9}}})
    assert result["continuous"]["c"]["range"] == {"mode": "manual", "min": 0.1, "max": 0.9}


@pytest.mark.parametrize("low,high", [(1.0, 1.0), (2.0, 1.0), (None, 1.0), (0.0, None)])
def test_a_manual_range_that_no_longer_makes_sense_reverts_to_auto(low, high):
    """Keeping an inverted range means a panel that refuses to draw and offers
    no way out except knowing to press Reset."""
    result = normalized(continuous={
        "c": {"range": {"mode": "manual", "min": low, "max": high}}})
    assert result["continuous"]["c"]["range"] == {"mode": "auto"}


def test_opacity_is_clamped_to_something_that_can_be_drawn():
    assert normalized(display={"opacity": 5})["display"]["opacity"] == 1.0
    assert normalized(display={"opacity": -1})["display"]["opacity"] == 0.0
    assert normalized(display={"opacity": "loud"})["display"]["opacity"] == state.DEFAULT_OPACITY


def test_an_unknown_display_mode_is_dropped():
    """The Cells control is core's, and handing it a mode it does not have
    would leave the control showing nothing selected."""
    assert normalized(display={"mode": "hologram"})["display"]["mode"] is None
    assert normalized(display={"mode": "filled"})["display"]["mode"] == "filled"


def test_only_the_two_real_kinds_can_be_overridden():
    result = normalized(overrides={"a": "categorical", "b": "continuous", "c": "spline"})
    assert result["overrides"] == {"a": "categorical", "b": "continuous"}


def test_settings_for_a_column_the_table_does_not_have_are_kept():
    """A swapped data file or a different image is temporary. Dropping the
    colours somebody chose, the one time the column was absent, is not."""
    result = normalized(categorical={"gone": {"colors": {"X": "#123456"}}})
    assert "gone" in result["categorical"]


def test_a_field_this_version_does_not_model_survives_a_round_trip():
    """A newer Plexora wrote it. The first save from an older tab must not
    strip it."""
    result = state.normalize({**state.default_state(), "legend_order": ["a", "b"]})
    assert result["legend_order"] == ["a", "b"]


def test_a_document_that_is_not_an_object_is_refused():
    with pytest.raises(ValueError):
        state.normalize(["not", "a", "document"])


def test_a_newer_schema_is_refused_rather_than_overwritten():
    with pytest.raises(state.UnreadableState):
        state.normalize({"schema_version": state.SCHEMA_VERSION + 1})


def test_an_older_schema_is_read_and_upgraded():
    """Versioning exists so future changes are safe, not so old documents are
    thrown away."""
    result = state.normalize({"schema_version": 0, "selected": "phenotype"})
    assert result["schema_version"] == state.SCHEMA_VERSION
    assert result["selected"] == "phenotype"


# --------------------------------------------------------------------------
# The repository
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.blob = None

    def get_state(self):
        return self.blob

    def put_state(self, blob):
        self.blob = blob


@pytest.fixture
def repository(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(state.api, "store", lambda datasource, plugin: store)
    return state.CellExplorerRepository("proj")


def test_an_unsaved_project_reads_as_the_default(repository):
    assert repository.load() == state.default_state()


def test_saving_bumps_the_revision(repository):
    saved = repository.save(0, {"selected": "phenotype"})
    assert saved["revision"] == 1
    assert repository.load()["selected"] == "phenotype"


def test_a_stale_writer_is_refused_rather_than_winning(repository):
    """Two tabs, both holding a full copy. Last-writer-wins means the stale one
    quietly reinstates its whole world -- every recoloured category reverted,
    with no error and nothing to notice."""
    repository.save(0, {"selected": "phenotype"})
    with pytest.raises(state.ConflictError) as raised:
        repository.save(0, {"selected": "neighbourhood"})
    assert raised.value.current_revision == 1
    assert repository.load()["selected"] == "phenotype", "nothing was written"


def test_a_conflicting_save_stores_nothing(repository):
    repository.save(0, {"selected": "a"})
    before = repository.load()
    with pytest.raises(state.ConflictError):
        repository.save(0, {"selected": "b"})
    assert repository.load() == before


def test_a_corrupt_document_is_loud_rather_than_silently_empty(repository):
    """Handing back an empty document presents "your settings are gone" as
    "this project has no settings", and the next autosave makes it true."""
    repository._store.blob = b"{not json"
    with pytest.raises(ValueError, match="could not be read"):
        repository.load()


def test_a_non_integer_revision_is_refused(repository):
    for bad in ("1", None, True, 1.5):
        with pytest.raises(ValueError):
            repository.save(bad, {})


def test_settings_must_be_an_object(repository):
    with pytest.raises(ValueError):
        repository.save(0, ["nope"])
