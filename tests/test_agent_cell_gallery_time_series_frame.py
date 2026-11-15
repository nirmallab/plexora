"""On a time series, a picture of a cell is drawn at the frame its row is from.

A measured time series' object table holds one row per object per frame (the
`t` role), and until the frames are linked each frame numbers its objects on
its own, so one id can be two different cells. render_cell_gallery and
explain_cell report a row's value and centroid; the crop under them has to be
that frame's pixels and that frame's mask, or the cell outlined is another
frame's object at another moment.
"""

import numpy as np
import pytest
import tifffile

from plexora import datasets
from plexora.agent import AgentSession, invoke, registry
from plexora.datasource import register_image_datasource
from plexora.server.models.project import Project

pytestmark = pytest.mark.pro

FRAMES, DEPTH, SIZE = 2, 4, 64


def _series(tmp_path, name):
    """Two frames; frame 1 is 1000 brighter everywhere. Frame 0 numbers its
    cells A=1, B=2; frame 1 numbers them A=2 (moved 12 px right), B=5 -- so
    id 2 is B in frame 0 and A in frame 1, and id 5 is only in frame 1."""
    from plexora.server.volume import objects

    rng = np.random.default_rng(0)
    image = rng.integers(50, 60, (FRAMES, 1, DEPTH, SIZE, SIZE)).astype(np.uint16)
    image[1] += 1000
    metadata = {"axes": "TCZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5,
                "PhysicalSizeZ": 1.0, "TimeIncrement": 30.0, "TimeIncrementUnit": "s",
                "Channel": {"Name": ["DNA"]}}
    path = tmp_path / f"{name}.ome.tif"
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(image, metadata=metadata)
    labels = np.zeros((FRAMES, DEPTH, SIZE, SIZE), dtype=np.uint16)
    labels[0, 1:3, 10:18, 10:18] = 1
    labels[0, 1:4, 40:50, 40:50] = 2
    labels[1, 1:3, 10:18, 22:30] = 2
    labels[1, 1:4, 40:50, 40:50] = 5
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels, photometric="minisblack")
    register_image_datasource(name, str(path))
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    summary = objects.measure_project_objects(Project.find(name), units="physical")
    assert summary["attached"] and Project.find(name).dataset.roles.t == "t"


def _frames_drawn(monkeypatch):
    """The frame of every render_region the tools ask for."""
    from plexora.agent import render
    from plexora.api.representation import to_spec

    drawn = []
    real = render.render_region

    def recording(session, spec, *args, **kwargs):
        view = getattr(spec, "view", None)
        drawn.append(to_spec(view).t if view else 0)
        return real(session, spec, *args, **kwargs)

    monkeypatch.setattr(render, "render_region", recording)
    return drawn


def test_a_gallery_of_a_series_draws_each_cell_at_its_rows_frame(tmp_path, monkeypatch):
    _series(tmp_path, "galseries")
    registry.discover(["gating"])
    drawn = _frames_drawn(monkeypatch)
    outcome = invoke(AgentSession(), "render_cell_gallery", {
        "project": "galseries", "marker": "DNA", "select": "brightest", "n": 2,
        "low": 500, "high": 1e9, "crop_px": 32, "tile_px": 64})
    assert outcome["ok"], outcome
    tiles = outcome["result"]["manifest"]["tiles"]
    # The two brightest rows are frame 1's (ids 2 and 5, both ~1000 brighter).
    assert sorted(t["cell_id"] for t in tiles) == [2, 5]
    assert all(t["value"] > 900 for t in tiles), tiles
    assert drawn == [1, 1], (
        f"each tile shows its row's frame (1), not frame 0; frames drawn: {drawn}")


def test_explain_cell_on_a_series_crops_the_frame_its_values_come_from(tmp_path,
                                                                        monkeypatch):
    _series(tmp_path, "expseries")
    registry.discover(["roi"])
    drawn = _frames_drawn(monkeypatch)
    outcome = invoke(AgentSession(), "explain_cell", {
        "project": "expseries", "cell_id": 5, "marker": "DNA", "crop_px": 32})
    assert outcome["ok"], outcome
    result = outcome["result"]
    # Cell 5 exists only in frame 1: its values are frame 1's...
    assert result["markers"]["DNA"]["value"] > 900, result["markers"]
    # ...so its crop is frame 1, where id 5 is in the mask; frame 0 has no 5.
    assert drawn == [1], f"the crop shows frame 1, not frame 0; frames drawn: {drawn}"


def test_sections_of_a_series_outline_the_frame_they_draw(tmp_path):
    """render_sections at t=1 cuts frame 1's mask: id 5 is only in frame 1,
    so its highlighted outline appears; with frame 0's labels it never did."""
    import io

    from PIL import Image

    _series(tmp_path, "secseries")
    registry.discover([])
    outcome = invoke(AgentSession(), "render_sections", {
        "project": "secseries", "x": 45, "y": 45, "z": 2, "t": 1, "size_px": 32,
        "output_px": 128, "segmentation": "outlines", "highlight": 5,
        "channels": [{"name": "DNA", "color": "#ffffff", "window": [0, 2000]}]})
    assert outcome["ok"], outcome
    pixels = np.asarray(Image.open(io.BytesIO(outcome["result"]["_images"][0])).convert("RGB"))
    assert ((pixels == (255, 214, 10)).all(axis=-1)).any(), "cell 5 of frame 1 is outlined"
