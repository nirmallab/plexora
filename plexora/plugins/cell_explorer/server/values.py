"""One column's values, on the wire.

A million cells is an ordinary image here, and the browser needs a value per
cell to build a colour lookup table. As JSON objects that is roughly 30 MB of
`{"cell_id": 1, "value": 0.52}` to serialize, send and parse; as a packed
structured array it is 6 MB, gzipped to less, and arrives as typed arrays the
LUT builder can read directly with no per-row work at all.

Two layouts, one per kind:

    categorical   id: uint32, code: uint16      -- an index into the descriptor's
                                                   category list
    continuous    id: uint32, value: float32

Categorical is dictionary-encoded rather than sending repeated strings, which is
where most of the saving is: "Macrophage" is eleven bytes per cell and two as a
code. The dictionary is the descriptor's `categories`, already fetched, so
nothing is sent twice.

float32 rather than float64 because this drives a colour ramp with 256 stops.
The stored data is untouched -- this is a display copy.

**The ids are the contract.** They are the same values the segmentation mask's
labels carry and the same ones the centroid cache packs, derived by the same
rule (`centroid_tiles.build_cache`): the cell-id column, cast to float, rows
with a non-finite id dropped, the rest to uint32. A different rule here would
produce a payload that is individually correct and lines up with nothing.
"""

from __future__ import annotations

import gzip

import numpy as np
import polars as pl

from plexora.plugins.cell_explorer.server import variables

#: Code for "this cell has no value". uint16 max, so it can never collide with a
#: real category index -- a column with 65535 categories is refused long before
#: this by the identifier check.
MISSING_CODE = 0xFFFF

CATEGORICAL_DTYPE = np.dtype([("id", "<u4"), ("code", "<u2")])
CONTINUOUS_DTYPE = np.dtype([("id", "<u4"), ("value", "<f4")])


def cell_ids(dataset):
    """(ids, keep) for the loaded table.

    `keep` is the row mask, returned alongside rather than applied, because
    every caller has a values array that has to lose exactly the same rows. A
    cell whose id will not survive the round trip to uint32 has no way to be
    matched against a mask label, so it is dropped rather than drawn somewhere
    arbitrary.
    """
    # geometry(), not frame(): the ids and coordinates are the part of the
    # table this server always holds, whether the file is here or on a node,
    # and this function wants nothing else.
    frame = dataset.table.geometry()
    if frame is None:
        return np.empty(0, dtype=np.uint32), np.empty(0, dtype=bool)
    column = (dataset.schema.cell_id if dataset.schema else None) or "id"
    if column not in frame.columns:
        # The role names a column of the SOURCE for the structural formats; the
        # table the adapter emits carries its own positional "id". Falling back
        # keeps a project whose role was answered against obs working.
        column = "id"
    raw = (frame[column].cast(pl.Float64, strict=False)
           .fill_null(float("nan")).to_numpy())
    keep = np.isfinite(raw)
    return raw[keep].astype(np.uint32, copy=False), keep


def encode(dataset, column: str, descriptor: dict) -> tuple[bytes, str, int]:
    """(gzipped bytes, kind, cell count) for one column.

    `descriptor` is passed in rather than looked up so the codes below are
    indexed against the very category list the client was given. Recomputing it
    here would work right up until the two orders diverged, at which point every
    cell is labelled as some other category and nothing reports it.
    """
    ids, keep = cell_ids(dataset)
    values = np.asarray(dataset.table.metadata_values(column).values)
    if values.size == keep.size:
        values = values[keep]
    elif values.size != ids.size:
        raise ValueError(
            f"column {column!r} has {values.size} values but the table has "
            f"{keep.size} rows"
        )

    kind = descriptor.get("kind", "categorical")
    if kind == "continuous":
        payload = _continuous(ids, values)
    else:
        payload = _categorical(ids, values, descriptor.get("categories") or [])
    return gzip.compress(payload.tobytes(), 5), kind, int(ids.size)


def _continuous(ids, values):
    numeric = _as_float(values)
    # Infinities join NaN as "no value". They are not the top of the scale --
    # clipping them there would paint a handful of cells the extreme colour and
    # say nothing about why.
    numeric = np.where(np.isfinite(numeric), numeric, np.nan)
    record = np.empty(ids.size, dtype=CONTINUOUS_DTYPE)
    record["id"] = ids
    record["value"] = numeric.astype(np.float32, copy=False)
    return record


def _as_float(values):
    if values.dtype.kind in "iuf":
        return values.astype(np.float64, copy=False)
    if values.dtype.kind == "b":
        return values.astype(np.float64)
    # A text column the user overrode to continuous. Anything unparseable
    # becomes NaN, which is the same "no value" every other route takes.
    return np.array([_to_float(value) for value in values], dtype=np.float64)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _categorical(ids, values, categories):
    text, missing = variables.as_text(values)
    labels = np.array([entry["value"] for entry in categories], dtype=str)

    codes = np.full(ids.size, MISSING_CODE, dtype=np.uint16)
    if labels.size and text.size:
        # Vectorized lookup: sort the dictionary, binary-search every value into
        # it, then map back through the sort permutation. A dict comprehension
        # over the values would be a Python loop over millions of rows.
        order = np.argsort(labels)
        ordered = labels[order]
        position = np.clip(np.searchsorted(ordered, text), 0, ordered.size - 1)
        matched = (ordered[position] == text) & ~missing
        codes[matched] = order[position][matched].astype(np.uint16)

    record = np.empty(ids.size, dtype=CATEGORICAL_DTYPE)
    record["id"] = ids
    record["code"] = codes
    return record
