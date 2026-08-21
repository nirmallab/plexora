"""Which columns can be coloured by, and whether each is categories or numbers.

The inference lives here, on the server, for two reasons. It needs the values,
which the browser does not have until it asks for them -- and asking for a
column in order to find out whether it is worth offering defeats the point. And
it has to give the same answer to every caller: the legend's category order, the
codes in the binary payload and the colour a category gets are all derived from
one descriptor, so a second opinion computed anywhere else would silently
mislabel cells.

Three judgements are made, in increasing order of how wrong they can be:

**Categorical or continuous.** Strings, booleans and anything the source
declared as a categorical are categories. Floats are numbers. The awkward middle
is a low-cardinality integer -- `leiden` 0..12, `grade` 1..3, a 0/1 flag -- which
is stored as a number and means a category. Those are called categorical and
marked `ambiguous`, which is what puts a two-button override in the panel. Only
for those: an override control on every variable is noise on the ninety percent
that were never in doubt.

**Identifier-like.** A column with a distinct value for nearly every cell is not
a variable, it is a name. Colouring by it produces a picture with as many
colours as cells, which is indistinguishable from noise and takes a while to
draw. Those are flagged `identifier_like` and marked in the dropdown, but they
are still offered and still selectable -- "nearly every" is a heuristic, and
somebody's 40-sample cohort in a million-cell table is a real thing to want.
The flag's one hard effect is that such a column is never chosen automatically.

**Too many categories.** Between roughly thirty and a hundred, colours stop
being tellable apart but the picture is still useful with search and isolation,
so it renders with a notice. Past that it needs an explicit confirmation.

Descriptor fields, and one deliberate omission: there is no `inferred_kind`
alongside `kind`. They would always have been equal -- the server does not see
the user's overrides, which live in the plugin's own state and are applied in
the browser. What the client needs is what inference decided (`kind`) and
whether to offer the override at all (`ambiguous`), and for an ambiguous column
BOTH the category list and the numeric stats are sent, so flipping the override
is instant and needs no second request.
"""

from __future__ import annotations

import math
import re

import numpy as np

#: How a missing value arrives once the values have been flattened to text.
#: One list, used both to build the category list and to encode the values, so
#: a value counted as missing in the legend is the same one drawn transparent.
MISSING_TOKENS = ("", "nan", "NaN", "NAN", "None", "NA", "N/A", "<NA>",
                  "null", "NULL", "NaT")

#: At or below this many distinct whole numbers, a numeric column is treated as
#: categories rather than a scale -- and flagged, since it is a guess. Chosen
#: to cover cluster labels and scores comfortably (leiden rarely exceeds ~30)
#: while leaving a genuine measurement recorded in whole units alone.
CATEGORICAL_INTEGER_LIMIT = 32

#: Above this share of distinct values, over a table big enough for the share to
#: mean anything, a column behaves like a name rather than a variable.
IDENTIFIER_UNIQUE_RATIO = 0.95
IDENTIFIER_MIN_ROWS = 1000

#: Where a legend stops being readable, and where it stops being sensible.
NOTICE_CATEGORY_COUNT = 30
CONFIRM_CATEGORY_COUNT = 100


def eligible_columns(dataset) -> list[str]:
    """Every annotation column, in the source's own order.

    Nothing is held back. An earlier version of this dropped the columns that
    say how the table is READ -- the cell id, x, y -- on the grounds that
    colouring by x draws a gradient across the slide rather than a finding. That
    reasoning is sound about what is USUALLY wanted and wrong about what is
    sometimes needed: a gradient across the slide is exactly how you check that
    coordinates were imported the right way up, and a table's `sample_id` is a
    perfectly good thing to colour by. A prefilter also cannot be argued with
    from the panel, so a column it decided against simply was not there, with
    nothing on screen to say why.

    What replaced it is a flag, not a filter: `identifier_like` marks the
    columns that will draw as many colours as cells, the dropdown says so, and
    the choice stays the user's. See `_looks_like_an_identifier`.

    Source order rather than sorted: a table's column order usually means
    something to whoever produced it, and re-sorting makes a familiar file
    unfamiliar. The dropdown searches by name for finding one in a wide table.
    """
    seen = set()
    columns = []
    for name in dataset.table.metadata_columns:
        if name in seen:
            continue
        seen.add(name)
        columns.append(name)
    return columns


