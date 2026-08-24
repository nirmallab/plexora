"""Figures survive, and one session cannot silently overwrite another.

A figure is hours of work that produces no file the user can see -- there is no
document on their desktop to notice the absence of -- so every way of losing one
is a silent failure by nature. These tests are mostly about the states where a
naive implementation loses figures (concurrent saves, a damaged database, a
document from a newer build) rather than about the happy path.

The other half is the claim the storage design rests on: one file per figure,
found by scanning a directory, with no shared index. A test that only ever
created one figure could not tell that design from a central one, so several
here deliberately work with a library of them.
"""

import json
import sqlite3

import pytest

import plexora
from plexora.plugins.figure_builder.server import repository, schema
from plexora.plugins.figure_builder.server.repository import ConflictError, UnknownFigure


@pytest.fixture
def figures(tmp_path, monkeypatch):
    """A data_path of our own, so nothing here touches the user's figures."""
    return tmp_path


def title_op(title):
    return [{"op": "set_meta", "changes": {"title": title}}]


# -- the basic promise --------------------------------------------------

def test_a_new_figure_opens_with_one_page_and_no_panels(figures):
    figure_id = repository.create("Figure 1")
    document = repository.load(figure_id)

    assert document["title"] == "Figure 1"
    assert document["revision"] == 0
    assert document["panels"] == {}
    # One page, not zero: a figure with no pages has nowhere to put the first
    # captured panel, and "add a page" is not a decision anyone wants to make
    # before they have seen one.
    assert len(document["pages"]) == 1
    assert document["pages"][0]["size_mm"] == {"w": 210.0, "h": 297.0}


def test_a_figure_survives_being_closed_and_reopened(figures):
    figure_id = repository.create("Figure 1")
    repository.apply(figure_id, 0, title_op("Figure 2"))

    # A second read with nothing held in memory -- the whole point of the
    # store existing.
    assert repository.load(figure_id)["title"] == "Figure 2"


def test_each_figure_is_its_own_file(figures):
    """The decision the whole design rests on. A figure spans datasources, so
    it cannot live in any one project's database -- and one file per figure is
    what makes deleting a project unable to take a figure with it."""
    first = repository.create("One")
    second = repository.create("Two")

    root = figures / repository.FIGURES_DIRNAME
    assert (root / first / repository.DB_FILENAME).is_file()
    assert (root / second / repository.DB_FILENAME).is_file()
    assert first != second


def test_the_figures_directory_is_dot_prefixed(figures):
    """Projects are directories under the same data_path, so a project literally
    named "figures" would otherwise land on top of the library."""
    repository.create("One")
    assert repository.FIGURES_DIRNAME.startswith(".")
    assert (figures / repository.FIGURES_DIRNAME).is_dir()


def test_no_figure_holds_image_data(figures):
    """A figure keeps references, never pixels. Asserted as a size bound rather
    than by inspection: the failure this guards against is somebody one day
    storing a preview, a tile or a channel array in the document blob, and the
    symptom would be a 400 MB figure nobody could open."""
    figure_id = repository.create("One")
    path = figures / repository.FIGURES_DIRNAME / figure_id / repository.DB_FILENAME
    assert path.stat().st_size < 200 * 1024


# -- the library --------------------------------------------------------

def test_the_library_lists_every_figure_newest_first(figures):
    older = repository.create("Older")
    newer = repository.create("Newer")
    repository.apply(newer, 0, title_op("Newer still"))

    listed = repository.list_figures()
    assert [entry["figure_id"] for entry in listed][0] == newer
    assert {entry["figure_id"] for entry in listed} == {older, newer}


def test_the_listing_carries_counts_without_parsing_every_document(figures):
    """Counts are denormalised into `meta` so listing fifty figures is fifty
    small reads rather than fifty JSON parses. Asserted through the numbers
    being right, since a summary that drifts from its document is worse than no
    summary."""
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, [{"op": "add_page", "page": {"page_id": "pg_2"}}])

    entry = next(e for e in repository.list_figures() if e["figure_id"] == figure_id)
    assert entry["page_count"] == 2
    assert entry["panel_count"] == 0
    assert entry["revision"] == 1
    assert entry["readable"] is True


