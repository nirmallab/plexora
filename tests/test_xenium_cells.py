"""Making a Xenium `cells.parquet` readable by the rest of Plexora.

The table a run ships is a correct description of the run and the wrong shape
for every consumer downstream of it: its cell ids are strings, so the centroid
cache's id column casts to NaN and drops every row, and its coordinates are
microns, so the cells that do survive land at a twentieth of their true
distance from the origin. Both are corrected once, at import, on the copy the
project owns -- never on the run directory.
"""

import numpy as np
import polars as pl
import pytest

from plexora.server.utils import xenium_cells


def _cells(path, count=5):
    pl.DataFrame({
        "cell_id": [f"cell{index:04d}-1" for index in range(count)],
        "x_centroid": np.arange(count, dtype=np.float64) * 100.0 + 10.0,
        "y_centroid": np.arange(count, dtype=np.float64) * 50.0 + 5.0,
        "transcript_counts": np.arange(count, dtype=np.int64),
    }).write_parquet(path)
    return path


def _boundaries(path, count=5, offset=1000):
    """One row per polygon VERTEX, the way a run writes them."""
    pl.DataFrame({
        "cell_id": [f"cell{index:04d}-1" for index in range(count)
                    for _ in range(3)],
        "vertex_x": np.zeros(count * 3),
        "vertex_y": np.zeros(count * 3),
        "label_id": [offset + index for index in range(count) for _ in range(3)],
    }).write_parquet(path)
    return path


# -- the numeric id -----------------------------------------------------


def test_a_string_cell_id_gets_a_numeric_counterpart(tmp_path):
    table = _cells(tmp_path / "cells.parquet")

    record = xenium_cells.normalise_cells_table(table, pixel_size=0.2125)

    frame = pl.read_parquet(table)
    assert record["cell_index_from"] == "row_number"
    assert frame["cell_index"].to_list() == [1, 2, 3, 4, 5]
    # The vendor's own code is kept: it is what the user sees in Xenium
    # Explorer and what a collaborator quotes in an email.
    assert frame["cell_id"][0] == "cell0000-1"
    assert frame.columns[:2] == ["cell_id", "cell_index"]


def test_the_runs_own_label_id_is_used_when_it_is_there(tmp_path):
    """`cell_boundaries.parquet` states a numeric id per cell, and it is the
    value a rasterised mask would carry. Inventing a row number when the run
    already numbered its cells would put the table and a future mask on
    different identifiers."""
    table = _cells(tmp_path / "cells.parquet")
    _boundaries(tmp_path / "cell_boundaries.parquet", offset=1000)

    record = xenium_cells.normalise_cells_table(
        table, pixel_size=0.2125, root=tmp_path)

    assert record["cell_index_from"] == "label_id"
    assert pl.read_parquet(table)["cell_index"].to_list() == [
        1000, 1001, 1002, 1003, 1004]


def test_a_boundary_table_that_does_not_cover_every_cell_is_not_used(tmp_path):
    """Half an answer is worse than a row number: a null `cell_index` is a
    cell the centroid cache silently drops."""
    table = _cells(tmp_path / "cells.parquet", count=5)
    _boundaries(tmp_path / "cell_boundaries.parquet", count=3)

    record = xenium_cells.normalise_cells_table(
        table, pixel_size=0.2125, root=tmp_path)

    assert record["cell_index_from"] == "row_number"


# -- the coordinates ----------------------------------------------------


def test_microns_become_reference_pixels(tmp_path):
    table = _cells(tmp_path / "cells.parquet", count=3)

    record = xenium_cells.normalise_cells_table(table, pixel_size=0.2125)

    frame = pl.read_parquet(table)
    assert record["units"] == "reference_pixels"
    assert record["pixel_size"] == pytest.approx(0.2125)
    assert frame["x_centroid"][0] == pytest.approx(10.0 / 0.2125)
    assert frame["y_centroid"][2] == pytest.approx(105.0 / 0.2125)


def test_without_a_pixel_size_the_coordinates_are_left_alone(tmp_path):
    """A guessed scale puts every cell at the wrong distance from the origin
    while looking entirely plausible."""
    table = _cells(tmp_path / "cells.parquet", count=3)

    record = xenium_cells.normalise_cells_table(table, pixel_size=None)

    assert record["units"] == "microns"
    assert pl.read_parquet(table)["x_centroid"][0] == pytest.approx(10.0)


# -- doing it twice -----------------------------------------------------


def test_a_second_pass_changes_nothing(tmp_path):
    """The edit page can re-attach a project's own copy. Scaling it again
    would put every cell at a twentieth of a twentieth."""
    table = _cells(tmp_path / "cells.parquet", count=3)
    xenium_cells.normalise_cells_table(table, pixel_size=0.2125)
    after_once = pl.read_parquet(table)

    assert xenium_cells.normalise_cells_table(table, pixel_size=0.2125) is None
    assert pl.read_parquet(table).equals(after_once)


def test_a_table_that_is_not_a_xenium_cell_table_is_left_alone(tmp_path):
    other = tmp_path / "other.parquet"
    pl.DataFrame({"id": [1, 2], "X": [1.0, 2.0], "Y": [3.0, 4.0]}) \
        .write_parquet(other)

    assert xenium_cells.normalise_cells_table(other, pixel_size=0.2125) is None
