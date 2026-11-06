"""plexora's `block_reduce` gives scikit-image's answers (server/utils/blocks.py)."""

import numpy as np
import pytest

skimage_measure = pytest.importorskip("skimage.measure")
from plexora.server.utils.blocks import block_reduce  # noqa: E402


@pytest.mark.parametrize("shape, block", [
    ((4, 512, 512), (1, 8, 8)),
    ((3, 757, 1031), (1, 16, 16)),   # edges that are not whole blocks
    ((101, 99), (4, 4)),
    ((1, 3, 5), (1, 8, 8)),          # smaller than one block
    ((64, 64), 4),
])
@pytest.mark.parametrize("func", [np.mean, np.max, np.sum])
def test_it_matches_scikit_image(shape, block, func):
    image = np.random.default_rng(0).integers(0, 65535, shape, dtype=np.uint16)
    ours = block_reduce(image, block, func)
    theirs = skimage_measure.block_reduce(image, block, func)
    assert ours.dtype == theirs.dtype and ours.shape == theirs.shape
    assert np.array_equal(ours, theirs)


def test_the_padding_value_is_cval():
    image = np.ones((3, 3))
    assert block_reduce(image, 2, np.sum, cval=5)[1, 1] == skimage_measure.block_reduce(
        image, 2, np.sum, cval=5)[1, 1] == 1 + 3 * 5
