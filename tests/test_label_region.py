"""A label mask's tiles at every level, including levels the mask does not have.

A single-level mask used to be read at full resolution for every level, so a
zoomed-out tile showed a patch 2**level times too small, stretched over the
whole tile: another part of the slide's cells drawn over the tissue. A mask
with fewer levels than the image raised KeyError instead.
"""

from __future__ import annotations

import numpy as np
import zarr

from plexora.server.models import data_model


def _labels(size=2048):
    return np.arange(size * size, dtype=np.uint32).reshape(size, size)


def _ids(tile):
    """Undo read_tile's RGBA packing."""
    tile = np.ascontiguousarray(tile)
    return tile.view(np.uint32)[..., 0]


def test_a_single_level_mask_is_sampled_at_the_requested_level():
    labels = _labels()
    mask = zarr.array(labels, chunks=(512, 512))
    tile = data_model.read_tile(mask, None, 2, "1_0", 256, 256)
    assert (_ids(tile) == labels[::4, ::4][0:256, 256:512]).all()


def test_level_zero_is_read_as_it_always_was():
    labels = _labels(512)
    mask = zarr.array(labels, chunks=(256, 256))
    tile = data_model.read_tile(mask, None, 0, "1_1", 256, 256)
    assert (_ids(tile) == labels[256:512, 256:512]).all()


def test_a_pyramid_short_of_levels_derives_the_rest_from_its_coarsest():
    labels = _labels()
    group = zarr.group()
    group.create_array("0", data=labels, chunks=(512, 512))
    group.create_array("1", data=labels[::2, ::2], chunks=(512, 512))
    tile = data_model.read_tile(group, None, 3, "0_0", 256, 256)
    assert (_ids(tile) == labels[::8, ::8][:256, :256]).all()
    # A level it has is read from that level.
    tile = data_model.read_tile(group, None, 1, "1_0", 512, 512)
    assert (_ids(tile) == labels[::2, ::2][0:512, 512:1024]).all()


def test_an_edge_tile_is_cut_at_the_mask_edge():
    labels = _labels(1000)
    mask = zarr.array(labels, chunks=(256, 256))
    tile = data_model.read_tile(mask, None, 1, "1_1", 256, 256)
    expected = labels[::2, ::2][256:512, 256:512]
    assert tile.shape[:2] == expected.shape
    assert (_ids(tile) == expected).all()