def test_a_damaged_figure_is_listed_as_damaged_rather_than_omitted(figures):
    """The figure that has gone wrong is exactly the one the user needs to see.
    A listing that silently skipped it would present "damaged" as "deleted"."""
    good = repository.create("Good")
    bad = repository.create("Bad")
    (figures / repository.FIGURES_DIRNAME / bad / repository.DB_FILENAME).write_bytes(
        b"this is not a database")

    listed = {entry["figure_id"]: entry for entry in repository.list_figures()}
    assert listed[good]["readable"] is True
    assert listed[bad]["readable"] is False
    # And the good one still opens: one bad file costs one card, not the library.
    assert repository.load(good)["title"] == "Good"


def test_a_stray_directory_is_not_mistaken_for_a_figure(figures):
    repository.create("One")
    (figures / repository.FIGURES_DIRNAME / "not_a_figure").mkdir()
    assert len(repository.list_figures()) == 1


def test_an_empty_library_lists_nothing_rather_than_erroring(figures):
    assert repository.list_figures() == []


# -- concurrency --------------------------------------------------------

def test_a_stale_writer_is_refused_rather_than_obeyed(figures):
    """Two tabs, both holding a full copy, both autosaving. Without this the
    stale one's next save reinstates its whole world -- deleting every panel
    captured in the other, with nothing shown to either user."""
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, title_op("From the first tab"))

    with pytest.raises(ConflictError) as caught:
        repository.apply(figure_id, 0, title_op("From the stale tab"))

    assert caught.value.current_revision == 1
    assert repository.load(figure_id)["title"] == "From the first tab"


def test_a_refused_write_leaves_the_revision_alone(figures):
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, title_op("Second"))
    with pytest.raises(ConflictError):
        repository.apply(figure_id, 0, title_op("Stale"))
    assert repository.load(figure_id)["revision"] == 1


def test_the_conflicted_writer_can_retry_once_it_catches_up(figures):
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, title_op("Second"))
    assert repository.apply(figure_id, 1, title_op("Third")) == 2


def test_revisions_only_ever_go_forwards(figures):
    """Undo is a new revision, never a rewind -- the whole conflict check
    depends on the number being monotonic."""
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, [{"op": "add_page", "page": {"page_id": "pg_2"}}])
    repository.apply(figure_id, 1, [{"op": "remove_page", "page_id": "pg_2", "panels": "tray"}])
    assert repository.load(figure_id)["revision"] == 2


@pytest.mark.parametrize("bad", ["1", None, 1.5, True])
def test_a_write_without_a_real_base_revision_is_refused(figures, bad):
    """A client that omits it, or sends something coercible, must not end up
    with an accidental force-write."""
    figure_id = repository.create("One")
    with pytest.raises(ValueError, match="base_revision"):
        repository.apply(figure_id, bad, title_op("Nope"))


def test_a_rejected_operation_does_not_consume_a_revision(figures):
    figure_id = repository.create("One")
    with pytest.raises(ValueError):
        repository.apply(figure_id, 0, [{"op": "add_page", "page": {"page_id": "pg_1"}}])
    assert repository.load(figure_id)["revision"] == 0


# -- damaged and future storage -----------------------------------------

def test_an_unreadable_document_is_reported_rather_than_read_as_empty(figures):
    """The alternative presents "your figure cannot be read" as "your figure is
    empty" -- and the next autosave then makes that true."""
    figure_id = repository.create("One")
    _write_raw(figures, figure_id, "{not json at all")

    with pytest.raises(schema.UnreadableFigure, match="could not be read"):
        repository.load(figure_id)


def test_a_figure_written_by_a_newer_plexora_is_refused(figures):
    """Reading it with today's rules would mean quietly dropping whatever the
    newer schema added -- and then writing that loss back on the next save."""
    figure_id = repository.create("One")
    _write_raw(figures, figure_id, json.dumps({
        "schema_version": schema.SCHEMA_VERSION + 5,
        "figure_id": figure_id, "revision": 3, "title": "From the future",
    }))

    with pytest.raises(schema.UnreadableFigure, match="newer version"):
        repository.load(figure_id)


def test_a_partially_unrecognisable_document_keeps_what_it_can(figures):
    """Entries that cannot be understood are dropped; the ones that can are
    kept. Losing one malformed panel is better than refusing to open a figure
    that represents a day of work."""
    figure_id = repository.create("One")
    _write_raw(figures, figure_id, json.dumps({
        "schema_version": 1, "figure_id": figure_id, "revision": 7, "title": "Mixed",
        "sources": {"src_1": {"kind": "plexora_project", "datasource": "demo"}},
        "pages": [{"page_id": "pg_1"}],
        "panels": {
            "pnl_ok": {"source_id": "src_1", "scene": {}},
            "pnl_bad": {"scene": {}},                       # no source_id
        },
    }), revision=7)

    document = repository.load(figure_id)
    assert document["revision"] == 7
    assert list(document["panels"]) == ["pnl_ok"]


