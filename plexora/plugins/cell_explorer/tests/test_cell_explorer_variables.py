"""Which columns are offered, and whether each is categories or numbers.

The inference is a guess in exactly one place -- a numeric column with few whole
values -- and the tests are arranged around that. Everything else is decided by
something that is not in doubt (text is labels, a declared categorical says so),
and the risk there is not getting it wrong but quietly changing it.

Two failures worth naming, because neither raises anything:

*Colouring by an identifier.* A column with a distinct value per cell renders as
many colours as cells. It looks like noise, takes a while to draw, and the user
has no way to tell it from a real variable that happens to be messy -- so it is
flagged, and the panel says so before it is picked. Flagged, not withheld: the
heuristic is a ratio, and it has no way of knowing about the cohort column
somebody actually meant to colour by.

*A legend that does not match the picture.* The category order here IS the
dictionary the value codes index into, so a second ordering computed anywhere
else relabels every cell as its neighbour, with nothing to report it.
"""

import numpy as np
import pytest

from plexora.plugins.cell_explorer.server import variables


def describe(values, declared=None, column="v", is_cell_id=False):
    return variables._describe_values(
        column, np.asarray(values), declared=declared, is_cell_id=is_cell_id)


def labels(descriptor):
    return [entry["value"] for entry in descriptor["categories"]]


# --------------------------------------------------------------------------
# Kind inference
# --------------------------------------------------------------------------

def test_text_is_categorical():
    descriptor = describe(np.array(["Tumor", "CD8 T", "Tumor"], dtype=object))
    assert descriptor["kind"] == "categorical"
    assert descriptor["ambiguous"] is False, "nothing about text is in doubt"


def test_floats_with_a_spread_are_continuous():
    descriptor = describe(np.linspace(0.1, 0.9, 500))
    assert descriptor["kind"] == "continuous"
    assert descriptor["ambiguous"] is False


def test_a_booleans_column_is_two_categories_not_a_gradient():
    """True/False on a continuous ramp gives two colours that both read as
    "somewhere on the scale" rather than as two labels."""
    descriptor = describe(np.array([True, False, True, True]))
    assert descriptor["kind"] == "categorical"
    assert labels(descriptor) == ["False", "True"]


def test_a_declared_categorical_is_believed_over_the_values():
    """A column of stringified integers is the case nothing derived from the
    values can settle. The file saying so is the only signal that survives."""
    values = np.array(["1", "2", "1", "3"], dtype=object)
    descriptor = describe(values, declared=("1", "2", "3"))
    assert descriptor["kind"] == "categorical"
    assert descriptor["ambiguous"] is False


def test_a_low_cardinality_integer_column_is_categorical_but_flagged():
    """leiden 0..4 is stored as numbers and means clusters. Called categorical,
    and marked so the panel offers the override -- because it IS a guess."""
    descriptor = describe(np.array([0, 1, 2, 3, 4] * 40))
    assert descriptor["kind"] == "categorical"
    assert descriptor["ambiguous"] is True


def test_an_ambiguous_column_carries_both_halves():
    """So flipping the override is a relabel of data already in hand. A refetch
    there would make a two-state toggle cost a network round trip each way."""
    descriptor = describe(np.array([0, 1, 2, 3, 4] * 40))
    assert descriptor["categories"]
    assert descriptor["stats"]["max"] == 4


def test_an_unambiguous_column_carries_only_its_own_half():
    text = describe(np.array(["a", "b"], dtype=object))
    numbers = describe(np.linspace(0, 1, 500))
    assert "stats" not in text
    assert "categories" not in numbers


def test_many_distinct_whole_numbers_are_a_measurement():
    """Cell area recorded in whole pixels is still a measurement. The
    cardinality limit is what separates it from a cluster label."""
    descriptor = describe(np.arange(0, 500))
    assert descriptor["kind"] == "continuous"


def test_integer_labels_are_not_rendered_as_floats():
    """A cluster column stored as float reads "3.0" without this, which looks
    like a measurement rather than the label it is."""
    descriptor = describe(np.array([1.0, 2.0, 3.0] * 5))
    assert labels(descriptor) == ["1", "2", "3"]


# --------------------------------------------------------------------------
# Identifier-like columns
# --------------------------------------------------------------------------

def test_a_column_with_a_value_per_cell_is_flagged():
    descriptor = describe(np.arange(5000).astype(str).astype(object))
    assert descriptor["identifier_like"] is True


def test_a_flagged_column_is_still_fully_described():
    """The flag is a warning, not a veto: picking one works like picking any
    other column, so it must not need a second request to find out what it
    would show."""
    descriptor = describe(np.arange(5000).astype(str).astype(object))
    assert descriptor["kind"] == "categorical"
    assert descriptor["n_unique"] == 5000


def test_a_small_table_is_never_called_an_identifier():
    """Forty cells with forty phenotypes is a small experiment, not a barcode
    column -- the ratio says nothing at that size."""
    descriptor = describe(np.array([f"p{i}" for i in range(40)], dtype=object))
    assert descriptor["identifier_like"] is False


