"""A mask served from memory, read at a level its own pyramid does not have.

`memory_pyramid.label_pyramid` stops halving once a level fits one 1024-pixel
tile, so a mask drawn over a disk image whose own pyramid goes deeper (an
OME-TIFF with levels down to ~300 px, say) has fewer levels than the image the
viewer zooms. `label_overlay.label_region` exists for exactly that case: it
subsamples the finest level below the one asked for, and the zoomed-out view --
the first one a viewer opens on -- shows the cells in the right place.

That has to hold for an in-memory mask (an `ome_zarr.NgffPyramid`) just as it
holds for a zarr group read off disk.
"""

import numpy as np

from plexora.server.models import data_model
from plexora.server.utils import label_overlay, memory_pyramid


def _mask(height=2100, width=2100):
    """Ids that change every 7 pixels, so a wrong subsampling factor shows."""
    rows = (np.arange(height, dtype="uint32") // 7)[:, None]
    columns = (np.arange(width, dtype="uint32") // 7)[None, :]
    return rows * 1000 + columns + 1


def _tile_ids(tile):
    """The uint32 ids `read_tile` packed as little-endian RGBA bytes."""
    return np.ascontiguousarray(tile).view("<u4")[..., 0]


def test_a_memory_mask_tile_past_its_last_level_is_subsampled_from_the_coarsest():
    mask = _mask()
    pyramid = memory_pyramid.label_pyramid(mask)
    missing = len(pyramid)  # one level coarser than the mask holds
    assert str(missing) not in pyramid

    tile = data_model.read_tile(pyramid, None, missing, "0_0", 1024, 1024)

    expected = mask[::2 ** missing, ::2 ** missing]
    assert np.array_equal(_tile_ids(tile), expected)


def test_a_memory_mask_region_past_its_last_level_is_padded_not_an_error():
    mask = _mask()
    pyramid = memory_pyramid.label_pyramid(mask)
    level = len(pyramid) + 1
    factor = 2 ** level
    full = -(-mask.shape[0] // factor)

    labels = label_overlay.padded_label_region(pyramid, level, (0, 0, 200, 200))

    assert labels.shape == (200, 200)
    assert np.array_equal(labels[:full, :full], mask[::factor, ::factor])
    assert not labels[full:, :].any() and not labels[:, full:].any()
