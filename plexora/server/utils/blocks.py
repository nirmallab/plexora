"""`block_reduce`, without scikit-image.

Opening an image computes its overview with `skimage.measure.block_reduce`,
and on the website importing scikit-image means fetching the 20 MB stage it
ships in (plexora/browser/stages.py) before the first project can open. This is
the same function in numpy: pad the far edges to a whole number of blocks with
`cval`, then reduce each block with `func`. Held to scikit-image's answers by
tests/test_blocks.py.
"""

from __future__ import annotations

import numpy as np


def block_reduce(image, block_size=2, func=np.sum, cval=0, func_kwargs=None):
    """`skimage.measure.block_reduce(image, block_size, func, cval, func_kwargs)`."""
    image = np.asarray(image)
    if np.isscalar(block_size):
        block_size = (int(block_size),) * image.ndim
    block_size = tuple(int(b) for b in block_size)
    if len(block_size) != image.ndim:
        raise ValueError("`block_size` must be a scalar or have the same length as `image.shape`")
    if any(b < 1 for b in block_size):
        raise ValueError("Down-sampling factors must be >= 1")
    pad = [(0, (-side) % block) for side, block in zip(image.shape, block_size)]
    if any(after for _before, after in pad):
        image = np.pad(image, pad, mode="constant", constant_values=cval)
    shape = []
    for side, block in zip(image.shape, block_size):
        shape += [side // block, block]
    blocks = image.reshape(shape)
    axes = tuple(range(1, 2 * image.ndim, 2))
    return func(blocks, axis=axes, **(func_kwargs or {}))
