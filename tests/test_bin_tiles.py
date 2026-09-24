"""The bin store: gridded counts, pooled exactly, a gene per two seeks.

Every assertion is against a dense `ref[gene, row, column]` the fixture built
the store from, so a fast path that is fast and wrong cannot pass. The grid is
deliberately not a multiple of the store tile, the barcodes arrive shuffled
(Space Ranger's h5 is not in grid order), and the tile geometry is small enough
that several store tiles, several render levels and a stored pooling above 1
all occur.
"""

import numpy as np
import pytest
from scipy import sparse

from plexora.server.models import bin_tiles as bt

from tests.helpers import use_data_root

GENES = ["INS", "GCG", "PRSS1", "KRT19", "SST"]
ROWS, COLUMNS = 80, 96


def _blocks(ref, tissue, rng, block=700):
    """`ref` as shuffled barcode blocks, the shape the vendor reader yields."""
    rr, cc = np.nonzero(tissue)
    order = rng.permutation(len(rr))
    rr, cc = rr[order], cc[order]
    counts = ref[:, rr, cc].T                        # barcodes x genes
    out = []
    for start in range(0, len(rr), block):
        stop = min(start + block, len(rr))
        csr = sparse.csr_matrix(counts[start:stop])
        out.append((rr[start:stop], cc[start:stop], csr.indptr,
                    csr.indices, csr.data.astype(np.int32)))
    return out


