"""Reading a vendor's transcript table.

The one assertion that matters most has nothing to do with parsing:

**Positions arrive in MICRONS and leave in REFERENCE PIXELS.** Xenium's
`x_location` is a physical coordinate, not a pixel index. Treating one as the
other is wrong by the pixel size -- a factor of nearly five for a 0.2125 um/px
run -- and the result looks exactly like a transcript layer that is registered
slightly wrong, which is the failure a registration exists to remove.

Everything else here is about what a gene selector can be used for: control
probes are real rows and are not genes, and a panel listing
"NegControlProbe_00042" beside EPCAM is one nobody can read.
"""

import numpy as np
import pytest

from plexora.plugins.transcripts.server import xenium

from tests.spatial_fixtures import has_pyarrow, write_transcripts_parquet

pytestmark = pytest.mark.skipif(
    not has_pyarrow(), reason="pyarrow is not importable in this build")


GENES = ["EPCAM", "CD3E", "PTPRC"]


@pytest.fixture
def parquet(tmp_path):
    return write_transcripts_parquet(tmp_path / "transcripts.parquet",
                                     genes=GENES, n=2_000, width=400, height=300)


# -- recognising the file ---------------------------------------------------

def test_a_xenium_table_is_recognised_by_its_columns(parquet):
    """By its columns rather than by its name: a file copied out of a run
    directory can be called anything, and a file called transcripts.parquet from
    another platform is not this."""
    assert xenium.is_xenium_transcripts(parquet)


def test_a_parquet_without_the_columns_is_not_one(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    other = tmp_path / "something_else.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), str(other))

    assert not xenium.is_xenium_transcripts(other)


def test_a_file_that_is_not_a_parquet_is_not_one(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    assert not xenium.is_xenium_transcripts(path)


def test_reading_something_else_is_refused_by_name(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(xenium.UnsupportedTranscriptFile):
        xenium.read_transcripts(path)


# -- the units --------------------------------------------------------------

def test_microns_become_reference_pixels(parquet):
    """The assertion this file exists for. A 0.2125 um/px run read as though its
    coordinates were pixels puts every transcript at a fifth of its real
    distance from the origin -- which looks like a registration that is slightly
    off rather than like a unit error."""
    _, _, x_raw, y_raw = xenium.read_transcripts(parquet)
    _, _, x_px, y_px = xenium.read_transcripts(parquet, pixel_size=0.2125)

    assert x_px.max() == pytest.approx(x_raw.max() / 0.2125, rel=1e-5)
    assert y_px.max() == pytest.approx(y_raw.max() / 0.2125, rel=1e-5)


def test_no_pixel_size_leaves_the_coordinates_alone(parquet):
    """Right only when the file is already in pixels, which is why every caller
    that has a calibration passes it."""
    _, _, x, _ = xenium.read_transcripts(parquet, pixel_size=None)
    _, _, x_again, _ = xenium.read_transcripts(parquet)

    assert np.array_equal(x, x_again)


# -- the vocabulary ---------------------------------------------------------

def test_the_vocabulary_is_the_genes_in_the_file(parquet):
    genes, index, x, y = xenium.read_transcripts(parquet)

    assert sorted(genes) == sorted(GENES)
    assert index.dtype == np.uint16
    assert len(index) == len(x) == len(y) == 2_000


def test_the_index_points_at_the_right_gene(parquet):
    """The whole file is an index into this list, so an off-by-one here draws
    every EPCAM where the CD3E should be and looks like real biology."""
    import pyarrow.parquet as pq

    genes, index, _, _ = xenium.read_transcripts(parquet)
    names = pq.read_table(str(parquet)).column("feature_name").to_pylist()

    assert [genes[i] for i in index[:50]] == names[:50]


def test_control_probes_are_dropped(tmp_path):
    """Real rows, and not genes. A selector listing NegControlProbe_00042 beside
    EPCAM is one nobody can use."""
    path = write_transcripts_parquet(
        tmp_path / "t.parquet",
        genes=("EPCAM", "NegControlProbe_00042", "BLANK_0033"), n=900)

    genes, _, _, _ = xenium.read_transcripts(path, drop_controls=True)

    assert genes == ["EPCAM"]


def test_control_probes_can_be_kept(tmp_path):
    """Somebody checking their run's background wants exactly these."""
    path = write_transcripts_parquet(
        tmp_path / "t.parquet",
        genes=("EPCAM", "NegControlProbe_00042"), n=400)

    genes, _, _, _ = xenium.read_transcripts(path, drop_controls=False)

    assert sorted(genes) == ["EPCAM", "NegControlProbe_00042"]


@pytest.mark.parametrize("name,control", [
    ("EPCAM", False),
    ("NegControlProbe_00042", True),
    ("NegControlCodeword_0500", True),
    ("BLANK_0033", True),
    ("UnassignedCodeword_0001", True),
    ("antisense_PROKR2", True),
    ("DeprecatedCodeword_0007", True),
    ("Intergenic_region_1", True),
])
def test_what_counts_as_a_control(name, control):
    assert xenium.is_control(name) is control


# -- quality ----------------------------------------------------------------

def test_quality_is_not_filtered_by_default(parquet):
    """Throwing away a third of somebody's data is not a default a viewer gets
    to choose. Xenium's own threshold is 20, and it is offered, not applied."""
    everything = len(xenium.read_transcripts(parquet)[1])
    filtered = len(xenium.read_transcripts(parquet, min_qv=20)[1])

    assert filtered < everything


def test_a_quality_threshold_keeps_what_is_above_it(parquet):
    import pyarrow.parquet as pq

    qv = np.array(pq.read_table(str(parquet)).column("qv").to_pylist())
    filtered = len(xenium.read_transcripts(parquet, min_qv=20)[1])

    assert filtered == int((qv >= 20).sum())


# -- peeking ----------------------------------------------------------------

def test_peek_reads_the_footer_rather_than_the_file(parquet):
    """Telling the user "8.4 million transcripts, 313 genes" on the import
    screen must cost kilobytes, not the whole file."""
    summary = xenium.peek(parquet)

    assert summary["rows"] == 2_000
    assert summary["is_xenium"] is True
    assert "feature_name" in summary["columns"]


def test_peek_on_something_that_is_not_a_parquet_is_none(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("nope", encoding="utf-8")

    assert xenium.peek(path) is None
