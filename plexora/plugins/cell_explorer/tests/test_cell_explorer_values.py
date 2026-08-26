"""The binary payload: what is in it, and that it lines up with the mask.

Two properties, and the second is the one that fails silently.

**The layout is a contract with the browser.** cellExplorerApi.js reads these
buffers as typed arrays at fixed offsets. A dtype change here is a client that
decodes garbage -- plausible ids, plausible codes, entirely wrong -- so the
field names, widths and byte order are pinned literally rather than by
round-tripping through the same constants the encoder used.

**The ids have to be the ones the mask carries.** The segmentation pyramid's
labels and the centroid cache's ids are both the cell-id column cast to float
with non-finite rows dropped, then to uint32 (centroid_tiles.build_cache). If
this encoder drops a different set of rows, every value after the first dropped
one is attached to the wrong cell -- and the picture still looks like cells.
"""

import gzip

import numpy as np
import pytest

from plexora.plugins.cell_explorer.server import values as encoder
from plexora.plugins.cell_explorer.server import variables


class FakeSchema:
    def __init__(self, cell_id="CellID"):
        self.cell_id = cell_id
        self.x = "X"
        self.y = "Y"


class FakeTable:
    def __init__(self, frame, columns):
        self._frame = frame
        self._columns = columns

    def frame(self):
        return self._frame

    def geometry(self):
        # The encoder asks for geometry() rather than frame(): it wants the
        # cell ids, which are the part of the table the server holds whether
        # the file is local or on a node. Same object here.
        return self._frame

    def metadata_values(self, column):
        from plexora.server.models.adapters import MetadataColumn

        name, values, declared = self._columns[column]
        return MetadataColumn(name=name, values=np.asarray(values), categories=declared)


class FakeDataset:
    """Enough of a Dataset for the encoder, which only reads two things."""

    def __init__(self, frame, columns, cell_id="CellID"):
        self.table = FakeTable(frame, columns)
        self.schema = FakeSchema(cell_id)


def dataset(ids, column_values, declared=None, cell_id="CellID"):
    import polars as pl

    frame = pl.DataFrame({cell_id: ids, "v": list(range(len(ids)))})
    return FakeDataset(frame, {"v": ("v", column_values, declared)}, cell_id)


def decode(payload, dtype):
    return np.frombuffer(gzip.decompress(payload), dtype=dtype)


def describe(values, declared=None):
    return variables._describe_values("v", np.asarray(values), declared=declared)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

def test_the_categorical_layout_is_what_the_client_reads():
    assert encoder.CATEGORICAL_DTYPE.names == ("id", "code")
    assert encoder.CATEGORICAL_DTYPE.itemsize == 6
    assert encoder.CATEGORICAL_DTYPE["id"].str == "<u4"
    assert encoder.CATEGORICAL_DTYPE["code"].str == "<u2"


def test_the_continuous_layout_is_what_the_client_reads():
    assert encoder.CONTINUOUS_DTYPE.names == ("id", "value")
    assert encoder.CONTINUOUS_DTYPE.itemsize == 8
    assert encoder.CONTINUOUS_DTYPE["id"].str == "<u4"
    assert encoder.CONTINUOUS_DTYPE["value"].str == "<f4"


def test_the_missing_code_cannot_collide_with_a_real_category():
    assert encoder.MISSING_CODE == 0xFFFF
    assert encoder.MISSING_CODE == np.iinfo(np.uint16).max


# --------------------------------------------------------------------------
# Categorical round trip
# --------------------------------------------------------------------------

def test_codes_index_into_the_descriptor_that_was_sent():
    values = np.array(["Tumor", "CD8 T", "Tumor", "B cell"], dtype=object)
    descriptor = describe(values)
    payload, kind, count = encoder.encode(
        dataset([1, 2, 3, 4], values), "v", descriptor)

    assert kind == "categorical"
    assert count == 4
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    order = [entry["value"] for entry in descriptor["categories"]]
    assert [order[code] for code in record["code"]] == list(values)


def test_the_dictionary_order_is_the_descriptors_not_a_second_opinion():
    """Passed in rather than recomputed. Two orderings that agree today and
    diverge later relabel every cell as its neighbour, with nothing to report
    it -- so the encoder is given the exact list the client holds."""
    values = np.array(["b", "a", "c"], dtype=object)
    descriptor = describe(values, declared=("c", "b", "a"))
    payload, _, _ = encoder.encode(dataset([1, 2, 3], values), "v", descriptor)
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert list(record["code"]) == [1, 2, 0]


def test_missing_values_get_the_sentinel_not_a_category():
    values = np.array(["Tumor", None, "Tumor"], dtype=object)
    payload, _, _ = encoder.encode(
        dataset([1, 2, 3], values), "v", describe(values))
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert list(record["code"]) == [0, encoder.MISSING_CODE, 0]


def test_a_value_the_dictionary_does_not_hold_is_missing_rather_than_wrong():
    """The safe direction. A cell whose value is not in the legend is drawn
    transparent; guessing a nearby code would colour it as a category it is
    not."""
    values = np.array(["Tumor", "Unlisted"], dtype=object)
    descriptor = {**describe(values), "categories": [{"value": "Tumor", "count": 1}]}
    payload, _, _ = encoder.encode(dataset([1, 2], values), "v", descriptor)
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert list(record["code"]) == [0, encoder.MISSING_CODE]


