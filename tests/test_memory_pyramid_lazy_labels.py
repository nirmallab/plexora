"""A lazy label mask's coarse levels stay lazy (audit lead io-image-10).

`label_pyramid` promises strided views. For a numpy mask they are; for a
zarr / dask / xarray mask each coarse level was read in full at attach time --
a third of the mask in RAM, and every chunk of it read before the first tile.
"""

import numpy as np

from plexora.server.utils.memory_pyramid import label_pyramid


class Lazy:
    """Slices like an array; counts the elements it hands out."""

    def __init__(self, array):
        self._array = array
        self.shape = array.shape
        self.dtype = array.dtype
        self.ndim = array.ndim
        self.read = 0

    def __getitem__(self, index):
        out = self._array[index]
        self.read += np.asarray(out).size
        return out


def test_attaching_a_lazy_mask_reads_nothing_and_its_levels_read_only_what_is_asked():
    mask = np.arange(5000 * 3000, dtype=np.uint32).reshape(5000, 3000)
    lazy = Lazy(mask)
    pyramid = label_pyramid(lazy, target=512)
    assert len(pyramid) == 5
    assert lazy.read == 0
    for level in range(len(pyramid)):
        factor = 2 ** level
        expected = mask[::factor, ::factor]
        got = pyramid[str(level)] if hasattr(pyramid, "__getitem__") else None
        assert tuple(got.shape) == expected.shape
        assert np.array_equal(np.asarray(got[3:40, 7:300]), expected[3:40, 7:300])
        assert np.array_equal(np.asarray(got[-5:, :4]), expected[-5:, :4])
        assert np.array_equal(np.asarray(got[2, 1:9:3]), expected[2, 1:9:3])
    lazy.read = 0
    tile = pyramid["3"][0:64, 0:64]
    assert isinstance(tile, np.ndarray) and lazy.read == 64 * 64
    assert np.array_equal(np.asarray(pyramid["4"]), mask[::16, ::16])
