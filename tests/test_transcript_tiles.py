"""Transcript points, tiled so a viewport read is a seek.

Mirrors `test_centroid_tiles.py` in shape, because the cache does -- and differs
in the two places the cache does, which is where the interesting assertions are.

**A gene index per tile.** Reading 10 genes out of a 300-gene panel must be 10
short seeks, not a scan of every record in the tile. Asserted against a brute
force pass over the same data, so the fast path cannot be fast and wrong.

**Counts stay counts down the pyramid.** A level-1 density bin holds the SUM of
the four level-0 bins under it. Averaging would make a hot spot fade as you zoom
away from it, which is exactly backwards, and it is the kind of wrong that looks
like a rendering choice.
"""

import json

import numpy as np
import pytest

from plexora.server.models import transcript_tiles as tt

from tests.helpers import use_data_root


GENES = ["EPCAM", "CD3E", "PTPRC", "KRT5"]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A built cache over a 512x512 grid, and the arrays it was built from."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    rng = np.random.default_rng(7)
    n = 4_000
    gene_index = rng.integers(0, len(GENES), size=n).astype(np.uint16)
    x = rng.uniform(0, 512, size=n).astype(np.float32)
    y = rng.uniform(0, 512, size=n).astype(np.float32)

    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"not really a parquet, only its mtime is read")
    expected = tt.expected_manifest(source, width=512, height=512,
                                    tile_size=128, layer_id="tx")
    manifest = tt.build("demo", "tx", genes=GENES, gene_index=gene_index,
                        x=x, y=y, expected=expected)
    return {"manifest": manifest, "genes": gene_index, "x": x, "y": y,
            "source": source, "expected": expected}


# -- the manifest -----------------------------------------------------------

def test_the_manifest_records_the_vocabulary_and_the_grid(cache):
    manifest = cache["manifest"]

    assert manifest["genes"] == GENES
    assert manifest["point_count"] == len(cache["x"])
    assert manifest["columns"] == 4 and manifest["rows"] == 4
    assert manifest["record_dtype"] == "gene:uint16,x:float32,y:float32"


def test_a_transcript_record_is_ten_bytes(cache):
    """No id field, and no padding. Nobody queries transcript 41,203,118 -- they
    ask for every EPCAM in a rectangle -- so the 4 bytes an id would cost is 200
    MB at 50 million rows, and the dtype is packed rather than aligned so the
    u2 gene does not silently become 4 bytes of its own."""
    assert tt.POINT_DTYPE.itemsize == 10
    assert "id" not in tt.POINT_DTYPE.names


def test_per_tile_counts_are_recorded(cache):
    """The client's LOD estimate reads these rather than fetching a tile to find
    out how many points are in it -- which would mean fetching the thing it is
    trying to decide whether to fetch."""
    counts = np.array(cache["manifest"]["tile_counts"])

    assert counts.shape == (4, 4)
    assert counts.sum() == len(cache["x"])


def test_a_stale_cache_is_recognised(cache, tmp_path):
    """Same three keys `centroid_tiles` compares, so the two cannot disagree
    about what "still current" means."""
    expected = cache["expected"]
    assert tt.is_current(cache["manifest"], expected)

    cache["source"].write_bytes(b"a different file entirely, with a different size")
    rebuilt = tt.expected_manifest(cache["source"], width=512, height=512,
                                   tile_size=128, layer_id="tx")

    assert not tt.is_current(cache["manifest"], rebuilt)


def test_a_layer_with_no_cache_has_no_manifest(cache):
    assert tt.read_manifest("demo", "never-built") is None


# -- reading a tile ---------------------------------------------------------

def test_a_tile_holds_exactly_the_points_inside_it(cache):
    points = tt.read_tile("demo", "tx", 0, 0)

    assert len(points) == cache["manifest"]["tile_counts"][0][0]
    assert (points["x"] < 128).all() and (points["y"] < 128).all()


def test_a_tile_that_was_never_written_reads_as_empty(cache):
    """The ordinary case: most of a slide is background, and an empty array is
    what the caller can concatenate without a special case."""
    points = tt.read_tile("demo", "tx", 99, 99)

    assert len(points) == 0
    assert points.dtype == tt.POINT_DTYPE


