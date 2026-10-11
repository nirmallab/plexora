"""Segmenting a z-stack in 3-D (plugins/segmentation), Cellpose stood in for.

Cellpose is an optional extra this suite does not install, so the model is
replaced by a threshold-and-label stand-in; what is tested is everything
around it: the plane cut into blocks that cover it, each cell written once by
the block whose core holds its centre (never twice, never cut where a
neighbour sees it whole), a coarse level drawn back up to the image's size,
the label volume made the project's mask, measured, and recorded -- and the
refusals (no Cellpose, no z step for 3-D, a mask already there).
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.plugins.segmentation.server import cellpose3d, run
from plexora.server.models.project import Project
from plexora.server.volume import labels as volume_labels
from tests import volume_fixtures as vf


@pytest.fixture
def client():
    return plexora.app.test_client()


def _stand_in(image, settings):
    """Cellpose's stand-in: the nuclear channel thresholded, components labelled."""
    from scipy import ndimage

    mask = image[:, 0] > 500
    labels, _ = ndimage.label(mask)
    return labels.astype(np.uint32)


@pytest.fixture
def cellpose(monkeypatch):
    monkeypatch.setattr(cellpose3d, "installed", lambda: True)
    monkeypatch.setattr(cellpose3d, "version", lambda: "4.2.1")
    monkeypatch.setattr(cellpose3d, "segment_block", _stand_in)
    return monkeypatch


def _stack_project(tmp_path, name="seg", physical_z=2.0, size=96, levels=1):
    """Nuclei as bright boxes through some planes; one straddles x = 48."""
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=1,
                                     depth=6, height=size, width=size, names=["DNA"],
                                     physical_z=physical_z)
    image = np.full((1, 6, size, size), 50, dtype=np.uint16)
    image[0, 1:4, 10:20, 10:20] = 1000
    image[0, 2:5, 40:52, 42:54] = 1000          # across the 48-px block edge
    image[0, 3:6, 70:80, 15:25] = 1000
    metadata = {"axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5,
                **({"PhysicalSizeZ": physical_z} if physical_z else {}),
                "Channel": {"Name": ["DNA"]}}
    with tifffile.TiffWriter(path, ome=True) as writer:
        writer.write(image, subifds=levels - 1, metadata=metadata)
        for level in range(1, levels):
            step = 2 ** level
            writer.write(image[..., ::step, ::step], subfiletype=1)
    register_image_datasource(name, path)
    return image


def test_blocks_tile_the_plane_and_their_halo_stays_inside():
    boxes = list(cellpose3d.blocks(100, 70, block=48, halo=8))
    covered = np.zeros((100, 70), dtype=int)
    for (y0, y1, x0, x1), (py0, py1, px0, px1) in boxes:
        covered[y0:y1, x0:x1] += 1
        assert py0 <= y0 and py1 >= y1 and px0 <= x0 and px1 >= x1
        assert py0 >= 0 and px0 >= 0 and py1 <= 100 and px1 <= 70
    assert covered.min() == 1 and covered.max() == 1, "the cores tile the plane exactly"


def test_a_cell_across_a_block_edge_is_written_once_whole():
    out = np.zeros((2, 20, 40), dtype=np.uint32)
    cell = np.zeros((2, 20, 40), dtype=np.uint32)
    cell[:, 5:10, 16:26] = 1                       # centre x 20.5: the left core's
    left_core, left_pad = (0, 20, 0, 20), (0, 20, 0, 28)
    right_core, right_pad = (0, 20, 20, 40), (0, 20, 12, 40)
    next_id = cellpose3d.place(out, cell[:, :, 0:28], left_core, left_pad, 1)
    next_id = cellpose3d.place(out, cell[:, :, 12:40], right_core, right_pad, next_id)
    assert next_id == 2, "written by one block only"
    assert np.array_equal(out > 0, cell > 0), "and whole, including the part past the edge"


def test_a_stack_is_segmented_made_the_mask_measured_and_recorded(tmp_path, cellpose):
    image = _stack_project(tmp_path)
    cellpose.setattr(cellpose3d, "BLOCK", 48)
    record = run.segment_project(Project.find("seg"), nuclear="DNA", mode="3d")
    assert record["cells"] == 3, record
    project = Project.find("seg")
    assert volume_labels.is_label_volume(project.segmentation.derived)
    volume = volume_labels.open_label_volume(project.segmentation.derived)
    assert (volume.depth, volume.height, volume.width) == (6, 96, 96)
    planes = volume.read(0, 0, 5, slice(None), slice(None))
    assert np.array_equal(planes > 0, image[0] > 500), "every bright voxel, nothing else"
    straddler = np.unique(planes[2:5, 40:52, 42:54])
    assert straddler.size == 1 and straddler[0] > 0, "one id for the cell across the edge"
    assert record["objects"]["objects"] == 3 and record["resolution"]["anisotropy"] == 4.0
    assert run.last_run(project)["cells"] == 3
    assert "licence" in record and record["engine"]["model"] == "Cellpose-SAM"


def test_a_coarse_level_is_drawn_back_up_to_the_images_size(tmp_path, cellpose):
    _stack_project(tmp_path, "coarse", levels=2)
    record = run.segment_project(Project.find("coarse"), nuclear="DNA", mode="stitch",
                                 level=1, measure=False)
    volume = volume_labels.open_label_volume(Project.find("coarse").segmentation.derived)
    assert (volume.height, volume.width) == (96, 96) and record["resolution"]["factor"] == 2


def test_what_it_refuses(tmp_path, cellpose, monkeypatch):
    _stack_project(tmp_path, "noz", physical_z=None)
    with pytest.raises(run.SegmentationRefused, match="distance between planes"):
        run.segment_project(Project.find("noz"), nuclear="DNA", mode="3d")
    run.segment_project(Project.find("noz"), nuclear="DNA", mode="stitch", measure=False)
    with pytest.raises(run.SegmentationRefused, match="already has a mask"):
        run.segment_project(Project.find("noz"), nuclear="DNA", mode="stitch")
    monkeypatch.setattr(cellpose3d, "installed", lambda: False)
    with pytest.raises(cellpose3d.CellposeMissing):
        run.segment_project(Project.find("noz"), nuclear="DNA", mode="stitch",
                            replace_mask=True)


def test_the_card_starts_a_run_and_asks_before_replacing_a_mask(tmp_path, client, cellpose):
    import time

    _stack_project(tmp_path, "card")
    state = client.get("/plugins/segmentation/api/state?datasource=card").get_json()
    assert state["channels"] == ["DNA"] and state["cellpose"]["installed"] is True
    started = client.post("/plugins/segmentation/api/run",
                          json={"datasource": "card", "nuclear": "DNA", "mode": "stitch"})
    assert started.status_code == 202, started.get_data(as_text=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get("/plugins/segmentation/api/state?datasource=card").get_json()
        if (state.get("task") or {}).get("status") not in ("pending",):
            break
        time.sleep(0.1)
    assert state["task"]["status"] == "ready", state["task"]
    assert state["last"]["cells"] == 3 and state["has_mask"] is True
    again = client.post("/plugins/segmentation/api/run",
                        json={"datasource": "card", "nuclear": "DNA", "mode": "stitch"})
    assert again.status_code == 409 and again.get_json()["needs"] == "replace_mask"