def test_a_missing_figure_is_distinguishable_from_an_empty_one(figures):
    with pytest.raises(UnknownFigure):
        repository.load("fig_deadbeefcafe")


def test_opening_a_missing_figure_does_not_create_one(figures):
    """sqlite3.connect happily conjures an empty database for a path that does
    not exist, which would turn "this figure was deleted" into "this figure is
    empty" -- and the next autosave would make that true."""
    with pytest.raises(UnknownFigure):
        repository.load("fig_deadbeefcafe")
    assert repository.list_figures() == []


@pytest.mark.parametrize("bad", ["", "../escape", "fig_../x", "figures", "fig_UPPER",
                                 "fig_a", "fig_" + "a" * 40])
def test_a_figure_id_that_is_not_one_is_refused(figures, bad):
    """This value arrives from a URL and is joined onto a filesystem path. The
    pattern is narrow for that reason and nothing else."""
    with pytest.raises(ValueError):
        repository.figure_dir(bad)


# -- deleting and duplicating -------------------------------------------

def test_deleting_a_figure_removes_it_and_leaves_the_others(figures):
    keep = repository.create("Keep")
    drop = repository.create("Drop")
    repository.delete(drop)

    assert [entry["figure_id"] for entry in repository.list_figures()] == [keep]
    assert not (figures / repository.FIGURES_DIRNAME / drop).exists()


def test_deleting_a_figure_that_is_already_gone_says_so(figures):
    with pytest.raises(UnknownFigure):
        repository.delete("fig_deadbeefcafe")


def test_a_duplicate_is_independent_of_its_original(figures):
    original = repository.create("Figure 1")
    repository.apply(original, 0, [{"op": "add_page", "page": {"page_id": "pg_2"}}])

    copy = repository.duplicate(original)
    repository.apply(copy, 0, title_op("Diverged"))

    assert repository.load(original)["title"] == "Figure 1"
    assert repository.load(copy)["title"] == "Diverged"
    assert len(repository.load(copy)["pages"]) == 2


def test_a_duplicate_is_named_so_the_two_can_be_told_apart(figures):
    original = repository.create("Figure 1")
    copy = repository.duplicate(original)
    assert repository.load(copy)["title"] == "Figure 1 copy"
    assert repository.load(copy)["figure_id"] == copy


def test_a_duplicate_starts_its_revisions_over(figures):
    """Inheriting the original's number would let a tab still holding it write
    into the copy without ever looking stale."""
    original = repository.create("Figure 1")
    repository.apply(original, 0, title_op("Second"))
    copy = repository.duplicate(original)
    assert repository.load(copy)["revision"] == 0


def test_a_duplicate_carries_the_previews_rather_than_dropping_them(figures):
    """The whole reason duplicate copies files instead of replaying the
    document: re-rendering every preview is minutes of work to reproduce pixels
    that are already on disk."""
    original = repository.create("Figure 1")
    repository.put_preview(original, "pnl_1", 1, b"webp-bytes")
    copy = repository.duplicate(original)
    assert repository.get_preview(copy, "pnl_1")[0] == b"webp-bytes"


# -- previews -----------------------------------------------------------

def test_a_preview_can_be_stored_and_read_back(figures):
    figure_id = repository.create("One")
    assert repository.put_preview(figure_id, "pnl_1", 3, b"abc", width=10, height=8) is True
    data, fmt, revision = repository.get_preview(figure_id, "pnl_1")
    assert (data, fmt, revision) == (b"abc", "webp", 3)


def test_a_late_render_never_overwrites_a_newer_one(figures):
    """Previews render asynchronously and a slow one can land after a fast one
    queued later. Without this, a user who changes a channel and changes it back
    sees the FIRST render win and the panel show a state they have left."""
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl_1", 5, b"newer")

    assert repository.put_preview(figure_id, "pnl_1", 2, b"older") is False
    assert repository.get_preview(figure_id, "pnl_1")[0] == b"newer"