def describe_all(dataset) -> list[dict]:
    """A descriptor per eligible column.

    Cached against the datasource, so opening the dropdown on a five-million-row
    table does not re-scan every column each time. `Dataset.cached` entries die
    with the load generation, so a project whose data was re-read cannot be
    served descriptors derived from the previous table.
    """
    return dataset.cached(
        ("cell_explorer", "variables"),
        lambda: [describe(dataset, name) for name in eligible_columns(dataset)],
    )


def describe(dataset, column: str) -> dict:
    """One column's descriptor. Raises KeyError if the project has no such
    column."""
    values = dataset.table.metadata_values(column)
    return _describe_values(
        column,
        values.values,
        declared=values.categories,
        is_cell_id=bool(dataset.schema and dataset.schema.cell_id == column),
    )


def find(dataset, column: str) -> dict | None:
    """The descriptor for one column out of the cached set, or None.

    Read from the cached list rather than recomputed, because the category order
    in it is what the value codes are indexed against -- deriving it a second
    time invites the two drifting apart, which is not an error anywhere, just
    every cell labelled as its neighbour.
    """
    for descriptor in describe_all(dataset):
        if descriptor["name"] == column:
            return descriptor
    return None


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------

def _describe_values(column, values, declared=None, is_cell_id=False) -> dict:
    values = np.asarray(values)
    total = int(values.size)
    text, missing = as_text(values)
    valid = int(total - int(missing.sum()))

    descriptor = {
        "name": column,
        "n": total,
        "n_missing": int(total - valid),
        "ambiguous": False,
        "identifier_like": False,
        "notice": None,
    }

    numeric = _numeric_values(values)
    distinct = int(np.unique(text[~missing]).size) if valid else 0
    descriptor["n_unique"] = distinct

    if _looks_like_an_identifier(column, distinct, valid, is_cell_id):
        # A label, not a veto. The panel marks it and declines to choose it on
        # the user's behalf; picking it deliberately works like any other
        # column, which is why it is described just as fully as one.
        descriptor["identifier_like"] = True

    kind = _infer_kind(values, numeric, declared, distinct, valid)
    # The override is offered exactly where the guess was a guess: a numeric
    # column called categorical because its values are few and whole. Everything
    # else was decided by something that is not in doubt -- text is labels,
    # floats with a spread are a scale, a declared categorical says so itself.
    ambiguous = numeric is not None and kind == "categorical" and declared is None
    descriptor["kind"] = kind
    descriptor["ambiguous"] = ambiguous

    if kind == "categorical":
        descriptor.update(_categorical_payload(text, missing, declared))
    # Both halves for an ambiguous column, which costs nothing (it has at most
    # CATEGORICAL_INTEGER_LIMIT categories) and makes flipping the override a
    # client-side relabel of data already in hand rather than a refetch.
    if kind == "continuous" or ambiguous:
        descriptor["stats"] = _continuous_stats(numeric)

    return descriptor


def _infer_kind(values, numeric, declared, distinct, valid) -> str:
    if declared is not None:
        # The source called it a categorical. Nothing derived from the values
        # beats the file saying so -- that is the one signal that survives a
        # column of stringified integers.
        return "categorical"
    if values.dtype.kind == "b":
        # Two categories, not a 0-1 gradient. Rendering True/False on a
        # continuous ramp gives two colours that both look like "some of the
        # scale" rather than two labels.
        return "categorical"
    if numeric is None:
        return "categorical"
    if valid and _all_whole(numeric) and distinct <= CATEGORICAL_INTEGER_LIMIT:
        return "categorical"
    return "continuous"


def _looks_like_an_identifier(column, distinct, valid, is_cell_id) -> bool:
    if is_cell_id:
        return True
    if valid < IDENTIFIER_MIN_ROWS:
        # Below this, a high distinct ratio says nothing: forty cells with forty
        # phenotypes is a small experiment, not a barcode column.
        return False
    return distinct / valid > IDENTIFIER_UNIQUE_RATIO


def _numeric_values(values):
    """The column as float64, or None if it is not numbers.

    Text is deliberately NOT parsed back into numbers here. A CSV column of
    "1"/"2"/"3" is a column of labels as far as this is concerned -- and it is
    already handled, because a text column is categorical, which is what those
    are. Coercing would turn cluster ids into a gradient.
    """
    if values.dtype.kind in "iuf":
        return values.astype(np.float64, copy=False)
    if values.dtype.kind == "b":
        return values.astype(np.float64)
    return None


