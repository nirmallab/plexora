"""The agent's mask shelf follows a mask rebuilt at the same path.

`render._mask_for` keeps a project's mask provider open between renders. When
the user re-segments and Plexora rebuilds the derived label pyramid, the new
pyramid lands at the SAME path (its name is derived from the source's). The
agent's next render, gallery, QC check or classifier pass must read the new
labels -- and holding the old file open must not stop the rebuild.
"""

import json

import numpy as np
import pytest
import tifffile

from plexora.agent import render as agent_render
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.utils import segmentation_pyramid
from tests.agent_fixtures import make_synthetic_project

NAME = "rebuilt_mask"


def _convert(raw, derived_root):
    """What the segmentation job runs for this mask (start_segmentation_job)."""
    return data_model.convertOmeTiff(
        str(raw), dataDirectory=str(derived_root), isLabelImg=True,
        segmentation_mode_=segmentation_pyramid.MODE_FILLED)["segmentation"]


def _labels_at_top_left(record):
    provider, status, reason, _locator = agent_render._mask_for(record)
    assert status == "rendered", reason
    return set(np.unique(provider.read_region(0, (0, 0, 64, 64))).tolist())


def test_a_mask_rebuilt_at_the_same_path_is_read_anew_and_can_be_rebuilt(tmp_path):
    info = make_synthetic_project(tmp_path, name=NAME)
    labels = info["labels"]
    raw = tmp_path / f"_{NAME}_files" / "cells_mask.tif"
    tifffile.imwrite(raw, labels)  # flat: Plexora builds its own pyramid of it
    derived = _convert(raw, tmp_path / "derived")

    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config[NAME].update(segmentation=derived, segmentationSource=str(raw))
    config_path.write_text(json.dumps(config), encoding="utf-8")

    # An agent looks at the project once: cell 1 sits in the top-left corner.
    assert _labels_at_top_left(Project.load(NAME)) == {0, 1}

    # The user re-segments: same cells, new ids. Plexora rebuilds the pyramid.
    tifffile.imwrite(raw, np.where(labels > 0, labels + 1000, 0).astype(np.uint32))
    try:
        rebuilt = _convert(raw, tmp_path / "derived")
    except PermissionError as error:
        pytest.fail("the agent's mask shelf still holds the old pyramid open, so the "
                    f"rebuilt mask cannot replace it: {error}")
    assert rebuilt == derived  # the same path, so only the file's identity changed

    record = Project.load(NAME)
    assert record.segmentation.derived == derived
    assert _labels_at_top_left(record) == {0, 1001}, (
        "the agent still reads the segmentation that was replaced")