def test_a_preview_at_the_same_revision_is_accepted(figures):
    """Only a strictly OLDER render is refused. A re-render at the same revision
    is how a preview is repaired after a failed upload, and refusing it would
    make that unrepairable."""
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl_1", 5, b"first")
    assert repository.put_preview(figure_id, "pnl_1", 5, b"second") is True
    assert repository.get_preview(figure_id, "pnl_1")[0] == b"second"


def test_a_panel_with_no_preview_is_not_an_error(figures):
    figure_id = repository.create("One")
    assert repository.get_preview(figure_id, "pnl_1") is None


def test_previews_do_not_bump_the_document_revision(figures):
    """A preview is a rendering of the document, not a change to it. Bumping the
    revision here would greet every other open tab with a conflict banner
    because a raster finished."""
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl_1", 1, b"abc")
    assert repository.load(figure_id)["revision"] == 0


def test_an_oversized_preview_is_refused(figures):
    figure_id = repository.create("One")
    with pytest.raises(ValueError, match="larger than"):
        repository.put_preview(figure_id, "pnl_1", 1,
                               b"x" * (repository.MAX_PREVIEW_BYTES + 1))


# -- imported assets ----------------------------------------------------

def test_an_imported_file_lives_beside_the_database(figures):
    figure_id = repository.create("One")
    asset = repository.import_asset(figure_id, "schematic.png", b"\x89PNG fake")

    path = repository.asset_path(figure_id, asset["asset_id"])
    assert path.read_bytes() == b"\x89PNG fake"
    assert path.parent == figures / repository.FIGURES_DIRNAME / figure_id / "assets"
    assert asset["media_type"] == "image/png"


def test_an_imported_file_is_stored_under_its_id_not_its_name(figures):
    """The original name is data, and data does not belong in a path -- this one
    arrives from a browser and is later served back."""
    figure_id = repository.create("One")
    asset = repository.import_asset(figure_id, "../../etc/passwd.png", b"x")
    path = repository.asset_path(figure_id, asset["asset_id"])
    assert path.name.startswith(asset["asset_id"])
    assert ".." not in str(path)


@pytest.mark.parametrize("filename", ["notes.txt", "script.js", "", "archive.zip"])
def test_a_file_that_is_not_an_image_is_refused(figures, filename):
    figure_id = repository.create("One")
    with pytest.raises(ValueError):
        repository.import_asset(figure_id, filename, b"x")


def test_asset_path_answers_none_for_something_that_is_not_an_asset_id(figures):
    figure_id = repository.create("One")
    assert repository.asset_path(figure_id, "../../figure") is None


# -- the recovery journal -----------------------------------------------

def test_applied_operations_are_journalled(figures):
    """Written but not yet read back by anything: the recovery UI is a later
    milestone, and a journal that only starts being kept when the UI ships can
    recover nothing from the sessions before it."""
    figure_id = repository.create("One")
    repository.apply(figure_id, 0, title_op("Second"))

    rows = _query(figures, figure_id, "SELECT base_revision, ops_json FROM journal")
    assert len(rows) == 1
    assert rows[0][0] == 0
    assert json.loads(rows[0][1])[0]["op"] == "set_meta"


def test_the_journal_does_not_grow_without_bound(figures):
    """A long session is thousands of committed actions. An unbounded journal
    turns a 300 KB figure into a large one for a feature nobody has used yet."""
    figure_id = repository.create("One")
    for revision in range(repository.JOURNAL_LIMIT + 20):
        repository.apply(figure_id, revision, title_op(f"Title {revision}"))

    rows = _query(figures, figure_id, "SELECT COUNT(*) FROM journal")
    assert rows[0][0] <= repository.JOURNAL_LIMIT + 1


# -- helpers ------------------------------------------------------------

def _write_raw(root, figure_id, blob, revision=0):
    """Put an arbitrary string where the document should be.

    `revision` is passed separately because the COLUMN is the authority on it,
    not the copy inside the JSON -- `load` overwrites the latter with the
    former, so a test that only rewrote the blob would be asserting against a
    number the repository deliberately ignores.
    """
    path = root / repository.FIGURES_DIRNAME / figure_id / repository.DB_FILENAME
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute("UPDATE document SET json = ?, revision = ? WHERE id = 1",
                               (blob, revision))
    finally:
        connection.close()


def _query(root, figure_id, sql):
    path = root / repository.FIGURES_DIRNAME / figure_id / repository.DB_FILENAME
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()
