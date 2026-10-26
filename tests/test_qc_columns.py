"""The per-cell QC columns every export writes (`cells/columns.py`).

One builder turns `qc_cells` into the columns cells.csv, the flat append, the
AnnData obs, the SpatialData table and cells.h5ad carry, so they cannot
disagree: a warning is "flagged", a note leaves a cell passing, a marker-only
flag is "channel" in its category, and who made a flag is in `source`.
"""

import polars as pl

from plexora.plugins.qc.server import schemas
from plexora.plugins.qc.server.cells import columns

L = pl.List(pl.Utf8)


def _cells(category_status=True):
    frame = pl.DataFrame({
        "cell_id": [1, 2, 3, 4, 5],
        "row": [0, 1, 2, 4, 5],
        "pass": [False, True, True, True, True],
        "action": ["exclude", "warn", "pass", "pass", "pass"],
        "primary_reason": ["region:tissue_fold", "", "", "", ""],
        "reasons": pl.Series([["region:tissue_fold", "seg_under"], ["seg_small"],
                              ["seg_over"], [], []], dtype=L),
        "reason_count": pl.Series([2, 1, 1, 0, 0], dtype=pl.Int16),
        "excluded_by": pl.Series([["region:tissue_fold"], [], [], [], []], dtype=L),
        "noted_by": pl.Series([["seg_under"], [], ["seg_over"], [], []], dtype=L),
        "background": [False] * 5,
        "unreliable_markers": pl.Series([[], [], [], ["CD3"], []], dtype=L),
        "marker_flags": pl.Series([[], [], [], ["CD3|region:out_of_focus|exclude",
                                                "CD8|rim|warn"], []], dtype=L),
        "roi_ids": pl.Series([["r_user"], [], [], ["r_blur"], []], dtype=L),
        "roi_method": ["mask", None, None, "centroid", None],
    })
    if category_status:
        frame = frame.with_columns(pl.Series("category_status", [
            ["segmentation|noted", "tissue_acquisition|excluded"],
            ["segmentation|flagged"], ["segmentation|noted"],
            ["blur_focus|channel", "tissue_acquisition|channel"], []], dtype=L))
    return frame


RESULT = {"candidates": {
    "a": {"roi_id": "r_user", "created_by": "user"},
    "b": {"roi_id": "r_blur", "detector": "blur", "created_by": "blur"},
    "c": {"roi_id": "r_ai", "created_by": "agent", "ai_decision": {"severity": "moderate"}},
    "d": {"roi_id": "r_mixed", "created_by": "blur",
          "findings": [{"candidate_id": "a"}, {"candidate_id": "b"}]}}}


def test_status_reads_a_warning_as_flagged_and_a_note_as_passing():
    """exclude -> excluded, warn -> flagged, notes only -> pass."""
    built = columns.build(_cells(), RESULT)
    by_id = dict(zip(built["cell_id"].to_list(), built["status"].to_list()))
    assert by_id == {1: "excluded", 2: "flagged", 3: "pass", 4: "pass", 5: "pass"}
    assert built["pass"].to_list() == [False, True, True, True, True]
    # The row a writer places the call on travels with it.
    assert built["row"].to_list() == [0, 1, 2, 4, 5]


def test_each_category_holds_the_cells_strongest_status_there():
    """excluded > flagged > channel (a marker only, the cell kept) > noted."""
    built = columns.build(_cells(), RESULT).to_dicts()
    assert built[0]["tissue_acquisition"] == "excluded"
    assert built[0]["segmentation"] == "noted"
    assert built[1]["segmentation"] == "flagged"
    assert built[2]["segmentation"] == "noted"
    assert built[3]["blur_focus"] == "channel" and built[3]["status"] == "pass"
    assert all(built[4][c] == "" for c in columns.CATEGORY_COLUMNS)
    # The category: the primary reason's, else the first that does more than note.
    assert built[0]["category"] == "tissue_acquisition"
    assert built[1]["category"] == "segmentation"
    assert built[2]["category"] == ""


def test_a_frame_derived_before_category_status_gets_the_same_columns():
    """A legacy frame (no `category_status`) is rebuilt from its reasons."""
    new = columns.build(_cells(), RESULT)
    old = columns.build(_cells(category_status=False), RESULT)
    for name in ("status", "segmentation", "tissue_acquisition", "blur_focus"):
        assert new[name].to_list() == old[name].to_list(), name


def test_who_flagged_a_cell_is_its_source():
    """A region drawn by hand is manual, a detector's is auto, an agent's
    verdict is ai, and a consolidated region whose findings disagree is mixed;
    every flag read on the cell itself is auto."""
    sources = columns.region_sources(RESULT)
    assert sources == {"r_user": "manual", "r_blur": "auto", "r_ai": "ai",
                       "r_mixed": "mixed"}
    built = columns.build(_cells(), RESULT).to_dicts()
    # A hand region; its seg note only notes the cell, so it does not count.
    assert built[0]["source"] == "manual"
    assert built[1]["source"] == "auto"
    assert built[3]["source"] == "auto"
    assert built[4]["source"] == ""
    both = _cells().with_columns(pl.Series("roi_ids", [["r_user", "r_blur"], [], [], [], []],
                                           dtype=L))
    assert columns.build(both, RESULT)["source"][0] == "mixed"
    assert built[0]["flag_source"] == "roi" and built[1]["flag_source"] == "direct"


def test_the_prefix_comes_off_and_goes_back_on():
    """with_prefix and strip_prefix round-trip; cell_id and row never take it."""
    built = columns.build(_cells(), RESULT)
    prefixed = columns.with_prefix(built)
    assert "cell_id" in prefixed.columns and "row" in prefixed.columns
    assert "plexora_qc_status" in prefixed.columns
    assert columns.strip_prefix(prefixed).columns == built.columns


def test_the_dictionary_names_every_column():
    """uns and the vocabulary describe every canonical column."""
    words = columns.dictionary()
    assert set(columns.ORDER) <= set(words)
    built = columns.build(_cells(), RESULT)
    assert [c for c in built.columns if c != "row"] == list(columns.ORDER)


def test_the_obsm_frames_are_reasons_markers_and_warned_markers():
    """One boolean column per whole-cell reason, per marker excluded, per
    marker only warned."""
    frames = columns.obsm_frames(_cells(), ["CD3", "CD8"])
    assert frames[columns.OBSM_REASONS].columns == list(schemas.REASONS)
    assert frames[columns.OBSM_MARKERS]["CD3"].to_list() == [False, False, False, True, False]
    assert frames[columns.OBSM_MARKERS]["CD8"].to_list() == [False] * 5
    assert frames[columns.OBSM_MARKERS_WARN]["CD8"].to_list() == [False, False, False, True,
                                                                  False]
    assert frames[columns.OBSM_MARKERS_WARN]["CD3"].to_list() == [False] * 5
