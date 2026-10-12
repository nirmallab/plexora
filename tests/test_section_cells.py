"""A 3-D mask's cells where an XZ or YZ section cuts them
(server/volume/section_cells.py, `/volume/<ds>/section_cells`).

A section pane is a plane through every z, and it shows the cells as they cross
it: what has to hold is that the runs along its line are exactly the labels on
that line, plane by plane -- each run one cell's, its ends where the cell's
voxels end -- at full resolution and at a coarser level, local or on a data node.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.volume import section_cells as cuts
from tests import volume_fixtures as vf
from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.test_node_label_volume import (_on_a_node, _stack_and_mask, _status,
                                          _wait_for_local_mask)


class Levels:
    """A label volume's face: strided XY levels, as labels.build_label_volume writes."""

    def __init__(self, array, levels=3):
        self.arrays = [array[:, ::2 ** i, ::2 ** i] for i in range(levels)]
        self.depth, self.height, self.width = array.shape
        self.levels = levels

    def read(self, level, z0, z1, rows, cols):
        return self.arrays[level][z0:z1 + 1, rows, cols]


def _labels():
    labels = np.zeros((8, 64, 80), np.uint32)
    labels[1:6, 10:30, 10:30] = 3
    labels[2:7, 10:30, 30:44] = 4          # touching 3 along x
    labels[0:3, 40:60, 50:70] = 9
    labels[5:8, 40:60, 50:70] = 9          # one cell, two runs of planes
    return labels


def brute(labels, axis, at):
    line = labels[:, at, :] if axis == "xz" else labels[:, :, at]
    out = []
    for z, row in enumerate(line):
        a = 0
        while a < row.size:
            b = a
            while b + 1 < row.size and row[b + 1] == row[a]:
                b += 1
            if row[a]:
                out.append((z, int(row[a]), a, b))
            a = b + 1
    return out


@pytest.mark.parametrize("axis, at", [("xz", 20), ("xz", 50), ("yz", 15), ("yz", 60), ("xz", 0)])
def test_the_runs_are_exactly_the_cells_on_the_line(axis, at):
    labels = _labels()
    z, label, first, last, level = cuts.runs(Levels(labels), axis, at)
    assert level == 0
    assert list(zip(z.tolist(), label.tolist(), first.tolist(), last.tolist())) == brute(labels, axis, at)


def test_a_coarser_level_spans_the_same_cells_in_full_resolution_pixels():
    labels = _labels()
    z, label, first, last, level = cuts.runs(Levels(labels), "xz", 20, level=1)
    assert level == 1
    fine = brute(labels, "xz", 20)
    assert {(a, b) for a, b in zip(z.tolist(), label.tolist())} == {(r[0], r[1]) for r in fine}
    assert first.min() >= 0 and last.max() <= labels.shape[2] - 1


def test_a_deep_line_is_read_coarser_than_asked(monkeypatch):
    monkeypatch.setattr(cuts, "MAX_VOXELS", 8 * 30)
    *_, level = cuts.runs(Levels(_labels()), "xz", 20, level=0)
    assert level == 2


def test_the_route_answers_and_a_flat_mask_is_refused(tmp_path):
    client = plexora.app.test_client()
    path, _ = vf.write_ome_volume(tmp_path / "sc.ome.tif", axes="CZYX", channels=2, depth=8,
                                  height=64, width=80, names=["DNA", "CD8"], physical_z=1.0)
    labels = _labels().astype(np.uint16)
    tifffile.imwrite(tmp_path / "sc_mask.tif", labels)
    register_image_datasource("sc", path)
    datasets.add_layers("sc", tmp_path / "sc_mask.tif", wait=True)
    response = client.get("/volume/sc/section_cells?axis=yz&at=60&t=0")
    assert response.status_code == 200, response.get_json()
    header = json.loads(response.headers["X-Section-Cells"])
    n = header["n"]
    data = response.data
    z = np.frombuffer(data[:2 * n], "<u2")
    label = np.frombuffer(data[2 * n:6 * n], "<u4")
    first = np.frombuffer(data[6 * n:10 * n], "<u4")
    last = np.frombuffer(data[10 * n:14 * n], "<u4")
    assert list(zip(z.tolist(), label.tolist(), first.tolist(), last.tolist())) \
        == brute(labels, "yz", 60)
    assert client.get("/volume/sc/section_cells?axis=xy&at=1").status_code == 400

    flat = np.zeros((64, 80), np.uint16)
    flat[5:9, 5:9] = 2
    tifffile.imwrite(tmp_path / "flat_mask.tif", flat)
    register_image_datasource("sc2d", path)
    datasets.add_layers("sc2d", tmp_path / "flat_mask.tif", wait=True)
    refused = client.get("/volume/sc2d/section_cells?axis=xz&at=6")
    assert refused.status_code == 400 and refused.get_json()["reason"] == "no_label_volume"


def test_a_node_held_mask_is_cut_the_same(tmp_path, node_process):
    from plexora import nodes
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import attach_segmentation
    from plexora.server.volume import labels as volume_labels

    image, mask = _stack_and_mask(tmp_path)
    register_image_datasource("here", image)
    attach_segmentation("here", str(mask))
    _wait_for_local_mask("here")
    node = node_process(f"image:img={image}", f"segmentation:cells={mask}")
    register("o2", node)
    _status(node, "cells")
    _on_a_node("there", image, "o2")
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    local = volume_labels.project_label_volume(Project.load("here"))
    remote = volume_labels.project_label_volume(Project.load("there"))
    for axis, at in (("xz", 120), ("yz", 100)):
        for level in (0, 1):
            ours = cuts.runs(local, axis, at, level=level)
            theirs = cuts.runs(remote, axis, at, level=level)
            assert all(np.array_equal(a, b) for a, b in zip(ours[:4], theirs[:4]))
            assert ours[4] == theirs[4] and ours[1].size > 0