@pytest.fixture
def store(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    rng = np.random.default_rng(11)
    tissue = np.zeros((ROWS, COLUMNS), dtype=bool)
    tissue[5:70, 8:90] = True
    tissue[30:40, 30:50] = False                     # a hole in the tissue
    ref = rng.poisson(0.4, size=(len(GENES), ROWS, COLUMNS)).astype(np.int64)
    ref[0, 50:60, 60:70] += 20                       # an islet of INS
    ref *= tissue
    source = tmp_path / "filtered_feature_bc_matrix.h5"
    source.write_bytes(b"only its mtime is read")
    expected = bt.expected_manifest(source, columns=COLUMNS, rows=ROWS,
                                    bin_um=2.0, tile_size=16, supersample=2,
                                    store_tile=16, layer_id="bins")
    stages = []
    manifest = bt.build("demo", "bins", genes=GENES, gene_ids=[
        f"ENSG{i:05d}" for i in range(len(GENES))],
        blocks=_blocks(ref, tissue, rng), expected=expected,
        stage=stages.append)
    return {"manifest": bt.read_manifest("demo", "bins"), "built": manifest,
            "ref": ref, "tissue": tissue, "expected": expected,
            "source": source, "stages": stages}


def _pooled(ref, pooling):
    """`ref` summed over pooling x pooling squares from the grid origin."""
    genes, rows, columns = ref.shape
    pr, pc = -(-rows // pooling), -(-columns // pooling)
    padded = np.zeros((genes, pr * pooling, pc * pooling), dtype=ref.dtype)
    padded[:, :rows, :columns] = ref
    return padded.reshape(genes, pr, pooling, pc, pooling).sum((2, 4))


def _mosaic(store, level, gene, pooling):
    """Every tile's field at one level, stitched back into one grid."""
    manifest = store["manifest"]
    span = manifest["tile_size"] * 2 ** level
    tiles_x = -(-manifest["width"] // span)
    tiles_y = -(-manifest["height"] // span)
    pr, pc = -(-ROWS // pooling), -(-COLUMNS // pooling)
    out = np.zeros((tiles_y * span, tiles_x * span), dtype=np.float64)
    parts = {}
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            fields, grid = bt.pooled_fields("demo", "bins", manifest, level,
                                            tx, ty, genes=[gene],
                                            pooling=pooling)
            parts[(tx, ty)] = (fields[gene], grid)
    full = np.zeros((pr + 64, pc + 64))
    for (tx, ty), (field, grid) in parts.items():
        full[grid.first_y:grid.first_y + grid.ny,
             grid.first_x:grid.first_x + grid.nx] = field
    del out
    return full[:pr, :pc]


def test_the_manifest_records_the_grid_and_the_vocabulary(store):
    manifest = store["manifest"]
    assert manifest["genes"] == GENES
    assert manifest["gene_ids"][2] == "ENSG00002"
    assert manifest["columns"] == COLUMNS and manifest["rows"] == ROWS
    assert manifest["store_poolings"] == [1, 4]
    assert manifest["hist_poolings"] == [1, 2, 4, 8]
    assert manifest["level_count"] == 5
    assert manifest["bin_count"] == int(store["tissue"].sum())
    assert manifest["nnz"] == int((store["ref"] > 0).sum())
    assert manifest["gene_counts"] == store["ref"].sum((1, 2)).tolist()
    assert manifest["total_index"] == len(GENES)
    assert store["stages"] == ["read", "tiling", "stats"]
    assert bt.is_current(manifest, store["expected"])


def test_a_gene_reads_back_bin_for_bin(store):
    for gene in range(len(GENES)):
        assert np.array_equal(_mosaic(store, 0, gene, 1), store["ref"][gene])


@pytest.mark.parametrize("pooling", [2, 4, 8])
def test_pooling_is_the_block_sum_from_the_grid_origin(store, pooling):
    expected = _pooled(store["ref"], pooling)
    for gene in (0, 3):
        assert np.array_equal(_mosaic(store, 0, gene, pooling), expected[gene])


def test_a_coarse_level_draws_the_same_squares(store):
    # Level 4: one tile pixel is 16 layer pixels, 8 bins -- the floor.
    manifest = store["manifest"]
    assert bt.effective_pooling(manifest, 4, 1) == 8
    assert bt.effective_pooling(manifest, 4, 16) == 16
    assert bt.effective_pooling(manifest, 0, 3) == 2
    assert np.array_equal(_mosaic(store, 4, 0, 8), _pooled(store["ref"], 8)[0])


def test_total_is_every_gene_summed(store):
    total = store["manifest"]["total_index"]
    assert bt.gene_indices(store["manifest"], ["total"]) == [total]
    assert np.array_equal(_mosaic(store, 0, total, 1), store["ref"].sum(0))
    assert np.array_equal(_mosaic(store, 0, total, 4),
                          _pooled(store["ref"].sum(0)[None], 4)[0])


def test_names_resolve_by_symbol_case_or_id(store):
    manifest = store["manifest"]
    assert bt.gene_indices(manifest, ["gcg", "ENSG00003", "nope"]) == [1, 3]


def test_nothing_draws_off_the_tissue(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    tile = bt.ramp_tile("demo", "bins", manifest, stats, 0, 1, 1,
                        genes=[manifest["total_index"]],
                        ramp=np.zeros((256, 3), np.uint8) + 200, pooling=1)
    assert tile.shape == (16, 16, 4)
    # Tile (1, 1) at level 0 covers bins 8..15 in both axes; rows 5+ and
    # columns 8+ are tissue, so only the first bin row... is not.
    assert tile[..., 3].max() > 0
    empty = bt.ramp_tile("demo", "bins", manifest, stats, 0, 0, 0,
                         genes=[manifest["total_index"]],
                         ramp=np.zeros((256, 3), np.uint8) + 200, pooling=1)
    assert empty[..., 3].max() == 0                   # bins 0..7: no tissue
    hole = bt.tissue_field("demo", "bins", manifest,
                           bt._tile_grid(manifest, 0, 5, 4, 1), 1)
    assert not hole.any()                             # rows 32..39, cols 40..47


def test_the_composite_is_transparent_where_the_selection_is_absent(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    tile = bt.rgb_tile("demo", "bins", manifest, stats, 0, 7, 6,
                       groups=[(0, (255, 0, 0))], pooling=1)
    fields, _ = bt.pooled_fields("demo", "bins", manifest, 0, 7, 6,
                                 genes=[0], pooling=1)
    blocks = tile[::2, ::2]
    assert ((blocks[..., 3] > 0) == (fields[0] > 0)).all()
    assert (blocks[..., 0][fields[0] > 0] == 255).all()
    assert (blocks[..., 1] == 0).all()


def test_the_gutter_draws_between_squares_when_they_are_wide_enough(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    # Pooling 8 at level 0 is a 16-pixel square: the gap is one pixel.
    tile = bt.ramp_tile("demo", "bins", manifest, stats, 0, 2, 2,
                        genes=[manifest["total_index"]],
                        ramp=np.zeros((256, 3), np.uint8) + 200, pooling=8)
    assert tile[0, 5, 3] < tile[5, 5, 3]


def test_the_automatic_window_is_the_ninety_ninth_percentile(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    for gene, pooling in ((0, 1), (1, 4), (manifest["total_index"], 8)):
        field = (_pooled(store["ref"], pooling)[gene] if gene < len(GENES)
                 else _pooled(store["ref"].sum(0)[None], pooling)[0])
        values = field[field > 0]
        window = bt.auto_window(manifest, stats, gene, pooling)
        # Rank ceil(0.99 n) of the non-empty squares, to within the bucket
        # the histogram puts it in.
        rank = np.sort(values)[int(np.ceil(0.99 * len(values))) - 1]
        assert window <= values.max()
        assert abs(window - rank) <= max(1.0, 0.05 * rank)


def test_stretch_is_monotone_and_saturates_at_the_top():
    values = np.array([0.0, 1.0, 5.0, 10.0, 50.0])
    for log in (False, True):
        out = bt.stretch(values, 0, 10, log)
        assert (np.diff(out) >= 0).all()
        assert out[3] == pytest.approx(1.0) and out[-1] == 1.0 and out[0] == 0


def test_a_square_under_the_cursor_is_its_counts(store):
    manifest = store["manifest"]
    counts = bt.square_at("demo", "bins", manifest, 65, 55, [0, 1], pooling=1)
    assert counts == {0: int(store["ref"][0, 55, 65]),
                      1: int(store["ref"][1, 55, 65])}
    pooled = bt.square_at("demo", "bins", manifest, 65, 55, [0], pooling=4)
    assert pooled[0] == int(_pooled(store["ref"], 4)[0, 13, 16])


def test_a_changed_source_is_stale(store):
    import os

    stat = store["source"].stat()
    os.utime(store["source"], ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    fresh = bt.expected_manifest(store["source"], columns=COLUMNS, rows=ROWS,
                                 bin_um=2.0, tile_size=16, supersample=2,
                                 store_tile=16, layer_id="bins")
    assert not bt.is_current(store["manifest"], fresh)


def test_a_saturated_count_is_clamped_and_counted(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "m.h5"
    source.write_bytes(b"x")
    expected = bt.expected_manifest(source, columns=4, rows=4, bin_um=2.0,
                                    tile_size=16, supersample=4,
                                    store_tile=16)
    csr = sparse.csr_matrix(np.array([[70_000]], dtype=np.int64))
    manifest = bt.build("demo", "bins", genes=["A"], blocks=[(
        np.array([1]), np.array([2]), csr.indptr, csr.indices, csr.data)],
        expected=expected)
    assert manifest["saturated_records"] == 1
    assert bt.square_at("demo", "bins", manifest, 2, 1, [0]) == {0: 65_535}


def test_supersample_must_be_a_power_of_two(tmp_path):
    with pytest.raises(ValueError):
        bt.expected_manifest(tmp_path / "x", columns=4, rows=4, bin_um=2.0,
                             supersample=3)


def test_a_coarse_stored_pooling_tiles_less_ground_per_tile(tmp_path, monkeypatch):
    """Pooled levels tile a bounded patch of ground, and still read back exact.

    One tile side for every pooling made a 32 micron store tile a sixteenth
    of a whole slide -- the build's memory peak. Here pooling 16 is stored on
    tiles of 16 squares, not 64, and the squares it serves are still the
    block sums."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    rng = np.random.default_rng(2)
    tissue = np.ones((ROWS, COLUMNS), dtype=bool)
    ref = rng.poisson(0.5, size=(2, ROWS, COLUMNS)).astype(np.int64)
    source = tmp_path / "m.h5"
    source.write_bytes(b"x")
    expected = bt.expected_manifest(source, columns=COLUMNS, rows=ROWS,
                                    bin_um=2.0, tile_size=8, supersample=1,
                                    store_tile=64)
    assert expected["store_poolings"] == [1, 4, 16]
    assert expected["store_sides"] == {"1": 64, "4": 64, "16": 16}
    bt.build("demo", "bins", genes=["A", "B"], blocks=_blocks(ref, tissue, rng),
             expected=expected)
    manifest = bt.read_manifest("demo", "bins")
    store = {"manifest": manifest}
    for pooling in (16, 32):
        got = _mosaic(store, 4, 1, pooling)
        assert np.array_equal(got, _pooled(ref, pooling)[1])
