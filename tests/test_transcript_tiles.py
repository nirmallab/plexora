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
    q = rng.integers(0, 41, size=n).astype(np.uint8)

    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"not really a parquet, only its mtime is read")
    expected = tt.expected_manifest(source, width=512, height=512,
                                    tile_size=128, layer_id="tx")
    manifest = tt.build("demo", "tx", genes=GENES, gene_index=gene_index,
                        x=x, y=y, q=q, expected=expected)
    return {"manifest": manifest, "genes": gene_index, "x": x, "y": y,
            "q": q, "source": source, "expected": expected}


# -- the manifest -----------------------------------------------------------

def test_the_manifest_records_the_vocabulary_and_the_grid(cache):
    manifest = cache["manifest"]

    assert manifest["genes"] == GENES
    assert manifest["point_count"] == len(cache["x"])
    assert manifest["columns"] == 4 and manifest["rows"] == 4
    assert manifest["record_dtype"] == "gene:uint16,x:float32,y:float32,q:uint8"


def test_a_transcript_record_is_eleven_bytes(cache):
    """No id field, and no padding. Nobody queries transcript 41,203,118 -- they
    ask for every EPCAM in a rectangle -- so the 4 bytes an id would cost is 200
    MB at 50 million rows, and the dtype is packed rather than aligned so the
    u2 gene does not silently become 4 bytes of its own.

    The eleventh byte is the quality score, and it earns its place: it is what
    lets the Q threshold be a slider that redraws rather than a build option
    that would take twenty minutes to change your mind about."""
    assert tt.POINT_DTYPE.itemsize == 11
    assert "id" not in tt.POINT_DTYPE.names
    assert tt.POINT_DTYPE["q"] == np.uint8


def test_the_quality_score_survives_the_round_trip(cache):
    """Per point, not per tile. The client discards below the threshold in its
    vertex shader, so a score that did not arrive intact is a slider that
    hides the wrong molecules."""
    points = tt.read_region("demo", "tx",
                            {"minX": 0, "minY": 0, "maxX": 512, "maxY": 512})

    assert sorted(points["q"].tolist()) == sorted(cache["q"].tolist())


def test_a_quality_floor_drops_the_calls_below_it(cache):
    """Applied server side only for the DENSITY path -- the point path sends
    the score and lets the shader decide."""
    everything = tt.read_region("demo", "tx",
                                {"minX": 0, "minY": 0, "maxX": 512, "maxY": 512})
    above = tt.read_region("demo", "tx",
                           {"minX": 0, "minY": 0, "maxX": 512, "maxY": 512},
                           min_q=20)

    assert len(above) == int((cache["q"] >= 20).sum())
    assert len(above) < len(everything)
    assert above["q"].min() >= 20


