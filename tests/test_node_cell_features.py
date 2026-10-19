"""Per-cell pixel features built on the data node that holds the image and the
mask (`/node/v1/cells/features`), equal to the same pass run locally."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from tests.agent_fixtures import blob_scene, write_image_pyramid, write_mask_pyramid
from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.test_node_label_volume import _status


def _local(image, labels, marker_index, dna_index):
    from plexora.server.utils import cell_features as cf

    def reader(plane):
        def read(box):
            clipped = cf.clipped_box(box, plane.shape[1], plane.shape[0])
            x0, y0, x1, y1 = clipped
            return cf.pad_to_box(plane[y0:y1, x0:x1], clipped, box)
        return read

    def read_labels(box):
        return reader(labels.astype(np.float64))(box).astype(np.uint32)

    return cf.accumulate(reader(image[marker_index].astype(np.float32)), read_labels,
                         width=labels.shape[1], height=labels.shape[0],
                         read_dna=reader(image[dna_index].astype(np.float32)))


def test_the_node_builds_the_features_a_local_pass_builds(tmp_path, node_process):
    from plexora.server.providers.node import cell_features_on_node

    image, labels, _cells = blob_scene(size=256, grid=4, radius=20)
    image_path = write_image_pyramid(tmp_path / "image.ome.tif", image)
    mask_path = write_mask_pyramid(tmp_path / "mask.tif", labels)
    node = node_process(f"image:img={image_path}", f"segmentation:cells={mask_path}")
    register("o2", node)
    _status(node, "cells")

    steps = []
    ids, features = cell_features_on_node(
        SimpleNamespace(node="o2", resource_id="img"),
        SimpleNamespace(node="o2", resource_id="cells"),
        marker_index=1, dna_index=0, level=0, mask_level=0,
        progress=lambda done, total: steps.append((done, total)), poll=0.05)
    local_ids, local = _local(image, labels, 1, 0)

    assert ids.tolist() == local_ids.tolist() == list(range(1, 17))
    assert set(features) == set(local)
    for name in local:
        np.testing.assert_allclose(features[name], local[name], rtol=1e-5, err_msg=name)
    assert steps and steps[-1][0] == steps[-1][1]


def test_a_project_on_a_node_trains_on_pixels_read_there(tmp_path, node_process):
    """The classifier's pixel group for a project whose image and mask a node
    holds: built on the node, the same matrix a local project gets."""
    from plexora import nodes
    from plexora.plugins.gating.server.classifier import features
    from tests.agent_fixtures import local_handles, make_synthetic_project

    made = make_synthetic_project(tmp_path, name="here", size=256)
    local_matrix, names, info = features.pixel_features(local_handles("here"), "CD8")
    assert info["where"] == "local"

    make_synthetic_project(tmp_path, name="there", size=256)
    node = node_process(f"image:img={made['image_path']}",
                        f"segmentation:cells={made['mask_path']}")
    register("o2", node)
    _status(node, "cells")
    nodes.attach_image("there", node="o2", resource_id="img", channel_names=["DNA", "CD8"])
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    remote_matrix, remote_names, remote_info = features.pixel_features(
        local_handles("there"), "CD8")
    assert remote_info["where"] == "node"
    assert remote_names == names
    np.testing.assert_allclose(remote_matrix, local_matrix, rtol=1e-5)
