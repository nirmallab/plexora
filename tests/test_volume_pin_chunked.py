"""Pinning a projection of a z-chunked stack reads whole chunks (io-volume-4).

An Imaris file or a z-chunked OME-Zarr stores several planes per chunk; a read
of one plane decodes the whole chunk. Reading the slab plane by plane decoded
every chunk once per plane in it; it is read in runs of whole chunks instead,
and the projection is the same.
"""

import numpy as np

from plexora.server.volume import pin
from plexora.server.volume.sources.memory import volume_from_array
from plexora.server.volume.viewspec import ViewSpec


def _chunked(array, z_chunk):
    source = volume_from_array(array, "CZYX")
    source.prefers_block = True
    source.z_chunk = z_chunk
    calls = {"plane": 0, "block": []}
    plane, block = source.read_plane, source.read_block

    def read_plane(level, t, z, channel, rows, cols):
        calls["plane"] += 1
        return plane(level, t, z, channel, rows, cols)

    def read_block(level, t, channel, z0, z1, rows, cols):
        calls["block"].append((z0, z1))
        return block(level, t, channel, z0, z1, rows, cols)

    source.read_plane, source.read_block = read_plane, read_block
    return source, calls


def test_a_projection_of_a_z_chunked_stack_reads_each_chunk_once(tmp_path):
    rng = np.random.default_rng(0)
    array = rng.integers(0, 4000, size=(2, 10, 64, 48), dtype=np.uint16)
    source, calls = _chunked(array, z_chunk=4)
    spec = ViewSpec(kind="slab", z0=1, z1=8, op="max")

    out = tmp_path / "p.ome.tif"
    pin.write_projection(source, spec, out)

    assert calls["plane"] == 0
    # Per channel: the chunks under planes 1..8 -- [1, 3], [4, 7], [8, 8].
    assert calls["block"] == [(1, 3), (4, 7), (8, 8)] * 2
    import tifffile

    written = tifffile.imread(out, level=0)
    assert np.array_equal(np.asarray(written).reshape(2, 64, 48), array[:, 1:9].max(axis=1))