def test_a_tile_body_is_gene_major(cache):
    """What the index is an index INTO. Runs out of order would make every seek
    return a mixture."""
    points = tt.read_tile("demo", "tx", 1, 1)

    assert (np.diff(points["gene"].astype(np.int64)) >= 0).all()


def test_selecting_genes_matches_a_brute_force_scan(cache):
    """The fast path cannot be fast and wrong. Same tile, same genes, once
    through the index and once by reading everything and masking."""
    wanted = [GENES.index("CD3E"), GENES.index("KRT5")]

    for tx_i in range(4):
        for ty_i in range(4):
            fast = tt.read_tile("demo", "tx", tx_i, ty_i, genes=wanted)
            everything = tt.read_tile("demo", "tx", tx_i, ty_i)
            brute = everything[np.isin(everything["gene"], wanted)]

            assert len(fast) == len(brute), f"tile {tx_i},{ty_i}"
            assert sorted(fast["x"].tolist()) == sorted(brute["x"].tolist())


def test_selecting_a_gene_the_tile_does_not_have_returns_nothing(cache):
    points = tt.read_tile("demo", "tx", 0, 0, genes=[9_999])

    assert len(points) == 0


def test_selecting_no_genes_returns_nothing_rather_than_everything(cache):
    """`genes=[]` is a selection of nothing, and `genes=None` is no selection at
    all. Collapsing the two would make clearing the gene list draw the whole
    panel -- the opposite of what the user asked for."""
    assert len(tt.read_tile("demo", "tx", 0, 0, genes=[])) == 0
    assert len(tt.read_tile("demo", "tx", 0, 0, genes=None)) > 0


# -- reading a region -------------------------------------------------------

def test_a_region_read_covers_every_overlapping_tile(cache):
    bounds = {"minX": 100, "minY": 100, "maxX": 260, "maxY": 260}

    points = tt.read_region("demo", "tx", bounds)
    counts = np.array(cache["manifest"]["tile_counts"])

    # Tiles (0,0), (1,0), (2,0) x same rows -- tile-aligned, so the whole of
    # each overlapping tile comes back.
    assert len(points) == counts[0:3, 0:3].sum()


def test_a_region_read_can_be_capped(cache):
    everything = tt.read_region("demo", "tx", {"minX": 0, "minY": 0,
                                               "maxX": 512, "maxY": 512})
    capped = tt.read_region("demo", "tx", {"minX": 0, "minY": 0,
                                           "maxX": 512, "maxY": 512},
                            max_points=100)

    assert len(everything) > 100
    assert len(capped) <= 100


def test_a_capped_read_samples_rather_than_truncates(cache):
    """A prefix of a gene-major file is whichever gene sorts first and nothing
    else, which reads as "the other genes are missing" rather than as a sample."""
    capped = tt.read_region("demo", "tx", {"minX": 0, "minY": 0,
                                           "maxX": 512, "maxY": 512},
                            max_points=200)

    assert len(set(capped["gene"].tolist())) > 1


def test_a_region_read_on_a_layer_with_no_cache_is_empty(cache):
    points = tt.read_region("demo", "nothing-here",
                            {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10})

    assert len(points) == 0


# -- gene names -------------------------------------------------------------

def test_gene_names_resolve_to_indices(cache):
    assert tt.gene_indices(cache["manifest"], ["CD3E", "EPCAM"]) == [1, 0]


def test_a_gene_the_panel_does_not_carry_is_skipped(cache):
    """A saved view naming a gene a re-imported panel no longer has should draw
    the rest, not fail."""
    assert tt.gene_indices(cache["manifest"], ["CD3E", "NOT_A_GENE"]) == [1]


# -- density ----------------------------------------------------------------

def test_a_density_tile_counts_every_point_in_it(cache):
    tile = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128)

    assert tile.dtype == np.uint16
    assert tile.sum() == cache["manifest"]["tile_counts"][0][0]


