"""Which cells are on which plane (server/volume/planes.py), against brute force.

The pass that measures a 3-D mask's cells writes, for every plane, the cells
with voxels on it and how many -- what the thresholding panel counts as the
cells on the plane on screen (the outlines that plane draws). What has to hold
on a stack built to be awkward (cells touching, a concave cell with planes
inside its box it has no voxels on, cells cut by both ends, two frames): every
plane lists exactly the labels present on it, with their voxel counts; a run of
planes is one slice; a cancelled pass leaves the previous index; and an index
survives the trip from a data node.
"""

import numpy as np
import pytest

from plexora.server.volume import measure as volume_measure
from plexora.server.volume import planes as volume_planes

DEPTH, HEIGHT, WIDTH = 12, 40, 48


class ArrayLabels:
    """The face `measure.measure` reads a label volume through."""

    def __init__(self, array):
        self.array = array
        self.depth, self.height, self.width = array.shape

    def read(self, level, z0, z1, rows, cols):
        return self.array[z0:z1 + 1, rows, cols]


def awkward_labels():
    labels = np.zeros((DEPTH, HEIGHT, WIDTH), np.uint32)
    labels[2:9, 4:14, 4:14] = 3            # a column through seven planes
    labels[2:9, 4:14, 14:22] = 4           # touching 3 along x on every plane
    labels[0:3, 20:30, 4:12] = 7           # cut by the first plane
    labels[9:12, 20:30, 30:40] = 8         # cut by the last plane
    labels[1:3, 30:38, 20:28] = 11         # concave in z: planes 1-2 and 8-9,
    labels[8:10, 30:38, 20:28] = 11        # none of 3-7 inside its box
    labels[5, 32:36, 40:44] = 12           # one plane only
    return labels


def build(folder, labels, frames=1):
    writer = volume_planes.PlaneWriter(folder, depth=labels.shape[0], frames=frames)
    image = np.ones((1,) + labels.shape)
    for t in range(frames):
        volume_measure.measure(ArrayLabels(labels), lambda c, z, rows, cols: image[c, z, rows, cols],
                               channel_names=["DNA"], units="voxel", t=t, planes=writer)
    writer.close()
    return volume_planes.PlaneIndex.open(folder)


@pytest.fixture
def index(tmp_path):
    labels = awkward_labels()
    return labels, build(tmp_path / volume_planes.FOLDER, labels)


def test_every_plane_lists_exactly_its_cells_and_their_voxels(index):
    labels, plane_index = index
    for z in range(DEPTH):
        cells, voxels = plane_index.cells_on(0, z)
        present = [int(v) for v in np.unique(labels[z]) if v]
        assert cells.tolist() == present
        assert voxels.tolist() == [int((labels[z] == cell).sum()) for cell in present]


def test_a_concave_cell_is_not_on_the_planes_inside_its_box(index):
    labels, plane_index = index
    on = [z for z in range(DEPTH) if 11 in plane_index.cells_on(0, z)[0]]
    assert on == [1, 2, 8, 9]


def test_a_run_of_planes_is_one_slice(index):
    labels, plane_index = index
    assert plane_index.span(0, 0, DEPTH - 1) == slice(0, plane_index.rows)
    rows = plane_index.span(0, 3, 5)
    expected = sum(len([v for v in np.unique(labels[z]) if v]) for z in (3, 4, 5))
    assert rows.stop - rows.start == expected
    assert set(plane_index.z[rows]) == {3, 4, 5}


def test_a_time_series_is_indexed_frame_by_frame(tmp_path):
    labels = awkward_labels()
    plane_index = build(tmp_path / volume_planes.FOLDER, labels, frames=2)
    assert plane_index.frames == 2 and set(np.unique(plane_index.t)) == {0, 1}
    for z in range(DEPTH):
        assert plane_index.cells_on(1, z)[0].tolist() == plane_index.cells_on(0, z)[0].tolist()


def test_an_index_survives_the_trip_from_a_data_node(index, tmp_path):
    _, plane_index = index
    moved = volume_planes.unpack(volume_planes.pack(plane_index.folder),
                                 tmp_path / "elsewhere" / volume_planes.FOLDER)
    copy = volume_planes.PlaneIndex.open(moved)
    for column in ("label", "z", "t", "voxels"):
        assert np.array_equal(getattr(copy, column), getattr(plane_index, column))


def test_a_cancelled_pass_leaves_the_previous_index(index):
    labels, plane_index = index
    before = plane_index.rows
    writer = volume_planes.PlaneWriter(plane_index.folder, depth=DEPTH)

    def failing(c, z, rows, cols):
        if z == 4:
            raise InterruptedError("cancelled")
        return np.ones(labels[z, rows, cols].shape)

    with pytest.raises(InterruptedError):
        try:
            volume_measure.measure(ArrayLabels(labels), failing, channel_names=["DNA"],
                                   units="voxel", planes=writer)
        except BaseException:
            writer.abort()
            raise
    assert volume_planes.PlaneIndex.open(plane_index.folder).rows == before
