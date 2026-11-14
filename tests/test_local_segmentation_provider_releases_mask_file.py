"""A label mask Plexora has finished reading does not stay open.

The derived label pyramid is rebuilt AT THE SAME PATH when the user
re-segments (its name comes from the source's), and the rebuild ends by
renaming the new file over the old one. On Windows that rename is refused
while any handle on the old file is still open. So a mask read once
(`SegHandle.read_region` builds a provider per call), or dropped by the viewer
when another project is opened, must let go of the file then -- not whenever
Python's cycle collector next happens to run.

The cycle collector is held off in these tests: a release that only happens
when it runs is not a release a conversion job can count on.
"""

import gc
import json
from contextlib import contextmanager

import numpy as np
import pytest
import tifffile

from plexora import api
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.utils import segmentation_pyramid
from tests.agent_fixtures import make_synthetic_project


@contextmanager
def _no_cycle_collection():
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()  # let the fixtures' teardown delete the files


def _project_with_derived_mask(tmp_path, name):
    """A project whose mask is a flat TIFF Plexora converted into a filled
    label pyramid, as the segmentation job does. (raw, derived, labels)."""
    info = make_synthetic_project(tmp_path, name=name)
    labels = info["labels"]
    raw = tmp_path / f"_{name}_files" / "cells_mask.tif"
    tifffile.imwrite(raw, labels)
    derived = _convert(raw, tmp_path / "derived" / name)
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config[name].update(segmentation=derived, segmentationSource=str(raw),
                        segmentationMode=segmentation_pyramid.MODE_FILLED)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return raw, derived, labels


def _convert(raw, derived_root):
    """What the segmentation job runs for this mask (start_segmentation_job)."""
    return data_model.convertOmeTiff(
        str(raw), dataDirectory=str(derived_root), isLabelImg=True,
        segmentation_mode_=segmentation_pyramid.MODE_FILLED)["segmentation"]


def _resegment(raw, labels):
    """The user re-segments: same cells, new ids, same file name."""
    tifffile.imwrite(raw, np.where(labels > 0, labels + 1000, 0).astype(np.uint32))


def _rebuild(raw, derived_root, derived):
    try:
        rebuilt = _convert(raw, derived_root)
    except PermissionError as error:
        pytest.fail("the old label pyramid is still held open, so the mask "
                    f"rebuilt from the re-segmented source cannot replace it: {error}")
    assert rebuilt == derived


def test_a_one_shot_mask_read_leaves_the_pyramid_free_to_be_rebuilt(tmp_path):
    name = "one_shot_mask_read"
    raw, derived, labels = _project_with_derived_mask(tmp_path, name)

    with _no_cycle_collection():
        seg = api.SegHandle(Project.load(name))
        # A plugin reads a few labels once; nothing keeps the provider.
        assert set(np.unique(seg.read_region(0, (0, 0, 64, 64))).tolist()) == {0, 1}
        del seg

        _resegment(raw, labels)
        _rebuild(raw, tmp_path / "derived" / name, derived)


def test_opening_another_project_releases_the_previous_projects_mask(tmp_path):
    name, other = "mask_released_first", "mask_released_second"
    raw, derived, labels = _project_with_derived_mask(tmp_path, name)
    make_synthetic_project(tmp_path, name=other, mask=False, seed=1)

    with _no_cycle_collection():
        data_model.load_datasource(name)
        assert data_model.seg is not None  # the viewer has the mask open
        data_model.load_datasource(other)
        assert data_model.seg is None      # ...and has let go of it

        _resegment(raw, labels)
        _rebuild(raw, tmp_path / "derived" / name, derived)
