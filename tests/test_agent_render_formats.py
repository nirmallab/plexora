"""render_region on every image format the viewer opens.

`SourceImage` used to open TIFFs and colour slides only, so an agent (and a
Figure Builder export) refused OME-Zarr, DICOM and Xenium focus folders. It now
dispatches the way `LocalImageProvider.open` does. Each test renders a region
at level 0 with a known window and checks the pixels against that format's own
reader: the shader's arithmetic, `clip((raw - lo) / span) * 255 * ALPHA`.
"""

import dataclasses
import io
import json

import numpy as np
import pytest
from PIL import Image

from plexora.agent import AgentSession
from plexora.agent import render as agent_render
from plexora.agent.render_spec import RenderInput
from plexora.server.models.project import Project
from plexora.server.utils import source_image
from tests.helpers import ALL_CONFIRMED, image_spec, project


def _register(tmp_path, name, src, channels, width, height, *, kind="ome_tiff",
              levels=1):
    record = project(name, image=image_spec(channels=channels, width=width,
                                            height=height, src=str(src)),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, kind=kind, max_level=levels - 1))
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config))
    return Project.load(name)


def _render(name, channel, box, window):
    x, y, w, h = box
    spec = RenderInput(project=name, bounds={"x": x, "y": y, "width": w, "height": h},
                       channels=[{"name": channel, "color": "#ffffff", "window": window}],
                       output={"width": w, "height": h}, level=0, segmentation="none",
                       scale_bar=False)
    result = agent_render.render_region(AgentSession(), spec, store=False)
    source_image.close_readers()
    return np.asarray(Image.open(io.BytesIO(result["png"]))), result["manifest"]


def _expected(raw, window):
    lo, hi = window
    scaled = np.clip((raw.astype(np.float32) - lo) / (hi - lo), 0, 1)
    return (np.clip(scaled * (source_image.CHANNEL_ALPHA / 255.0) * 255.0, 0, 1)
            * 255.0).astype(np.uint8)


def _check(rendered, raw, window):
    expected = _expected(raw, window)
    # Every output pixel is the channel's value, white on all three samples.
    assert rendered.shape[:2] == expected.shape
    assert np.abs(rendered[..., 0].astype(int) - expected.astype(int)).max() <= 1
    assert (rendered[..., 0] == rendered[..., 2]).all()


def test_an_ome_zarr_image(tmp_path):
    from plexora.server.utils import ome_zarr
    from tests.ngff_fixtures import write_ngff

    path = write_ngff(tmp_path / "image.zarr", shape=(2, 256, 256), levels=3)
    _register(tmp_path, "ngff", path, ("A", "B"), 256, 256, kind="ome_zarr", levels=3)
    window = [0, 4000]
    rendered, manifest = _render("ngff", "B", (40, 60, 64, 48), window)
    raw = np.asarray(ome_zarr.open_image(str(path))[0][1, 60:108, 40:104])
    _check(rendered, raw, window)
    assert manifest["level"] == 0 and manifest["channels"][0]["index"] == 1


def test_an_ome_zarr_with_t_and_z_axes(tmp_path):
    from plexora.server.utils import ome_zarr
    from tests.ngff_fixtures import write_ngff

    path = write_ngff(tmp_path / "tczyx.zarr", shape=(1, 2, 1, 128, 128), levels=2,
                      axes="tczyx")
    _register(tmp_path, "tczyx", path, ("A", "B"), 128, 128, kind="ome_zarr", levels=2)
    window = [0, 4000]
    rendered, _ = _render("tczyx", "A", (0, 0, 32, 32), window)
    raw = np.asarray(ome_zarr.open_image(str(path))[0][0, 0:32, 0:32])
    _check(rendered, raw, window)


def test_a_dicom_multiplex_slide(tmp_path):
    pytest.importorskip("wsidicom")
    from plexora.server.utils import dicom_wsi
    from tests.dicom_fixtures import MARKERS, write_if_slide

    folder = write_if_slide(tmp_path / "slide", height=256, width=320, levels=2)
    _register(tmp_path, "dicom", folder, MARKERS, 320, 256, kind="dicom_wsi", levels=2)
    window = [0, 30000]
    rendered, manifest = _render("dicom", "CD3", (100, 50, 64, 64), window)
    pyramid = dicom_wsi.open_image(str(folder))
    try:
        raw = np.asarray(pyramid[0][1, 50:114, 100:164])
    finally:
        pyramid.close()
    assert raw.max() > 0
    _check(rendered, raw, window)
    assert manifest["brightfield"] is False


def test_a_xenium_focus_folder(tmp_path):
    from plexora.server.utils import xenium_focus
    from tests.test_xenium_focus import _folder

    folder, _planes = _folder(tmp_path, names=("DAPI", "18S"), height=64, width=48)
    # Something to see: a bright square in the second channel.
    import tifffile

    second = sorted(folder.glob("*.ome.tif"))[1]
    plane = np.full((64, 48), 2, dtype=np.uint16)
    plane[10:30, 5:25] = 900
    tifffile.imwrite(second, plane, ome=True, metadata={"Channel": {"Name": "18S"}},
                     photometric="minisblack")
    _register(tmp_path, "focus", folder, ("DAPI", "18S"), 48, 64, kind="xenium_focus")
    window = [0, 1000]
    rendered, _ = _render("focus", "18S", (0, 0, 48, 64), window)
    pyramid = xenium_focus.open_focus(str(folder))
    raw = np.asarray(pyramid[0][1, 0:64, 0:48])
    _check(rendered, raw, window)
    assert rendered[20, 15, 0] > rendered[50, 40, 0]


def test_a_dicom_he_slide_renders_as_colour(tmp_path):
    pytest.importorskip("wsidicom")
    from tests.dicom_fixtures import write_he_slide

    folder = write_he_slide(tmp_path / "he", height=256, width=320)
    record = project("he", image=image_spec(channels=(), width=320, height=256,
                                            src=str(folder)), confirmed=ALL_CONFIRMED)
    entry = record.to_entry()
    entry["imageData"] = [{"name": "rgb", "fullname": "H&E", "src": "/generated/data/he/rgb/"}]
    entry["kind"] = entry.get("kind")
    record = Project.from_entry("he", entry)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, kind="brightfield", max_level=0))
    (tmp_path / "config.json").write_text(json.dumps({"he": record.to_entry()}))
    spec = RenderInput(project="he", bounds={"x": 0, "y": 0, "width": 320, "height": 256},
                       output={"width": 160}, segmentation="none", scale_bar=False)
    result = agent_render.render_region(AgentSession(), spec, store=False)
    source_image.close_readers()
    assert result["manifest"]["brightfield"] is True
    rgb = np.asarray(Image.open(io.BytesIO(result["png"])))
    # A pale field with a stained patch: not black, and not one flat colour.
    assert rgb.mean() > 100 and rgb.std() > 5
