"""Per-cell pixel features (utils/cell_features.py): known answers on a
synthetic mask, the same answer whatever the tiling, and the node wire form."""

from __future__ import annotations

import numpy as np
import pytest

from plexora.server.utils import cell_features as cf


def _scene():
    labels = np.zeros((64, 64), dtype=np.uint32)
    marker = np.full((64, 64), 5.0, dtype=np.float32)
    dna = np.zeros((64, 64), dtype=np.float32)
    labels[10:20, 10:20] = 1          # uniform cell, 50
    marker[10:20, 10:20] = 50.0
    labels[30:44, 30:44] = 7          # membrane cell: bright ring, dim middle
    marker[30:44, 30:44] = 100.0
    marker[31:43, 31:43] = 10.0
    dna[34:40, 34:40] = 1.0           # nucleus in the dim middle
    labels[50:60, 2:8] = 3            # a cell on no marker signal
    marker[50:60, 2:8] = 5.0
    return labels, marker, dna


def _reader(plane):
    height, width = plane.shape

    def read(box):
        clipped = cf.clipped_box(box, width, height)
        x0, y0, x1, y1 = clipped
        return cf.pad_to_box(plane[y0:y1, x0:x1], clipped, box)
    return read


def _labels(plane):
    read = _reader(plane.astype(np.float32))
    return lambda box: read(box).astype(np.uint32)


def _run(tile, with_dna=True):
    labels, marker, dna = _scene()
    return cf.accumulate(_reader(marker), _labels(labels), width=64, height=64,
                         read_dna=_reader(dna) if with_dna else None, tile=tile)


def test_known_values_per_cell():
    ids, f = _run(1024)
    assert ids.tolist() == [1, 3, 7]
    one, three, seven = 0, 1, 2
    assert f["px_mean"][one] == pytest.approx(50.0)
    assert f["px_area"][one] == 100
    assert f["px_std"][one] == pytest.approx(0.0, abs=1e-3)
    assert f["px_max"][seven] == pytest.approx(100.0)
    # The ring is the membrane: bright edge over a dim interior.
    assert f["px_membrane_ratio"][seven] > 5
    assert f["px_membrane_ratio"][one] == pytest.approx(1.0, rel=1e-3)
    # Background is the tissue between cells.
    assert f["px_background"][one] == pytest.approx(5.0)
    assert f["px_contrast"][one] == pytest.approx(45.0)
    assert f["px_contrast"][three] == pytest.approx(0.0, abs=1e-3)
    # The nucleus sits in the dim middle of the membrane cell.
    assert f["px_nuclear_ratio"][seven] < 0.5


def test_the_tiling_does_not_change_the_answer():
    ids_a, a = _run(1024)
    ids_b, b = _run(16)
    assert ids_a.tolist() == ids_b.tolist()
    for name in a:
        np.testing.assert_allclose(a[name], b[name], rtol=1e-5, atol=1e-5, err_msg=name)


def test_no_nuclear_channel_drops_only_that_feature():
    _ids, f = _run(32, with_dna=False)
    assert "px_nuclear_ratio" not in f
    assert set(f) == set(cf.NAMES) - {"px_nuclear_ratio"}


def test_progress_and_cancel():
    labels, marker, _dna = _scene()
    seen = []
    cf.accumulate(_reader(marker), _labels(labels), width=64, height=64, tile=32,
                  progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (4, 4)
    with pytest.raises(InterruptedError):
        cf.accumulate(_reader(marker), _labels(labels), width=64, height=64,
                      cancelled=lambda: True)


def test_an_empty_mask_gives_no_cells():
    ids, f = cf.accumulate(lambda box: np.zeros((box[3] - box[1], box[2] - box[0])),
                           lambda box: np.zeros((box[3] - box[1], box[2] - box[0]), np.uint32),
                           width=40, height=40)
    assert ids.size == 0 and all(v.size == 0 for v in f.values())


def test_pack_round_trip_and_level_choice():
    ids, f = _run(1024)
    back_ids, back = cf.unpack(cf.pack(ids, f))
    assert back_ids.tolist() == ids.tolist()
    assert set(back) == set(f)
    assert cf.choose_level(1000, 1000, 4, max_pixels=10**7) == 0
    assert cf.choose_level(40000, 40000, 6, max_pixels=4 * 10**8) == 1
    assert cf.choose_level(40000, 40000, 1, max_pixels=10) == 0


def test_pad_to_box_places_a_clipped_read():
    out = cf.pad_to_box(np.ones((2, 3)), (0, 0, 3, 2), (-1, -1, 4, 3))
    assert out.shape == (4, 5)
    assert out[1:3, 1:4].sum() == 6 and out.sum() == 6
