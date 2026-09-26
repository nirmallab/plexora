"""Label arithmetic shared by the tile route and rendered evidence."""

import numpy as np
import zarr

from plexora.server.utils import label_overlay


def _labels():
    labels = np.zeros((8, 8), dtype=np.uint32)
    labels[1:5, 1:5] = 1
    labels[5:8, 5:8] = 2
    return labels


def _js_is_boundary(ids, y, x):
    """labelTile.js isBoundary, transcribed literally, for comparison."""
    height, width = ids.shape
    cell = ids[y, x]
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if not dy and not dx:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < height and 0 <= nx < width and ids[ny, nx] != cell:
                return True
    return False


def test_boundary_matches_the_browsers_rule():
    labels = _labels()
    edge = label_overlay.boundary_mask(labels)
    for y in range(8):
        for x in range(8):
            expected = labels[y, x] != 0 and _js_is_boundary(labels, y, x)
            assert edge[y, x] == expected, (y, x)
    # A cell cut by the array edge is not outlined along the cut.
    assert not edge[7, 7]


def test_paint_outlines_only_the_chosen_cells():
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    labels = _labels()
    label_overlay.paint_labels(rgb, labels, mode="outlines",
                               colour_for=lambda label: "#ff0000" if label == 1 else None)
    assert (rgb[1, 1] == [255, 0, 0]).all()
    assert (rgb[2, 2] == 0).all()           # interior untouched
    assert (rgb[5, 5] == 0).all()           # cell 2 not chosen


def test_filled_mode_tints_the_interior():
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    label_overlay.paint_labels(rgb, _labels(), mode="filled", default=(0, 255, 0),
                               fill_alpha=0.5)
    assert rgb[2, 2, 1] == 127 or rgb[2, 2, 1] == 128
    assert rgb[1, 1, 1] == 255


def test_padded_region_is_zero_off_the_edge():
    pyramid = zarr.array(_labels())
    out = label_overlay.padded_label_region(pyramid, 0, (-2, -2, 4, 4))
    assert out.shape == (6, 6)
    assert out[:2].max() == 0
    assert out[3, 3] == 1


def test_a_missing_level_is_derived_by_stride():
    pyramid = zarr.array(np.arange(64, dtype=np.uint32).reshape(8, 8))
    region = label_overlay.label_region(pyramid, 1, 0, 0, 4, 4)
    assert region.shape == (4, 4)
    assert region[1, 1] == pyramid[2, 2]


def test_resize_nearest_never_invents_a_label():
    labels = _labels()
    big = label_overlay.resize_labels_nearest(labels, 32, 32)
    assert set(np.unique(big)) <= set(np.unique(labels))
    assert big.shape == (32, 32)


def test_cell_ids_uses_the_role_column_when_present():
    import polars as pl

    frame = pl.DataFrame({"id": [0, 1, 2], "CellID": [5, None, 7]})
    ids, keep = label_overlay.cell_ids(frame, "CellID")
    assert ids.tolist() == [5, 7] and keep.tolist() == [True, False, True]
    ids, _ = label_overlay.cell_ids(frame, "absent")
    assert ids.tolist() == [0, 1, 2]