def test_a_column_with_no_categories_encodes_as_all_missing():
    values = np.array([None, None], dtype=object)
    payload, _, count = encoder.encode(
        dataset([1, 2], values), "v", describe(values))
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert count == 2
    assert list(record["code"]) == [encoder.MISSING_CODE] * 2


def test_integer_categories_match_the_labels_the_legend_shows():
    """The legend says "3"; the encoder has to agree, or that row colours
    nothing. Both go through variables.as_text for exactly this reason."""
    values = np.array([1.0, 2.0, 3.0])
    descriptor = describe(values)
    payload, _, _ = encoder.encode(dataset([1, 2, 3], values), "v", descriptor)
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    order = [entry["value"] for entry in descriptor["categories"]]
    assert order == ["1", "2", "3"]
    assert list(record["code"]) == [0, 1, 2]


# --------------------------------------------------------------------------
# Continuous round trip
# --------------------------------------------------------------------------

def test_continuous_values_survive_as_float32():
    values = np.linspace(0.0, 1.0, 5)
    descriptor = describe(np.linspace(0, 1, 500))
    payload, kind, _ = encoder.encode(
        dataset([1, 2, 3, 4, 5], values), "v", descriptor)
    assert kind == "continuous"
    record = decode(payload, encoder.CONTINUOUS_DTYPE)
    assert record["value"] == pytest.approx(values, abs=1e-6)


def test_infinities_become_nan_rather_than_the_top_of_the_scale():
    values = np.array([1.0, np.inf, -np.inf, np.nan])
    descriptor = {"kind": "continuous"}
    payload, _, _ = encoder.encode(dataset([1, 2, 3, 4], values), "v", descriptor)
    record = decode(payload, encoder.CONTINUOUS_DTYPE)
    assert record["value"][0] == pytest.approx(1.0)
    assert np.isnan(record["value"][1:]).all()


def test_a_text_column_forced_to_continuous_degrades_to_missing():
    """An override the user can reach, so it must not raise. Anything
    unparseable is the same "no value" every other route takes."""
    values = np.array(["1.5", "oops"], dtype=object)
    payload, _, _ = encoder.encode(
        dataset([1, 2], values), "v", {"kind": "continuous"})
    record = decode(payload, encoder.CONTINUOUS_DTYPE)
    assert record["value"][0] == pytest.approx(1.5)
    assert np.isnan(record["value"][1])


# --------------------------------------------------------------------------
# The ids -- the same rule the mask and the centroid cache use
# --------------------------------------------------------------------------

def test_ids_come_from_the_cell_id_column_as_uint32():
    values = np.array(["a", "b"], dtype=object)
    payload, _, _ = encoder.encode(
        dataset([41, 42], values), "v", describe(values))
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert list(record["id"]) == [41, 42]
    assert record["id"].dtype == np.uint32


def test_a_row_with_no_usable_id_is_dropped_along_with_its_value():
    """Both halves, or every value after the first dropped row is attached to
    the wrong cell -- and the picture still looks like cells."""
    values = np.array(["a", "b", "c"], dtype=object)
    payload, _, count = encoder.encode(
        dataset([1.0, float("nan"), 3.0], values), "v", describe(values))
    record = decode(payload, encoder.CATEGORICAL_DTYPE)
    assert count == 2
    assert list(record["id"]) == [1, 3]
    order = [entry["value"] for entry in describe(values)["categories"]]
    assert [order[code] for code in record["code"]] == ["a", "c"]


def test_the_positional_id_is_used_when_the_role_names_a_source_column():
    """For AnnData the cell_id role names a column of the FILE, while the table
    the adapter emits carries its own positional "id". Falling back keeps a
    project whose role was answered against obs working, rather than 400ing."""
    import polars as pl

    values = np.array(["a", "b"], dtype=object)
    frame = pl.DataFrame({"id": [7, 8]})
    fake = FakeDataset(frame, {"v": ("v", values, None)}, cell_id="not_in_the_table")
    payload, _, _ = encoder.encode(fake, "v", describe(values))
    assert list(decode(payload, encoder.CATEGORICAL_DTYPE)["id"]) == [7, 8]


def test_a_values_array_that_does_not_fit_the_table_is_refused():
    """Rather than encoded against whichever rows happen to line up."""
    import polars as pl

    frame = pl.DataFrame({"CellID": [1, 2, 3]})
    fake = FakeDataset(frame, {"v": ("v", np.array(["a", "b"], dtype=object), None)})
    with pytest.raises(ValueError, match="2 values"):
        encoder.encode(fake, "v", {"kind": "categorical", "categories": []})


# --------------------------------------------------------------------------
# Size -- the reason this is not JSON
# --------------------------------------------------------------------------

def test_the_payload_is_a_packed_buffer_rather_than_objects():
    values = np.array(["Macrophage"] * 10_000, dtype=object)
    descriptor = describe(values)
    payload, _, _ = encoder.encode(
        dataset(list(range(10_000)), values), "v", descriptor)
    raw = gzip.decompress(payload)
    assert len(raw) == 10_000 * encoder.CATEGORICAL_DTYPE.itemsize
    # The same rows as JSON objects are roughly 300 kB before compression.
    assert len(raw) < 100_000
