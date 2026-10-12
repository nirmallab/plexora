"""Which cells are on which plane, for a z-stack and mask a data node holds.

The plane index is written by the measurement pass, and for a stack on a node
that pass runs THERE (node/api.volume_measure); the index travels back beside
the table. What has to hold is that it is the index a local measurement of the
same files writes.
"""

from __future__ import annotations

import numpy as np

from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.test_node_label_volume import (_on_a_node, _stack_and_mask, _status,
                                          _wait_for_local_mask)


def test_a_node_measurement_sends_its_plane_index_back(tmp_path, node_process):
    from plexora import nodes
    from plexora.datasource import register_image_datasource
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import attach_segmentation
    from plexora.server.volume import objects, planes

    image, mask = _stack_and_mask(tmp_path)
    register_image_datasource("here", image)
    attach_segmentation("here", str(mask))
    _wait_for_local_mask("here")
    objects.measure_project_objects(Project.load("here"), units="voxel", attach=False)

    node = node_process(f"image:img={image}", f"segmentation:cells={mask}")
    register("o2", node)
    _status(node, "cells")
    _on_a_node("there", image, "o2")
    nodes.attach_segmentation("there", node="o2", resource_id="cells")
    objects.measure_project_objects(Project.load("there"), units="voxel", attach=False)

    local = planes.index_for(objects.object_paths(Project.load("here"))[0])
    remote = planes.index_for(objects.object_paths(Project.load("there"))[0])
    assert remote is not None and remote.rows == local.rows > 0
    assert remote.depth == local.depth
    for column in ("label", "z", "t", "voxels"):
        assert np.array_equal(getattr(remote, column), getattr(local, column))