def test_a_file_with_no_quality_column_keeps_every_point(tmp_path, monkeypatch):
    """255 rather than 0. A reader whose format states no score should not
    have every one of its points vanish the first time somebody touches the
    threshold."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root").mkdir(parents=True, exist_ok=True)
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "t.parquet"
    source.write_bytes(b"x")
    expected = tt.expected_manifest(source, width=128, height=128,
                                    tile_size=128, layer_id="tx")
    tt.build("demo", "tx", genes=["A"], gene_index=np.zeros(5, np.uint16),
             x=np.arange(5, dtype=np.float32), y=np.arange(5, dtype=np.float32),
             expected=expected)

    points = tt.read_region("demo", "tx",
                            {"minX": 0, "minY": 0, "maxX": 128, "maxY": 128},
                            min_q=40)

    assert len(points) == 5


def test_the_manifest_counts_every_gene(cache):
    """In vocabulary order, including a gene with no calls at all: "we looked
    and found none" is a result, and a selector that silently omitted the row
    would read as "this panel does not have that gene"."""
    counts = cache["manifest"]["gene_counts"]

    assert len(counts) == len(GENES)
    assert sum(counts) == len(cache["x"])
    for index in range(len(GENES)):
        assert counts[index] == int((cache["genes"] == index).sum())


def test_a_cache_written_at_the_old_stride_is_not_current(cache):
    """The load-bearing check. A 10-byte cache decoded at 11 bytes does not
    come back slightly wrong -- it comes back as noise, every point somewhere
    else on the slide, plausibly distributed, with nothing on screen to say
    so."""
    older = {**cache["manifest"], "version": 1,
             "record_dtype": "gene:uint16,x:float32,y:float32"}

    assert not tt.is_current(older, cache["expected"])


def test_several_genes_composite_into_one_coloured_tile(cache):
    """One request instead of one per gene. Each gene keeps its own contrast
    window -- a rare gene and an abundant one share a tile and must not share
    a stretch -- and the sum is clipped rather than wrapped, so a bin where
    three genes are dense reads as white and not as black."""
    red = tt.density_rgb_tile("demo", "tx", 0, 0, 0,
                              groups=[([0], (255, 0, 0), 4)], tile_size=128)
    both = tt.density_rgb_tile("demo", "tx", 0, 0, 0,
                               groups=[([0], (255, 0, 0), 4),
                                       ([1], (0, 0, 255), 4)], tile_size=128)

    assert red.shape == (128, 128, 3) and red.dtype == np.uint8
    assert red[..., 1].max() == 0 and red[..., 2].max() == 0
    # The blue group added something the red one had not.
    assert both[..., 2].max() > 0
    assert np.array_equal(both[..., 0], red[..., 0])
    assert both.max() <= 255


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
    """A coarser bin holds more, and the tile is still a tile.

    The raster comes back at the TILE's size whatever the bin size is, one
    flat block per bin -- OpenSeadragon was told how big this tile is when
    the layer was added, and a 16x16 array served for a 128 tile is a
    sixteenth of the slide drawn over all of it. So the counts are checked on
    one sample per block rather than on the sum, which the blocks repeat.

    The sample is taken one pixel INTO each block, because the first row and
    column of one is the gap between the boxes (`_gutter`), drawn down
    towards zero. Every other pixel of a block is the bin's count.
    """
    fine = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128)
    coarse = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128, bin_pixels=8)

    assert coarse.shape == fine.shape == (128, 128)
    assert coarse[1::8, 1::8].sum() == fine.sum()
    # And it really is coarser: the busiest 8x8 block holds more than the
    # busiest single pixel did.
    assert coarse.max() > fine.max()


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


# -- the density map's controls ---------------------------------------------

def test_a_bin_size_is_the_same_physical_size_at_every_level():
    """THE PROPERTY THE WHOLE CONTROL RESTS ON, and EXACTLY rather than
    approximately. A bin measured in level pixels would halve in area every
    time the user zoomed out, so a density map's numbers would mean something
    different in every frame -- and the contrast window, which is a multiple
    of the average bin, would be wrong by the same factor.

    It used to be approximate: the bin count was rounded to fit the tile, so
    188 pixels was drawn as 204.8 at level 0 and 186.2 at level 1. That is
    visible -- the boxes resize and the grid shifts as the viewer crosses a
    level, and a patch of tissue changes colour -- so the size is now the one
    that was asked for, at every level."""
    sizes = [tt.bin_pixels_for(1024, level, 188) for level in range(7)]

    assert sizes == [188] * 7


def test_a_bin_is_the_same_patch_of_tissue_at_every_level():
    """The other half of it: the grid is anchored to the IMAGE, so a bin
    boundary falls on the same tissue however finely the tile is drawn. A
    grid cut from each tile's own corner would shift by up to a bin every
    time the level changed, which is the same bug seen from the side."""
    for level in range(5):
        # The tile holding image pixel 20,480 at this level, and the bin that
        # pixel falls in -- which must be bin 108 (20480 // 188) at all of
        # them, not "the fourth bin of whichever tile this is".
        span = 1024 * (2 ** level)
        grid = tt.grid_for(1024, level, 20_480 // span, 0, 188)
        column = (20_480 - grid.origin_x) // grid.step
        index = tt._block_index(grid, 1024)[0][column]

        assert grid.size == 188, level
        assert index + grid.first_x == 20_480 // 188, level


def test_no_bin_size_asked_for_is_what_was_always_served():
    """One bin per LEVEL pixel, which is what every density tile was before
    this control existed."""
    for level in range(4):
        grid = tt.grid_for(1024, level, 0, 0, None)
        assert grid.nx == grid.ny == 1024
        assert grid.size == 2 ** level


def test_a_bin_cannot_be_finer_than_the_tile_it_is_drawn_into():
    """At a coarse level a tile covers more ground than it has pixels, so the
    bins are widened rather than the raster being asked for more rows than it
    has."""
    assert tt.bin_pixels_for(1024, 8, 10) == 256
    assert tt.grid_for(1024, 8, 0, 0, 10).nx == 1024


def test_a_bin_holds_the_same_molecules_at_every_level(cache):
    """THE BUG THE USER REPORTED, as an assertion: zooming changed the colour.

    Two things were behind it and both are here. The bin was rounded to fit
    the tile, so it was drawn 204.8 image pixels wide at one level and 186.2
    at the next -- and the grid was cut from each tile's own corner, so the
    boxes shifted as well as resized. A box now holds the same molecules
    whatever level is drawing it, which is what "a bin is 40 microns" has to
    mean if the colour is to mean anything.

    Checked on the FIELD rather than on the tile, because a tile is the field
    magnified by a different amount at each level -- the numbers are the
    claim, and the pixels are a drawing of them.
    """
    # Global bins around image pixel 256, which every level below draws.
    want = [(bx, by) for by in (9, 10) for bx in (9, 10)]
    seen = []
    for level in range(3):
        span = 128 * (2 ** level)
        grid = tt.grid_for(128, level, 256 // span, 256 // span, 30)
        field = tt._density_field("demo", "tx", level, 256 // span, 256 // span,
                                  indices=None, tile_size=128, grid=grid,
                                  min_q=None)
        assert grid.size == 30, "the bin is the size that was asked for"
        seen.append([float(field[by - grid.first_y, bx - grid.first_x])
                     for bx, by in want])

    assert seen[0] == seen[1] == seen[2], seen
    assert sum(seen[0]) > 0, "an empty patch would pass this by accident"


def test_a_ramp_tile_reads_one_field_off_a_colour_scale(cache):
    """The other half of the density story. Per-gene colours answer "where is
    each of these genes"; a ramp answers "where is there a lot", which is one
    field and a colour that is a quantity."""
    from plexora.server.utils import colormaps

    ramp = colormaps.ramp("viridis")
    tile = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None,
                                ramp=ramp, ceiling=2.0, tile_size=128)

    # RGBA: the alpha is the gap between the boxes, which at one bin per
    # pixel there is no room for -- so this tile is opaque throughout.
    assert tile.shape == (128, 128, 4) and tile.dtype == np.uint8
    assert tile[..., 3].min() == 255
    # Every colour drawn is one of the ramp's, which a summed per-gene
    # composite would not be.
    drawn = {tuple(pixel) for pixel in tile[..., :3].reshape(-1, 3)}
    allowed = {tuple(row) for row in ramp} | {(0, 0, 0)}
    assert drawn <= allowed
    assert len(drawn) > 3, "a ramp that drew one colour is not reading a field"


def test_an_empty_bin_is_the_ramps_bottom_colour_and_not_a_hole(cache):
    """A heat map that stops where the molecules stop is a picture of the
    selection's outline: the eye reads the gaps as "not measured" rather than
    as "none here", and the legend's bottom end labels a colour that was
    never drawn. Zero is a value, so it gets painted."""
    from plexora.server.utils import colormaps

    ramp = colormaps.ramp("viridis")
    tile = tt.density_ramp_tile("demo", "tx", 0, 9, 9, indices=None,
                                ramp=ramp, ceiling=2.0, tile_size=128)

    # A tile with no transcripts under it is one flat block of the ramp's
    # zero end, edge to edge -- not transparent, and not some other colour.
    assert np.array_equal(tile[..., :3], np.broadcast_to(
        ramp[0], tile[..., :3].shape)), "empty tile is not ramp[0]"
    # The pairing this relies on: `source-over` in the browser. Added with
    # `lighter` instead, this wash would sit on top of the morphology image.
    assert ramp[0].max() > 0, "viridis' zero end is a colour, not black"


def test_narrowing_the_threshold_brightens_what_is_left(cache):
    """The window is a pair of FRACTIONS of the automatic ceiling, so raising
    the floor and lowering the roof stretches what remains across the whole
    ramp rather than clipping it away."""
    from plexora.server.utils import colormaps

    ramp = colormaps.ramp("viridis")
    wide = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None, ramp=ramp,
                                ceiling=4.0, tile_size=128)
    narrow = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None, ramp=ramp,
                                  ceiling=4.0, tile_size=128, low=0.0, high=0.25)

    # Same picture, stretched: every bin that was drawn at all is at least as
    # far up the ramp as it was. Colour only -- the alpha plane is the grid,
    # and it is the same grid in both.
    assert narrow[..., :3].astype(int).sum() > wide[..., :3].astype(int).sum()


def test_a_coarse_bin_is_a_grid_of_flat_blocks(cache):
    """A density map with 40-micron bins IS a grid of squares, and the tile
    has to come back at the size OpenSeadragon was told it is -- a 16x16
    array served for a 128 tile would be a sixteenth of the slide drawn over
    all of it."""
    from plexora.server.utils import colormaps

    tile = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None,
                                ramp=colormaps.ramp("viridis"), ceiling=2.0,
                                tile_size=128, bin_pixels=16)

    assert tile.shape == (128, 128, 4)
    # The box, without the one-pixel gap down its leading edge.
    block = tile[1:16, 1:16]
    assert np.array_equal(block, np.broadcast_to(tile[1, 1], block.shape))


def test_a_bin_size_that_does_not_divide_the_tile_leaves_no_seam(cache):
    """A bin that does not divide the tile straddles its edge, and the two
    halves have to line up. The grid is the image's, so the boxes run
    straight across a tile boundary: the last box of one tile and the first
    of the next are the two parts of ONE box, not two boxes of their own
    with a double-width gap between them."""
    from plexora.server.utils import colormaps

    for bin_pixels in (7, 30, 94, 376):
        left = tt.grid_for(128, 0, 0, 0, bin_pixels)
        right = tt.grid_for(128, 0, 1, 0, bin_pixels)
        tile = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None,
                                    ramp=colormaps.ramp("viridis"), ceiling=2.0,
                                    tile_size=128, bin_pixels=bin_pixels)
        assert tile.shape == (128, 128, 4), bin_pixels

        # Global bin ids, tile pixel by tile pixel, across the boundary.
        ids = np.concatenate([
            tt._block_index(left, 128)[0] + left.first_x,
            tt._block_index(right, 128)[0] + right.first_x])
        # Every bin in between is drawn its full width, and every bin appears
        # exactly once: no bin skipped at the seam, none drawn twice.
        runs = np.bincount(ids - ids[0])
        assert (runs[1:-1] == bin_pixels).all(), bin_pixels
        assert (np.diff(np.unique(ids)) == 1).all(), bin_pixels


# -- the gap between the boxes ----------------------------------------------
#
# A density map IS a grid of boxes, and boxes drawn edge to edge read as a
# continuous field -- a resolution the bins do not have. The gap is also the
# only place the morphology shows through a ramp that covers every bin, which
# is why it is cut out of the ALPHA on that path and painted black on the two
# that composite with `lighter`.


def test_the_boxes_are_drawn_with_a_gap_between_them(cache):
    """One transparent strip per box, on its leading edge.

    Leading rather than trailing because every tile is binned from its own
    left edge: a trailing strip would put a gap at the end of one tile and
    another at the start of the next, which is a double-width seam down every
    tile boundary in a picture whose whole point is a regular grid.
    """
    from plexora.server.utils import colormaps

    tile = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None,
                                ramp=colormaps.ramp("viridis"), ceiling=2.0,
                                tile_size=128, bin_pixels=32)
    alpha = tile[..., 3]

    # 4 bins of 32 pixels, each opened by a 2-pixel hole. Read across the
    # middle of a row of boxes -- row 0 is itself a gap, all the way along.
    gaps = np.flatnonzero(alpha[8] == 0)
    assert list(gaps) == [0, 1, 32, 33, 64, 65, 96, 97]
    # The same cut on both axes, because the grid is square.
    assert np.array_equal(np.flatnonzero(alpha[:, 8] == 0), gaps)
    # And nothing else is transparent: a box is opaque, edge to edge.
    assert alpha[2:32, 2:32].min() == 255


def test_a_gap_thinner_than_a_pixel_is_drawn_faintly(cache):
    """THE GRID IS THERE AT EVERY ZOOM, and this is what it costs to keep it
    there. At the whole-slide levels a bin is two or three pixels across, so
    the gap it is owed is a fraction of one -- and a whole transparent pixel
    would take a third of the map rather than a twentieth, which reads as a
    screen door dimming everything rather than as a grid. The line is drawn
    at partial strength instead, so the gap stays worth the same fraction of
    a bin however far out the view is."""
    from plexora.server.utils import colormaps

    tile = tt.density_ramp_tile("demo", "tx", 0, 0, 0, indices=None,
                                ramp=colormaps.ramp("viridis"), ceiling=2.0,
                                tile_size=128, bin_pixels=4)
    alpha = tile[..., 3]

    # 32 bins of 4 pixels: the gap is worth a quarter of a pixel, so the line
    # is one pixel drawn part-way -- not a quarter of the picture cut out of
    # it. It is held at `DENSITY_GUTTER_MIN_COVERAGE` rather than drawn
    # strictly proportional, because a gap nobody can see is not a gap.
    assert list(np.flatnonzero(alpha[2] < 255)) == list(range(0, 128, 4))
    assert alpha[2, 0] == round(255 * (1 - tt.DENSITY_GUTTER_MIN_COVERAGE))
    # Still a gap and not a hole, which is the whole point of it.
    assert 0 < alpha[2, 0] < 255
    # The boxes themselves are untouched.
    assert alpha[1:4, 1:4].min() == 255


def test_the_per_gene_composite_gets_the_same_grid(cache):
    """Black, not transparent, and that is not an inconsistency: this tile is
    composited with `lighter`, under which black does not draw. Zero already
    means "nothing here" on this path -- an empty bin is black too -- so the
    gap is the same nothing, and no alpha channel is needed to say it."""
    tile = tt.density_rgb_tile("demo", "tx", 0, 0, 0,
                               groups=[([0], (255, 0, 0), 4)],
                               tile_size=128, bin_pixels=32)

    assert tile.shape == (128, 128, 3)
    for edge in range(0, 128, 32):
        assert tile[:, edge].max() == 0, edge
        assert tile[edge, :].max() == 0, edge
    # The boxes themselves still hold the picture -- the gap took a pixel off
    # each one, not the tile.
    assert tile[..., 0].max() == 255


def test_the_counts_raster_gets_it_too(cache):
    """The uint16 path the channel encoder takes is the same grid of boxes,
    drawn the same way, and a gap it did not have would be the one density
    mode whose picture disagreed with the other two."""
    tile = tt.density_tile("demo", "tx", 0, 0, 0, tile_size=128, bin_pixels=32)

    assert tile.dtype == np.uint16
    assert tile[:, 32].max() == 0
    assert tile[33:64, 33:64].max() > 0


# -- aggregated points ------------------------------------------------------
#
# The level of detail Points mode uses instead of handing the view over to a
# density raster. Two properties carry the whole feature and both are asserted
# against the arrays the cache was built from rather than against each other:
# NOTHING IS LOST (every molecule is inside exactly one aggregate) and THE DOT
# SITS WHERE THE MOLECULES ARE (the position is their centroid, so a bin
# holding one molecule sits exactly on it and a level change moves nothing).

def test_the_level_ladder_ends_at_one_tile():
    assert tt.aggregate_levels(512, 512, 128) == 3
    assert tt.aggregate_levels(128, 128, 128) == 1
    # Not a power of two, and the rounding has to go up or the last strip of
    # the slide has no tile to be drawn in.
    assert tt.aggregate_levels(45450, 27241, 512) == 8


def test_every_molecule_lands_in_exactly_one_aggregate(cache):
    # Level 2 is one tile over the whole 512-pixel grid, so this is the
    # strongest form of the claim: the counts over the entire slide.
    records = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128)

    assert records["count"].sum() == len(cache["x"])
    assert set(records["gene"].tolist()) == set(range(len(GENES)))


def test_a_gene_selection_aggregates_only_those_genes(cache):
    wanted = [1, 3]
    records = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128,
                                genes=wanted)

    assert set(records["gene"].tolist()) == set(wanted)
    expected = sum(int((cache["genes"] == g).sum()) for g in wanted)
    assert records["count"].sum() == expected


def test_every_aggregate_sits_on_a_molecule_it_stands_for(cache):
    bins = tt.AGGREGATE_BINS
    level, tile_size = 2, 128
    span = tile_size * (2 ** level)
    records = tt.aggregate_tile("demo", "tx", level, 0, 0, tile_size=tile_size)

    # NOT the mean of the bin. With two hundred molecules in a bin the mean
    # converges on the bin's CENTRE, so an abundant gene at whole-slide zoom
    # came out as a perfect lattice of evenly spaced dots -- an artifact of
    # the binning, drawn as though it were the data.
    known = set(zip(cache["x"].tolist(), cache["y"].tolist()))
    for record in records:
        assert (float(record["x"]), float(record["y"])) in known

    # ...and it is a molecule from ITS OWN bin, not just any molecule.
    scale = bins / span
    ix = np.clip((cache["x"] * scale).astype(int), 0, bins - 1)
    iy = np.clip((cache["y"] * scale).astype(int), 0, bins - 1)
    key = (cache["genes"].astype(np.int64) * bins + iy) * bins + ix
    for record in records:
        own = (int(record["gene"]) * bins
               + min(int(record["y"] * scale), bins - 1)) * bins \
            + min(int(record["x"] * scale), bins - 1)
        assert record["count"] == (key == own).sum()


def test_the_representative_does_not_move_when_another_gene_is_added(cache):
    """Switching a second gene on must not shift the first gene's dots.

    Which is why the pick is a hash of the molecule's own position rather
    than anything derived from the order the points were read in: a buffer
    order changes with the selection, and every crowded bin would jump.
    """
    alone = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128, genes=[1])
    beside = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128,
                               genes=[1, 2, 3])
    mine = beside[beside["gene"] == 1]

    assert len(alone) == len(mine)
    assert sorted(zip(alone["x"].tolist(), alone["y"].tolist())) \
        == sorted(zip(mine["x"].tolist(), mine["y"].tolist()))


def test_a_crowded_bin_is_not_represented_by_its_own_corner(cache):
    """The pick has to be spatially arbitrary, not systematically an edge.

    Writing the points in the order they are stored picks the bin's
    right-hand edge every time, because a level-0 tile is x-sorted and a bin
    sits inside one -- measured on a real run as a mean offset of 0.92
    across the bin, which is a lattice again, just shifted.
    """
    bins, tile_size, level = tt.AGGREGATE_BINS, 128, 2
    width = tile_size * (2 ** level) / bins
    records = tt.aggregate_tile("demo", "tx", level, 0, 0, tile_size=tile_size)
    crowded = records[records["count"] >= 3]
    assert len(crowded) > 30, "the fixture has to crowd some bins to test this"

    across = (crowded["x"] % width) / width
    # A uniform pick over the bin averages 0.5; an edge pick averages ~0 or
    # ~1. Loose bounds, because this is a hash and not a shuffle.
    assert 0.3 < across.mean() < 0.7, across.mean()
    assert across.std() > 0.15, across.std()


def test_a_crowded_bin_is_not_represented_by_its_own_middle():
    """...and not its middle either, which the test above cannot see.

    A bin with three molecules in it spreads the pick wide however the hash
    behaves, so the check above passed against a `_priority` whose winner was
    almost always central. Real occupancy at whole-slide zoom is forty
    molecules to a bin, not three, and there the bias is the whole picture:
    an abundant gene came out as a vertical comb of dots exactly one bin
    apart, the binning drawn as though it were the data.

    The cause is that the winner of a bin is the MAXIMUM of the hash, so
    what places the dot is the hash's high bits, and the old one's high bits
    were a monotone ramp in x -- `qx * 73_856_093` never reaches 2**63 on
    real coordinates, so it never wraps. Taking the maximum of a ramp picks
    the same place in every bin.

    Asserted on `_priority` itself rather than through a tile, because it is
    a property of the hash and it needs realistic crowding to show at all.
    """
    rng = np.random.default_rng(0)
    width, molecules = 182.0, 40
    x = rng.uniform(0, width, (4000, molecules)) + 10_000
    y = rng.uniform(0, width, (4000, molecules)) + 20_000

    winner = np.argmax(tt._priority(x, y), axis=1)
    rows = np.arange(len(x))
    for axis, picked in (("x", x[rows, winner] - 10_000),
                         ("y", y[rows, winner] - 20_000)):
        share = np.histogram(picked / width, bins=10, range=(0, 1))[0] \
            / len(picked)
        # Every tenth of the bin wins about a tenth of the time. The old hash
        # gave the middle two tenths 75% of the winners and the outer four
        # tenths none at all.
        assert share.max() < 0.16, (axis, share.round(3))
        assert share.min() > 0.05, (axis, share.round(3))


def test_a_bin_holding_one_molecule_sits_exactly_on_it(cache):
    # WHERE THE TISSUE IS SPARSE THE AGGREGATE IS THE MOLECULE, so zooming
    # through a level boundary moves nothing on screen -- which is what
    # makes the transition read as a resolution change rather than as a
    # different picture. True of most of a panel: the median gene on the
    # run this was measured against puts three molecules in a whole-slide
    # bin, and the sparse ones one.
    records = tt.aggregate_tile("demo", "tx", 0, 0, 0, tile_size=128)
    alone = records[records["count"] == 1]
    assert len(alone), "a 2-pixel bin at level 0 has to leave some singletons"

    known = set(zip(cache["x"].tolist(), cache["y"].tolist()))
    for record in alone[:50]:
        assert (float(record["x"]), float(record["y"])) in known


def test_the_quality_threshold_is_applied_before_the_merge(cache):
    records = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128, min_q=30)

    # A count is a count of what survived the filter. Applying it afterwards
    # is not possible at all -- the scores are gone by then -- and applying
    # it not at all would draw a dot for twenty molecules when the threshold
    # admits three.
    assert records["count"].sum() == int((cache["q"] >= 30).sum())
    assert records["count"].sum() < len(cache["x"])


def test_coarser_levels_merge_more_and_conserve_the_total(cache):
    counts = {}
    for level in range(3):
        step = 2 ** level
        total, rows = 0, 0
        for ty in range(4 // step):
            for tx in range(4 // step):
                part = tt.aggregate_tile("demo", "tx", level, tx, ty,
                                         tile_size=128)
                total += int(part["count"].sum())
                rows += len(part)
        assert total == len(cache["x"]), f"level {level} lost molecules"
        counts[level] = rows

    assert counts[0] > counts[1] > counts[2], counts


def test_the_most_populous_aggregate_is_drawn_first(cache):
    records = tt.aggregate_tile("demo", "tx", 2, 0, 0, tile_size=128)

    # Descending, so the small dots land ON TOP of the large ones. A rare
    # gene sharing a bin with an abundant one is otherwise painted over.
    assert (np.diff(records["count"].astype(np.int64)) <= 0).all()


def test_an_empty_region_aggregates_to_nothing(cache):
    assert len(tt.aggregate_tile("demo", "tx", 0, 3, 3, tile_size=128,
                                 genes=[])) == 0
    # Off the end of the grid: every source tile is missing, which is the
    # ordinary case for most of a slide.
    assert len(tt.aggregate_tile("demo", "tx", 0, 99, 99, tile_size=128)) == 0


def test_aggregates_are_not_produced_for_a_layer_with_no_cache():
    assert len(tt.aggregate_tile("demo", "nothing-here", 1, 0, 0)) == 0