def test_density_is_a_raster_the_channel_encoder_already_takes(cache):
    """The whole reason the low-zoom view costs no new client code: a density
    tile is a single-channel uint16 raster, which is exactly what
    data_model.encode_tile_array is handed for an image channel. To the viewer a
    density layer IS a channel."""
    from plexora.server.models import data_model

    tile = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128)

    payload, mimetype = data_model.encode_tile_array(
        tile, False, "fast", qmin=0, qmax=max(1, int(tile.max())))

    assert isinstance(payload, (bytes, bytearray)) and len(payload) > 0
    # The same mimetype an ordinary channel tile is served with, which is the
    # claim: the client needs no new decode path for this.
    assert mimetype in ("image/webp", "image/png")


def test_counts_stay_counts_down_the_pyramid(cache):
    """A level-1 bin is the SUM of the four level-0 bins under it. Averaging
    would make a hot spot fade as the user zooms away from it, which looks like
    a rendering choice and is a data error."""
    coarse = tt.density_tile("demo", "tx", 1, 0, 0, tile_size=128)
    fine = [tt.density_tile("demo", "tx", 0, tx_i, ty_i, tile_size=128)
            for ty_i in (0, 1) for tx_i in (0, 1)]

    assert coarse.sum() == sum(int(t.sum()) for t in fine)


def test_a_density_tile_can_be_restricted_to_some_genes(cache):
    everything = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128)
    one = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128,
                          genes=[GENES.index("CD3E")])

    assert 0 < one.sum() < everything.sum()


def test_binning_coarsens_without_losing_counts(cache):
    fine = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128, bin_size=1)
    coarse = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128, bin_size=8)

    assert coarse.shape == (16, 16)
    assert coarse.sum() == fine.sum()


def test_an_empty_density_tile_is_zeros_rather_than_an_error(cache):
    tile = tt.density_tile("demo", "tx", 0, 99, 99, tile_size=128)

    assert tile.shape == (128, 128)
    assert tile.sum() == 0


# -- refusals ---------------------------------------------------------------

def test_a_panel_too_large_for_a_uint16_index_is_refused(tmp_path, monkeypatch):
    """Refused at build time rather than wrapping silently at row 65,537, which
    would serve one gene's points under another gene's name."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "t.parquet"
    source.write_bytes(b"x")
    expected = tt.expected_manifest(source, width=16, height=16, layer_id="tx")

    with pytest.raises(ValueError, match="uint16"):
        tt.build("demo", "tx", genes=[f"g{i}" for i in range(tt.MAX_GENES + 1)],
                 gene_index=np.zeros(1, dtype=np.uint16),
                 x=np.zeros(1, dtype=np.float32), y=np.zeros(1, dtype=np.float32),
                 expected=expected)


def test_points_with_no_position_are_dropped(tmp_path, monkeypatch):
    """A NaN coordinate is a row that cannot be placed. Kept, it would be binned
    at whatever np.clip made of it -- a pile of transcripts at the origin that
    looks like real signal."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "t.parquet"
    source.write_bytes(b"x")
    expected = tt.expected_manifest(source, width=64, height=64,
                                    tile_size=64, layer_id="tx")

    manifest = tt.build(
        "demo", "tx", genes=["A"],
        gene_index=np.zeros(3, dtype=np.uint16),
        x=np.array([1.0, np.nan, 3.0], dtype=np.float32),
        y=np.array([1.0, 2.0, np.inf], dtype=np.float32),
        expected=expected)

    assert manifest["point_count"] == 1


def test_a_layer_id_that_is_not_a_filename_still_gets_a_cache(tmp_path, monkeypatch):
    """Layer ids come from element names in somebody's store, so they can hold
    anything a zarr key can."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "t.parquet"
    source.write_bytes(b"x")
    expected = tt.expected_manifest(source, width=64, height=64,
                                    tile_size=64, layer_id="points/tx run 1")

    tt.build("demo", "points/tx run 1", genes=["A"],
             gene_index=np.zeros(1, dtype=np.uint16),
             x=np.array([1.0], dtype=np.float32),
             y=np.array([1.0], dtype=np.float32), expected=expected)

    stored = tt.read_manifest("demo", "points/tx run 1")
    assert stored["layer_id"] == "points/tx run 1"
    assert len(tt.read_tile("demo", "points/tx run 1", 0, 0)) == 1