def _all_whole(numeric) -> bool:
    finite = numeric[np.isfinite(numeric)]
    return bool(finite.size) and bool(np.all(finite == np.round(finite)))


def as_text(values):
    """(text, missing) for any column.

    One normalization, shared with the encoder, so a value that becomes the
    category "Tumor" in the legend becomes the code for "Tumor" in the payload.
    Doing this twice with two rules is how a legend ends up describing a picture
    it does not match.
    """
    values = np.asarray(values)
    if values.size == 0:
        return np.empty(0, dtype="<U1"), np.zeros(0, dtype=bool)

    if values.dtype.kind in "fc":
        numeric = values.astype(np.float64, copy=False)
        missing = ~np.isfinite(numeric)
        finite = numeric[~missing]
        text = np.empty(values.shape, dtype=object)
        if finite.size and np.all(finite == np.round(finite)):
            # Whole numbers get whole-number labels. A cluster column stored as
            # float otherwise reads "3.0" in the legend, which looks like a
            # measurement rather than the label it is.
            text[~missing] = finite.astype(np.int64).astype(str)
        else:
            text[~missing] = finite.astype(str)
        text[missing] = ""
        return text.astype(str), missing

    if values.dtype.kind in "iub":
        return values.astype(str), np.zeros(values.shape, dtype=bool)

    # Object or string. None, pandas' NA and an empty CSV cell all survive the
    # cast as text, so they are recognised by name rather than by dtype.
    text = values.astype(str)
    return text, np.isin(text, MISSING_TOKENS)


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------

def _categorical_payload(text, missing, declared) -> dict:
    present = text[~missing]
    if present.size:
        labels, counts = np.unique(present, return_counts=True)
        tally = dict(zip(labels.tolist(), counts.tolist()))
    else:
        tally = {}

    order = _category_order(tally.keys(), declared)
    categories = [{"value": value, "count": tally[value]} for value in order]
    n_categories = len(categories)
    return {
        "categories": categories,
        "n_categories": n_categories,
        "notice": _cardinality_notice(n_categories),
    }


def _category_order(present, declared) -> list[str]:
    """Legend order: what the file said, else a natural sort.

    Declared levels that no cell actually has are dropped. They are real levels
    in the file, but a legend row that cannot be shown or hidden and colours
    nothing is a row that only takes up space -- and on a subset project (one
    image out of twenty) most of the declared levels are typically absent.

    A value present in the data but missing from the declared list is appended
    rather than dropped, because dropping it would leave those cells silently
    uncoloured.
    """
    present = set(present)
    if declared:
        known = [str(value) for value in declared if str(value) in present]
        extra = sorted(present - set(known), key=natural_key)
        return known + extra
    return sorted(present, key=natural_key)


_NUMBER_RUN = re.compile(r"(\d+)")


def natural_key(value: str):
    """Sort "Cluster 2" before "Cluster 10".

    Plain lexicographic order puts 10 before 2, which makes a legend of numbered
    clusters read as though the numbering were arbitrary. The tuples never
    compare a number against a string: the leading 0/1 separates them first.
    """
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part.lower())
        for part in _NUMBER_RUN.split(str(value)) if part != ""
    )


def _cardinality_notice(n_categories):
    if n_categories > CONFIRM_CATEGORY_COUNT:
        return "high_cardinality"
    if n_categories > NOTICE_CATEGORY_COUNT:
        return "many_categories"
    return None


def _continuous_stats(numeric) -> dict:
    """min/max plus the robust range the display defaults to.

    p01/p99 rather than the literal extremes, because one outlying cell
    compresses every other cell into a few percent of the ramp -- a picture that
    is technically correct and shows nothing. The true min and max are still
    reported so the panel can say what was clipped away.
    """
    if numeric is None:
        return {}
    finite = numeric[np.isfinite(numeric)]
    if not finite.size:
        return {}
    low, mid, high = (float(v) for v in np.percentile(finite, [1, 50, 99]))
    if not math.isfinite(low) or not math.isfinite(high):
        return {}
    return {
        "min": float(finite.min()),
        "max": float(finite.max()),
        "p01": low,
        "p50": mid,
        "p99": high,
        # A column where every cell holds the same number has a zero-width
        # scale. Said here rather than left for the client to rediscover by
        # dividing by it.
        "constant": bool(finite.min() == finite.max()),
    }
