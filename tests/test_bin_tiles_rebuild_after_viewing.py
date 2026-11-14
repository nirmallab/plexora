"""A bin store that has been drawn from can still be rebuilt.

Drawing a bin tile maps the store's tissue raster (`read_tissue`) and keeps the
map in a module cache for as long as the file's mtime holds. A rebuild replaces
the whole store directory. On Windows a file another handle has mapped cannot
be deleted, so the rebuild must not trip over the map the tiles left behind --
and a failed rebuild must not leave the old store without its manifest.
"""

import numpy as np
from scipy import sparse

from plexora.server.models import bin_tiles as bt

from tests.helpers import use_data_root

GENES = ["INS", "GCG"]
ROWS, COLUMNS = 40, 48


def _blocks(ref, tissue):
    rr, cc = np.nonzero(tissue)
    csr = sparse.csr_matrix(ref[:, rr, cc].T)
    return [(rr, cc, csr.indptr, csr.indices, csr.data.astype(np.int32))]


def _build(source, ref, tissue):
    expected = bt.expected_manifest(source, columns=COLUMNS, rows=ROWS,
                                    bin_um=2.0, tile_size=16, supersample=2,
                                    store_tile=16, layer_id="bins")
    return bt.build("rebuilt", "bins", genes=GENES,
                    blocks=_blocks(ref, tissue), expected=expected), expected


def test_a_store_drawn_from_rebuilds_when_its_source_changes(tmp_path,
                                                             monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    rng = np.random.default_rng(3)
    tissue = np.zeros((ROWS, COLUMNS), dtype=bool)
    tissue[4:36, 6:44] = True
    ref = rng.poisson(0.5, size=(len(GENES), ROWS, COLUMNS)).astype(np.int64)
    ref *= tissue
    source = tmp_path / "filtered_feature_bc_matrix.h5"
    source.write_bytes(b"only its mtime is read")
    _build(source, ref, tissue)

    # The viewer draws a tile: this maps tissue_p1.npy and caches the map.
    manifest = bt.read_manifest("rebuilt", "bins")
    stats = bt.read_stats("rebuilt", "bins")
    tile = bt.ramp_tile("rebuilt", "bins", manifest, stats, 0, 1, 1,
                        genes=[manifest["total_index"]],
                        ramp=np.zeros((256, 3), np.uint8) + 200, pooling=1)
    assert tile[..., 3].max() > 0

    # The source matrix changes, so the panel's /build runs again.
    source.write_bytes(b"a newer matrix, a different size")
    rebuilt, expected = _build(source, ref * 2, tissue)

    assert bt.is_current(rebuilt, expected)
    on_disk = bt.read_manifest("rebuilt", "bins")
    assert on_disk is not None and bt.is_current(on_disk, expected)
    assert on_disk["total_count"] == int((ref * 2).sum())
