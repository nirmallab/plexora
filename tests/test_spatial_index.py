"""The cell index behind the right-click pick and QC's hover
(plexora/server/utils/spatial_index.py)."""

import pickle

import numpy as np
import polars as pl
import pytest

from plexora.server.utils.spatial_index import SpatialIndex


def _brute(xs, ys, x, y):
    d = np.hypot(xs - x, ys - y)
    return int(np.argmin(d)), float(d.min())


def test_a_2d_pick_is_the_nearest_cell_anywhere():
    rng = np.random.default_rng(7)
    xs, ys = rng.uniform(0, 5000, 2000), rng.uniform(0, 3000, 2000)
    index = SpatialIndex.build(pl.DataFrame({"X": xs, "Y": ys}), x="X", y="Y")
    assert len(index) == 2000
    for qx, qy in rng.uniform(-200, 5200, (200, 2)):
        row, distance = index.nearest(qx, qy)
        expected_row, expected_distance = _brute(xs, ys, qx, qy)
        assert row == expected_row
        assert distance == pytest.approx(expected_distance)


def test_a_2d_pick_matches_the_ball_tree_it_replaces():
    sklearn = pytest.importorskip("sklearn.neighbors")
    rng = np.random.default_rng(11)
    points = rng.uniform(0, 1000, (500, 2))
    old = sklearn.BallTree(points, metric="euclidean")
    index = SpatialIndex.build(pl.DataFrame({"x": points[:, 0], "y": points[:, 1]}),
                               x="x", y="y")
    for q in rng.uniform(0, 1000, (100, 2)):
        _, at = old.query([q], k=1)
        assert index.nearest(*q)[0] == int(at[0][0])


def test_rows_without_a_position_are_left_out_and_rows_keep_their_numbers():
    frame = pl.DataFrame({"x": [0.0, None, 10.0, float("nan")], "y": [0.0, 5.0, 0.0, 1.0]})
    index = SpatialIndex.build(frame, x="x", y="y")
    assert len(index) == 2
    assert index.nearest(9.0, 0.0)[0] == 2
    assert SpatialIndex.build(pl.DataFrame({"x": [None], "y": [1.0]}), x="x", y="y") is None


def _stack():
    """Two cells stacked in z at one spot, a tall one, and a one-plane one."""
    return pl.DataFrame({
        "id": [5, 9, 7, 8],
        "X": [15.0, 15.0, 35.0, 49.0], "Y": [15.0, 15.0, 35.0, 35.0],
        "Z": [1.0, 4.0, 2.5, 5.0],
        "bbox_x0": [10, 10, 30, 44], "bbox_x1": [19, 19, 39, 53],
        "bbox_y0": [10, 10, 30, 30], "bbox_y1": [19, 19, 39, 39],
        "bbox_z0": [0, 3, 0, 5], "bbox_z1": [2, 5, 5, 5],
        "n_planes": [3, 3, 6, 1]})


def test_planes_pick_the_cell_on_them():
    index = SpatialIndex.build(_stack(), x="X", y="Y", z="Z")
    assert index.extent_from == "bbox"
    assert index.nearest(15, 15, planes=(1, 1))[0] == 0
    assert index.nearest(15, 15, planes=(4, 4))[0] == 1
    assert index.nearest(15, 15, planes=(0, 1))[0] == 0
    assert index.nearest(15, 15, planes=(4, 5))[0] == 1
    # The tall cell is under the cursor on its last plane, though the
    # one-plane cell's centroid is nearer in 3-D.
    assert index.nearest(41, 35, planes=(5, 5))[0] == 2
    # Glass: nothing within a few cell diameters.
    assert index.nearest(400, 400, planes=(1, 1)) is None
    # No planes: the nearest anywhere, as in 2-D.
    assert index.nearest(400, 400)[0] in (2, 3)


def test_without_an_extent_the_centroid_plane_stands_in():
    frame = _stack().drop(["bbox_z0", "bbox_z1"])
    index = SpatialIndex.build(frame, x="X", y="Y", z="Z")
    assert index.extent_from == "centroid"
    # median n_planes 2 -> one plane either side of the rounded centroid
    assert index.nearest(15, 15, planes=(0, 0))[0] == 0
    assert index.nearest(15, 15, planes=(5, 5))[0] == 1


def test_planes_are_ignored_without_a_z_role_and_a_frame_keeps_to_its_frame():
    frame = pl.DataFrame({"x": [0.0, 0.5, 9.0], "y": [0.0, 0.0, 0.0], "t": [0, 1, 1]})
    index = SpatialIndex.build(frame, x="x", y="y", t="t")
    assert index.nearest(0, 0, planes=(3, 3))[0] == 0
    assert index.nearest(0, 0, t=1)[0] == 1
    assert index.nearest(0, 0, t=4) is None


def test_a_cell_with_no_z_is_on_no_plane():
    frame = _stack().with_columns(pl.Series("bbox_z0", [None, 3, 0, 5]))
    index = SpatialIndex.build(frame, x="X", y="Y", z="Z")
    # Not cell 5 (no z), not cell 9 (planes 3..5): the tall cell beside them.
    assert index.nearest(15, 15, planes=(1, 1))[0] == 2


def test_the_cached_state_round_trips_and_refuses_another_version():
    index = SpatialIndex.build(_stack(), x="X", y="Y", z="Z", t=None)
    state = pickle.loads(pickle.dumps(index.state()))
    again = SpatialIndex.from_state(state)
    assert again.nearest(15, 15, planes=(4, 4)) == index.nearest(15, 15, planes=(4, 4))
    assert SpatialIndex.from_state({**state, "version": -1}) is None
    assert SpatialIndex.from_state(object()) is None
