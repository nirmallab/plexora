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


_AGGREGATE = {"mean": np.mean, "sum": np.sum, "max": np.max, "min": np.min}


@pytest.mark.parametrize("how", bt.AGGREGATIONS)
def test_several_genes_aggregate_before_the_ramp(store, how):
    # Tile (7, 6) is bins 56..63 x 48..55: all tissue, and half of it the INS
    # islet, so INS and GCG differ enough for every aggregation to differ.
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    grey = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    genes = [0, 1]
    tile = bt.ramp_tile("demo", "bins", manifest, stats, 0, 7, 6, genes=genes,
                        ramp=grey, pooling=1, how=how)
    fields, _ = bt.pooled_fields("demo", "bins", manifest, 0, 7, 6,
                                 genes=genes, pooling=1)
    field = _AGGREGATE[how]([fields[g] for g in genes], axis=0)
    # The window is the genes' own automatic windows, aggregated the same
    # way, so each aggregation fills the ramp rather than saturating.
    top = _AGGREGATE[how]([bt.auto_window(manifest, stats, g, 1) for g in genes])
    expected = np.rint(np.clip(field / top, 0, 1) * 255)
    assert np.array_equal(tile[::2, ::2, 0], expected)   # supersample 2


def test_the_aggregations_draw_different_pictures(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    grey = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    drawn = {how: bt.ramp_tile("demo", "bins", manifest, stats, 0, 7, 6,
                               genes=[0, 1], ramp=grey, pooling=1, how=how)
             for how in ("max", "min", "mean")}
    assert not np.array_equal(drawn["max"], drawn["min"])
    assert not np.array_equal(drawn["mean"], drawn["min"])


def test_an_unknown_aggregation_is_the_mean(store):
    assert bt.DEFAULT_AGGREGATION == "mean"
    assert bt.aggregation(None) == "mean"
    assert bt.aggregation("median") == "mean"
    assert bt.aggregation(" MAX ") == "max"
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    grey = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    default = bt.ramp_tile("demo", "bins", manifest, stats, 0, 7, 6,
                           genes=[0, 1], ramp=grey, pooling=1)
    mean = bt.ramp_tile("demo", "bins", manifest, stats, 0, 7, 6,
                        genes=[0, 1], ramp=grey, pooling=1, how="mean")
    assert np.array_equal(default, mean)


def test_one_gene_is_the_same_under_every_aggregation(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    grey = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    tiles = [bt.ramp_tile("demo", "bins", manifest, stats, 0, 7, 6, genes=[0],
                          ramp=grey, pooling=1, how=how)
             for how in bt.AGGREGATIONS]
    assert all(np.array_equal(tiles[0], other) for other in tiles[1:])


# -- the colour scale does not move with the zoom -----------------------------

def test_the_requested_pooling_is_a_power_of_two():
    assert [bt.requested_pooling(v) for v in (None, "x", 0, 1, 3, 8, "5")] == \
        [1, 1, 1, 1, 2, 8, 4]


def test_the_effective_pooling_rounds_the_request_down(store):
    assert bt.effective_pooling(store["manifest"], 0, 5) == 4


def test_a_coarse_level_is_the_mean_of_the_requested_squares(store):
    manifest = store["manifest"]
    stats = bt.read_stats("demo", "bins")
    grey = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    # Level 2 draws pooling 2 (4 layer px per tile px, 2 per bin); 1 asked.
    scaled = bt.ramp_tile("demo", "bins", manifest, stats, 2, 1, 1, genes=[0],
                          ramp=grey, pooling=2, log=True, scale_pooling=1)
    fields, grid = bt.pooled_fields("demo", "bins", manifest, 2, 1, 1,
                                    genes=[0], pooling=2)
    expected = np.rint(bt.stretch(fields[0] / 4.0, 0,
                                  bt.auto_window(manifest, stats, 0, 1),
                                  log=True) * 255)
    assert np.array_equal(scaled[..., 0], expected)
    unscaled = bt.ramp_tile("demo", "bins", manifest, stats, 2, 1, 1,
                            genes=[0], ramp=grey, pooling=2, log=True)
    assert not np.array_equal(unscaled[..., 0], scaled[..., 0])


@pytest.fixture
def patch_store(tmp_path, monkeypatch):
    """Random counts around a constant patch: 3 per bin, bins 16..31."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    rng = np.random.default_rng(5)
    tissue = np.ones((64, 64), dtype=bool)
    ref = rng.poisson(0.7, size=(2, 64, 64)).astype(np.int64)
    ref[:, 16:32, 16:32] = 3
    source = tmp_path / "m.h5"
    source.write_bytes(b"x")
    expected = bt.expected_manifest(source, columns=64, rows=64, bin_um=2.0,
                                    tile_size=16, supersample=2,
                                    store_tile=16)
    bt.build("demo", "bins", genes=["A", "B"], blocks=_blocks(ref, tissue, rng),
             expected=expected)
    return bt.read_manifest("demo", "bins")


def test_a_uniform_region_keeps_its_colour_across_levels(patch_store):
    manifest = patch_store
    stats = bt.read_stats("demo", "bins")
    # The bug this pins: the window at a coarser pooling is not the finer
    # one scaled, so stretching each level against its own drifts the colour.
    assert bt.auto_window(manifest, stats, 0, 2) != \
        4 * bt.auto_window(manifest, stats, 0, 1)
    viridis = np.stack([np.arange(256), 255 - np.arange(256),
                        np.full(256, 90)], 1).astype(np.uint8)

    def draw(level, tx, ty, kind):
        pooling = bt.effective_pooling(manifest, level, 1)
        if kind == "ramp":
            return bt.ramp_tile("demo", "bins", manifest, stats, level, tx, ty,
                                genes=[0], ramp=viridis, pooling=pooling,
                                log=True, scale_pooling=1)
        return bt.rgb_tile("demo", "bins", manifest, stats, level, tx, ty,
                           groups=[(0, (255, 0, 0)), (1, (0, 0, 255))],
                           pooling=pooling, log=True, scale_pooling=1)

    for kind in ("ramp", "rgb"):
        # Bin (20, 20): level 0 tile (2, 2) pixel 9; level 2 tile (0, 0)
        # pixel 10 (one tile pixel per 2-bin square).
        fine, coarse = draw(0, 2, 2, kind), draw(2, 0, 0, kind)
        assert np.array_equal(fine[9, 9, :3], coarse[10, 10, :3])
        fine_alpha = np.concatenate([draw(0, x, y, kind)[..., 3].ravel()
                                     for x in (2, 3) for y in (2, 3)]).mean()
        coarse_alpha = coarse[8:16, 8:16, 3].mean()
        assert abs(fine_alpha - coarse_alpha) / fine_alpha < 0.01


def test_the_gutter_costs_the_same_ink_at_every_level(store):
    manifest = store["manifest"]
    for level in range(5):
        grid = bt._tile_grid(manifest, level, 0, 0, 8)
        assert 0.87 <= bt._alpha_grid(16, grid).mean() <= 0.89
    one_pixel = bt._tile_grid(manifest, 4, 0, 0, 8)
    assert one_pixel.size == one_pixel.step
    assert np.allclose(bt._alpha_grid(16, one_pixel), bt.GUTTER_MEAN_ALPHA)


# -- composition --------------------------------------------------------------

def test_components_parse_strictly():
    assert bt.parse_components("0|1|2:mean,3", 4) == [((0, 1, 2), "mean"),
                                                      ((3,), None)]
    assert bt.parse_components("2:MAX", 3) == [((2,), "max")]
    assert bt.parse_components("0|1:median", 2) == [((0, 1), "mean")]
    for bad in ("5", "0,0", "a", "", "0|:max", "0|1", "0,,1"):
        with pytest.raises(ValueError):
            bt.parse_components(bad, 4)


def _one(value):
    return np.full((1, 1), float(value))


def _area(x0, x1, y0, y1, index):
    return (x1[index] - x0[index]) * (y1[index] - y0[index])


def test_the_treemap_is_the_squarified_layout():
    """The reference chart's own numbers, laid out as it lays them out: the
    two largest down the left, then rows filling what is left."""
    values = np.array([35, 30, 25, 18, 15, 12, 8], float)[:, None]
    x0, x1, y0, y1 = bt._squarify(values, 0, 0, 1, 1)
    assert _area(x0, x1, y0, y1, slice(None))[:, 0] == pytest.approx(
        values[:, 0] / values.sum())
    assert x1[0, 0] == pytest.approx(x1[1, 0]) == pytest.approx(65 / 143)
    assert y1[0, 0] == pytest.approx(y0[1, 0]) and y1[1, 0] == pytest.approx(1)
    assert x0[2, 0] == pytest.approx(65 / 143) and x1[2, 0] == pytest.approx(1)
    # Every cell is closer to square than a strip would be.
    width, height = x1 - x0, y1 - y0
    assert (np.maximum(width, height) / np.minimum(width, height)).max() < 2.5


def test_the_treemap_tiles_the_square_in_any_order():
    rng = np.random.default_rng(4)
    values = rng.integers(0, 20, size=(5, 40)).astype(float)
    values[:, 0] = 0
    x0, x1, y0, y1 = bt._squarify(values, 0, 0, 1, 1)
    areas = _area(x0, x1, y0, y1, slice(None))
    live = values.sum(0) > 0
    assert areas.sum(0)[live] == pytest.approx(1.0)
    assert (areas[:, ~live] == 0).all() and (areas[values == 0] == 0).all()
    assert (x0 >= -1e-12).all() and (x1 <= 1 + 1e-12).all()
    assert (y0 >= -1e-12).all() and (y1 <= 1 + 1e-12).all()


@pytest.mark.parametrize("how, group", [("mean", 4), ("sum", 8), ("max", 6),
                                        ("min", 2)])
def test_a_groups_rule_sets_its_area_and_its_members_split_it(how, group):
    fields = [_one(6), _one(2), _one(4)]
    leaves, share, total, x0, x1, y0, y1 = bt.composition_shares(
        fields, [((0, 1), how), ((2,), None)])
    assert leaves == [0, 1, 2]
    outer = group / (group + 4)
    areas = _area(x0, x1, y0, y1, slice(None))[:, 0, 0]
    assert areas[0] + areas[1] == pytest.approx(outer)
    assert areas[2] == pytest.approx(1 - outer)
    # Inside the group the split is always the members' counts, 6 : 2 ...
    assert areas[0] / areas[1] == pytest.approx(3.0)
    assert share[:2, 0, 0] == pytest.approx([outer * 0.75, outer * 0.25])
    assert share.sum() == pytest.approx(1.0)
    # ... and the members together are one rectangle, the group's.
    gx0, gx1 = min(x0[0, 0, 0], x0[1, 0, 0]), max(x1[0, 0, 0], x1[1, 0, 0])
    gy0, gy1 = min(y0[0, 0, 0], y0[1, 0, 0]), max(y1[0, 0, 0], y1[1, 0, 0])
    assert (gx1 - gx0) * (gy1 - gy0) == pytest.approx(outer)


def test_min_gives_a_group_with_an_absent_member_no_area():
    leaves, share, *_ = bt.composition_shares(
        [_one(6), _one(0), _one(4)], [((0, 1), "min"), ((2,), None)])
    assert share[:, 0, 0] == pytest.approx([0, 0, 1])


def test_a_square_without_signal_has_no_shares():
    _, share, total, *_ = bt.composition_shares(
        [_one(0), _one(0)], [((0,), None), ((1,), None)])
    assert total[0, 0] == 0 and not share.any()


RED, GREEN, BLUE = (230, 30, 30), (30, 200, 60), (40, 60, 220)


def test_glyph_areas_are_the_shares(store):
    manifest = store["manifest"]
    # Pooling 8 at level 0: tile (7, 6) is one 16-pixel square, 7 across.
    components = [((0, 1), "max"), ((2,), None)]
    tile = bt.composition_tile("demo", "bins", manifest, 0, 7, 6,
                               genes=[0, 1, 2], colours=[RED, GREEN, BLUE],
                               components=components, pooling=8)
    a, b, c = (float(v) for v in _pooled(store["ref"], 8)[:3, 6, 7])
    assert a > 0 and b > 0 and c > 0
    rgb = tile[..., :3]
    masks = {colour: (rgb == colour).all(-1) for colour in (RED, GREEN, BLUE)}
    assert sum(int(m.sum()) for m in masks.values()) == 256   # one colour each
    group = max(a, b)
    assert abs(masks[RED].sum() + masks[GREEN].sum()
               - 256 * group / (group + c)) <= 16
    assert abs(masks[BLUE].sum() - 256 * c / (group + c)) <= 16
    assert abs(masks[RED].sum() - 256 * group / (group + c) * a / (a + b)) <= 16
    # Every cell is a solid rectangle, and so is the group's pair together.
    for mask in (masks[RED], masks[GREEN], masks[BLUE],
                 masks[RED] | masks[GREEN]):
        rows, cols = np.nonzero(mask)
        if len(rows):
            box = mask[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
            assert box.all()


def _leaf_of(rgb, palette):
    """Each pixel's leaf index by its exact colour, -1 for none."""
    out = np.full(rgb.shape[:2], -1)
    for index, colour in enumerate(palette):
        out[(rgb == colour).all(-1)] = index
    return out


def test_a_dither_draws_each_gene_in_proportion_to_its_share():
    """Too small for a treemap, a square is NOT its largest gene's colour --
    that would paint 60% as 100% and repaint the tissue when the glyphs
    appear. Over a patch, each colour's pixel count is its share."""
    from plexora.server.models import transcript_tiles as tt
    grid = tt.Grid(4, 0, 0, 256, 256, 4, 0, 0)            # one px per square
    share = np.empty((3, 256, 256))
    share[:] = np.array([0.6, 0.3, 0.1])[:, None, None]
    palette = np.asarray([RED, GREEN, BLUE], dtype=np.uint8)
    leaf = _leaf_of(bt._dither_leaves(palette, share, 256, grid), palette)
    assert (leaf >= 0).all()
    fractions = np.bincount(leaf.ravel(), minlength=3) / leaf.size
    assert fractions == pytest.approx([0.6, 0.3, 0.1], abs=0.01)
    # A gene with no share in a square never appears in it.
    share[2] = 0.0
    leaf = _leaf_of(bt._dither_leaves(palette, share, 256, grid), palette)
    assert not (leaf == 2).any()


def test_the_dither_runs_on_across_a_tile_edge():
    """Indexed by the level's pixels, not the tile's: the tile to the right
    starts where this one's pattern would have gone on."""
    from plexora.server.models import transcript_tiles as tt
    wide = bt._dither_threshold(512, tt.Grid(4, 0, 0, 512, 512, 4, 0, 0))
    right = bt._dither_threshold(256, tt.Grid(4, 256, 0, 256, 256, 4, 1024, 0))
    assert np.allclose(wide[:256, 256:], right)


def _check_dithered(tile, share, total, palette, scale):
    """Every live pixel one of its own square's genes, none elsewhere, and the
    colours in the proportions of the shares."""
    leaf = _leaf_of(tile[..., :3], palette)
    ny, nx = total.shape
    squares = np.repeat(np.repeat(np.arange(ny * nx).reshape(ny, nx), scale, 0),
                        scale, 1)[:tile.shape[0], :tile.shape[1]]
    live = (total > 0).ravel()[squares]
    assert (tile[..., 3][~live] == 0).all() and (tile[..., 3][live] > 0).all()
    flat = share.reshape(len(palette), -1)
    assert (leaf[live] >= 0).all()
    assert (flat[leaf[live], squares[live]] > 0).all()
    drawn = np.bincount(leaf[live], minlength=len(palette)) / live.sum()
    expected = flat[:, squares[live]].mean(1)
    assert drawn == pytest.approx(expected, abs=0.04)


def test_small_squares_are_dithered_among_their_genes(store):
    manifest = store["manifest"]
    components = [((0,), None), ((1, 2), "sum")]
    # Pooling 1 at level 0 is 2 px per square: under the glyph threshold.
    tile = bt.composition_tile("demo", "bins", manifest, 0, 7, 6,
                               genes=[0, 1, 2], colours=[RED, GREEN, BLUE],
                               components=components, pooling=1)
    fields, _ = bt.pooled_fields("demo", "bins", manifest, 0, 7, 6,
                                 genes=[0, 1, 2], pooling=1)
    leaves, share, total, *_ = bt.composition_shares(
        [fields[g] for g in (0, 1, 2)], components)
    palette = np.asarray([RED, GREEN, BLUE], dtype=np.uint8)
    _check_dithered(tile, share, total, palette, 2)
    off = bt.composition_tile("demo", "bins", manifest, 0, 0, 0,
                              genes=[0, 1, 2], colours=[RED, GREEN, BLUE],
                              components=components, pooling=1)
    assert off[..., 3].max() == 0


def test_a_coarse_level_composes_the_merged_squares(store):
    manifest = store["manifest"]
    components = [((0, 1), "mean"), ((2,), None)]      # positions in genes
    # Level 4 draws pooling 8 at one tile pixel per square.
    tile = bt.composition_tile("demo", "bins", manifest, 4, 0, 0,
                               genes=[0, 1, 3], colours=[RED, GREEN, BLUE],
                               components=components, pooling=8)
    pooled = _pooled(store["ref"], 8)
    _, share, total, *_ = bt.composition_shares(
        [pooled[0].astype(float), pooled[1].astype(float),
         pooled[3].astype(float)], components)
    palette = np.asarray([RED, GREEN, BLUE], dtype=np.uint8)
    ny, nx = total.shape
    live = total > 0
    leaf = _leaf_of(tile[:ny, :nx, :3], palette)
    assert (leaf[live] >= 0).all()
    assert (share.reshape(3, -1)[leaf[live], np.flatnonzero(live)] > 0).all()
    assert (tile[:ny, :nx, 3][~live] == 0).all()
