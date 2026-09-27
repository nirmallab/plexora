"""Which cells a gate passes, decided exactly as the viewer decides it.

The viewer's rule (`data_model.apply_range_mask`, `centroid_tiles._apply_gates`,
`labelGpu.evaluateGateMask`) is `low < value <= high` on a FLOAT32 column with
the bounds rounded to float32. The agent reads the same column and must count
the same cells, so it compares in float32 too: a column maximum stored as a
float64 bound rounds, as a float32 value, to either side of it, and a float64
comparison would then drop the brightest cell the viewer colours.
"""

from __future__ import annotations

import numpy as np


def passes(values, low, high):
    """A boolean mask: finite, above `low`, at or below `high` -- in float32."""
    v = np.asarray(values, dtype=np.float32)
    return np.isfinite(v) & (v > np.float32(low)) & (v <= np.float32(high))
