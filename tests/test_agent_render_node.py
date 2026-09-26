"""render_region against an image served by a real data node.

The same pixels either side of the wire, so the same picture: a node-backed
render must be byte-identical to the local render of the same file, and
identical to itself.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.agent import render as agent_render
from plexora.agent.render_spec import RenderInput
from tests.node_harness import node_process  # noqa: F401 - fixture
from tests.test_node_image import _local_project, node_image  # noqa: F401 - fixture


def _spec(project, name):
    return RenderInput(project=project, bounds={"x": 64, "y": 64, "width": 256, "height": 256},
                       channels=[{"name": name, "color": "#ffffff", "window": [0, 6000]}],
                       output={"width": 128}, segmentation="none", scale_bar=False)


def test_a_node_image_renders_like_the_local_file(node_image, tmp_path):
    _node, _attached, path = node_image
    local = _local_project(tmp_path, "here", path)
    local_name = local.image.channels[0].get("fullname") or local.image.channels[0]["name"]

    here = agent_render.render_region(AgentSession(), _spec("here", local_name), store=False)
    remote_a = agent_render.render_region(AgentSession(), _spec("remote", "A"), store=False)
    remote_b = agent_render.render_region(AgentSession(), _spec("remote", "A"), store=False)

    assert remote_a["png"] == remote_b["png"]
    assert remote_a["manifest"]["provenance"]["image"]["provider"] == "node"
    from io import BytesIO

    from PIL import Image

    a = np.asarray(Image.open(BytesIO(here["png"])))
    b = np.asarray(Image.open(BytesIO(remote_a["png"])))
    assert (a == b).all()


def test_a_node_mask_renders_like_the_local_file(tmp_path, node_process):
    import dataclasses

    from plexora.nodes import attach_image, attach_segmentation
    from plexora.server.models.project import Project
    from tests.helpers import ALL_CONFIRMED, project
    from tests.node_harness import register
    from tests.test_node_image import SIZE, _image_file, _mask_file

    image = _image_file(tmp_path)
    mask = _mask_file(tmp_path)
    node = node_process(f"image:slide={image}", f"segmentation:mask={mask}")
    register("both", node)
    project("remote", channels=("A", "B", "C"), confirmed=ALL_CONFIRMED,
            width=SIZE, height=SIZE).save()
    attach_image("remote", node="both", resource_id="slide", channel_names=["A", "B", "C"])
    attach_segmentation("remote", node="both", resource_id="mask")
    remote = Project.load("remote")

    local = _local_project(tmp_path, "here", image)
    local = dataclasses.replace(local, segmentation=dataclasses.replace(
        remote.segmentation, derived=str(mask), source=str(mask)))
    local.save()
    local_name = local.image.channels[0].get("fullname") or local.image.channels[0]["name"]

    def spec(name, channel):
        return RenderInput(project=name, bounds={"x": 0, "y": 0, "width": SIZE,
                                                 "height": SIZE},
                           channels=[{"name": channel, "color": "#ffffff",
                                      "window": [0, 6000]}],
                           segmentation="outlines", output={"width": 128}, scale_bar=False)

    agent_render.close_masks()
    there = agent_render.render_region(AgentSession(), spec("remote", "A"), store=False)
    here = agent_render.render_region(AgentSession(), spec("here", local_name), store=False)
    assert there["manifest"]["segmentation"]["status"] == "rendered"
    assert there["manifest"]["provenance"]["mask"]["provider"] == "node"
    assert there["manifest"]["provenance"]["mask_path"] is None
    assert "segmentation" in there["manifest"]["overlays"]
    from io import BytesIO

    from PIL import Image

    a = np.asarray(Image.open(BytesIO(here["png"])))
    b = np.asarray(Image.open(BytesIO(there["png"])))
    assert (a == b).all()