def test_the_cell_id_column_is_flagged_whatever_it_looks_like():
    descriptor = describe(np.array([1, 1, 2, 2]), is_cell_id=True)
    assert descriptor["identifier_like"] is True


# --------------------------------------------------------------------------
# Category order -- this IS the dictionary the codes index into
# --------------------------------------------------------------------------

def test_the_declared_order_wins_over_sorting():
    """Sorting would put "CD8 T" first. The file said otherwise, and a legend
    the user recognises beats an alphabetical one."""
    values = np.array(["CD8 T", "Tumor", "Tumor"], dtype=object)
    assert labels(describe(values, declared=("Tumor", "CD8 T"))) == ["Tumor", "CD8 T"]


def test_a_declared_level_no_cell_has_is_dropped():
    """A legend row that colours nothing and cannot be shown or hidden is a row
    that only takes up space -- and on a one-image subset of a twenty-image
    table, most declared levels are absent."""
    values = np.array(["Tumor", "Tumor"], dtype=object)
    assert labels(describe(values, declared=("Tumor", "CD8 T", "B cell"))) == ["Tumor"]


def test_a_value_missing_from_the_declared_order_is_kept():
    """Dropping it would leave those cells silently uncoloured."""
    values = np.array(["Tumor", "Surprise"], dtype=object)
    assert labels(describe(values, declared=("Tumor",))) == ["Tumor", "Surprise"]


def test_numbered_categories_sort_the_way_people_read_them():
    values = np.array(["Cluster 10", "Cluster 2", "Cluster 1"], dtype=object)
    assert labels(describe(values)) == ["Cluster 1", "Cluster 2", "Cluster 10"]


def test_categories_carry_their_counts():
    values = np.array(["a", "b", "b", "b"], dtype=object)
    assert describe(values)["categories"] == [
        {"value": "a", "count": 1}, {"value": "b", "count": 3}]


# --------------------------------------------------------------------------
# Missing values
# --------------------------------------------------------------------------

def test_missing_values_are_never_a_category():
    """Turning None into the category "None" invents a phenotype. It is counted
    and drawn as Unassigned by the panel instead."""
    values = np.array(["Tumor", None, "Tumor", float("nan")], dtype=object)
    descriptor = describe(values)
    assert labels(descriptor) == ["Tumor"]
    assert descriptor["n_missing"] == 2


def test_empty_csv_cells_count_as_missing():
    values = np.array(["Tumor", "", "NA"], dtype=object)
    assert describe(values)["n_missing"] == 2


def test_non_finite_numbers_are_missing_not_extremes():
    """Mapping an infinity to the top of the scale paints a few cells the
    extreme colour and explains nothing."""
    values = np.array([1.0, 2.0, np.inf, np.nan, -np.inf])
    descriptor = describe(values)
    assert descriptor["n_missing"] == 3
    assert descriptor["stats"]["max"] == 2.0


def test_a_column_with_no_valid_values_says_so_rather_than_inventing_a_scale():
    descriptor = describe(np.array([np.nan, np.nan]))
    assert descriptor["n_missing"] == 2
    assert descriptor["stats"] == {}


# --------------------------------------------------------------------------
# Continuous stats
# --------------------------------------------------------------------------

def test_the_auto_range_is_robust_to_one_outlier():
    """The literal max compresses every other cell into a few percent of the
    ramp -- correct, and a picture of nothing."""
    values = np.append(np.linspace(0, 1, 1000), 10_000.0)
    stats = describe(values)["stats"]
    assert stats["max"] == 10_000.0
    assert stats["p99"] < 2.0


def test_the_true_extremes_are_still_reported():
    """So the panel can say what the automatic range clipped away."""
    stats = describe(np.linspace(-5, 5, 1000))["stats"]
    assert stats["min"] == pytest.approx(-5)
    assert stats["max"] == pytest.approx(5)


def test_a_constant_column_is_flagged_rather_than_divided_by():
    stats = describe(np.full(500, 1.0))["stats"]
    assert stats["constant"] is True
    assert stats["min"] == stats["max"] == 1.0


def test_a_varying_column_is_not_flagged_constant():
    assert describe(np.linspace(0, 1, 500))["stats"]["constant"] is False


# --------------------------------------------------------------------------
# Cardinality notices
# --------------------------------------------------------------------------

def test_a_handful_of_categories_needs_no_notice():
    assert describe(np.array(["a", "b", "c"], dtype=object))["notice"] is None


def test_a_crowded_legend_says_so():
    values = np.array([f"c{i}" for i in range(50)] * 3, dtype=object)
    assert describe(values)["notice"] == "many_categories"


def test_a_legend_past_a_hundred_needs_confirming():
    values = np.array([f"c{i}" for i in range(150)] * 20, dtype=object)
    descriptor = describe(values)
    assert descriptor["notice"] == "high_cardinality"
    assert descriptor["identifier_like"] is False, (
        "150 categories over 3000 cells is crowded, not an identifier"
    )
