"""A column of numbers is described without turning every value into text.

`describe` used to make the text of every value of every column (`as_text`)
just to count its distinct values: on a 202 k-cell table, most of Cell
Explorer's first `variables`. `_count_numbers` counts numbers as numbers, the
text is made only for a column that turns out to be categorical, and every
descriptor is exactly what the text made it.
"""

import numpy as np
import pytest

from plexora.plugins.cell_explorer.server import variables

rng = np.random.default_rng(11)

COLUMNS = {
    "floats": rng.normal(size=5000),
    "floats_with_nan_inf": np.where(rng.random(5000) < 0.1, np.nan,
                                    np.where(rng.random(5000) < 0.05, np.inf, rng.normal(size=5000))),
    "signed_zeros": np.array([0.0, -0.0, 0.5, -0.5, 0.0, np.nan]),
    "whole_floats": rng.integers(0, 12, 5000).astype(np.float64),
    "whole_floats_and_nan": np.where(rng.random(5000) < 0.2, np.nan, rng.integers(-3, 3, 5000).astype(np.float64)),
    "whole_with_negative_zero": np.array([0.0, -0.0, 1.0, 2.0]),
    "float32": rng.normal(size=3000).astype(np.float32),
    "ints": rng.integers(0, 40, 5000),
    "ids": np.arange(5000, dtype=np.uint32),
    "big_ints": np.array([2**62, 2**62 + 1, -(2**62), 7], dtype=np.int64),
    "bools": rng.random(1000) < 0.3,
    "all_nan": np.full(10, np.nan),
    "empty_float": np.empty(0, dtype=np.float64),
    "empty_int": np.empty(0, dtype=np.int64),
}


@pytest.mark.parametrize("name", sorted(COLUMNS))
def test_numbers_are_counted_as_their_text_counts_them(name):
    values = COLUMNS[name]
    text, missing = variables.as_text(values)
    counted_missing, counted_distinct = variables._count_numbers(values)

    assert np.array_equal(counted_missing, missing)
    expected = int(np.unique(text[~missing]).size) if (~missing).any() else 0
    assert counted_distinct == expected


@pytest.mark.parametrize("name", sorted(COLUMNS))
def test_every_descriptor_is_what_the_text_made_it(name, monkeypatch):
    values = COLUMNS[name]
    fast = variables._describe_values(name, values)
    monkeypatch.setattr(variables, "_count_numbers", lambda values: None)
    slow = variables._describe_values(name, values)
    assert fast == slow


def test_text_columns_still_go_through_the_text():
    assert variables._count_numbers(np.array(["a", "b"], dtype=object)) is None
    assert variables._count_numbers(np.array(["a", "b"])) is None
