"""A panel captured on a turned or mirrored view exports as it was seen.

Core's Rotate and Flip turn the whole viewer (client/src/js/services/
viewTransform.js), and a capture's preview is a crop of what the viewer drew --
turned. The export re-renders from the source pixels, and until the scene
recorded the orientation and the renderer applied it, the export came out
upright under a turned preview: the figure on screen and the figure in the file
were different pictures.

What this holds:

  * **The orientation survives saving.** `schema.normalize_scene` used to keep
    `x, y, w, h` and drop everything else, so the fact was lost before any
    renderer could read it.
  * **A right angle is the upright render rearranged, exactly.** Turning and
    mirroring the pixels of the same box must give the same numbers as numpy
    turning and mirroring the upright render -- no resampling, no drift.
  * **Any angle keeps the frame's middle.** A dot a known distance from the
    frame's centre lands where the viewer's own transform puts it.
  * **An upright panel is untouched**, byte for byte: every figure made before
    this has no orientation and must export exactly as it did.
  * **The browser and the server turn the same way.** `FigureSchema` (the
    preview compositor and Quick Edit) and `render.orient_offset` agree, case
    by case -- tests/js/figure_orientation_probe.mjs.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from plexora.plugins.figure_builder.server import compose, provenance, render, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_orientation_probe.mjs"


def _source(stack):
    """A SourceImage over an in-memory (C, H, W) stack, reading through the
    real `read` -- so the clipping at the slide's edge is production code."""
    source = render.SourceImage.__new__(render.SourceImage)
    source.datasource = "memory"
    source.channels = [{"src": f"/tiles/ch_{index}/"} for index in range(stack.shape[0])]
    source.height, source.width = stack.shape[1], stack.shape[2]
    source.levels = 1
    source._remote = None
    source._file = None
    source.is_brightfield = False
    source.level = lambda level: stack
    return source


def _scene(viewport, channels=1):
    return {
        "viewport": viewport,
        "channels": [{"key": f"ch_{index}", "window": [0, 1000],
                      "color": {"r": 255, "g": 255, "b": 255}, "visible": True}
                     for index in range(channels)],
    }


def _oriented(cx, cy, frame_w, frame_h, degrees, flip_h=False, flip_v=False):
    """schema's viewport for a frame, as FigureSchema.orientedViewport builds it."""
    radians = math.radians(degrees)
    cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
    w = frame_w * cos + frame_h * sin
    h = frame_w * sin + frame_h * cos
    return schema.normalize_viewport({
        "x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h,
        "orientation": {"degrees": degrees, "flip_h": flip_h, "flip_v": flip_v,
                        "frame_w": frame_w, "frame_h": frame_h},
    })


@pytest.fixture
def stack():
    rng = np.random.default_rng(7)
    return rng.integers(0, 1000, size=(1, 200, 300), dtype=np.uint16)


def _pixels(image):
    return np.asarray(image)[..., 0]


# -- the schema --------------------------------------------------------------

def test_the_orientation_survives_normalizing_a_scene():
    scene = schema.normalize_scene({"viewport": {
        "x": 10, "y": 20, "w": 80, "h": 120,
        "orientation": {"degrees": 90, "flip_h": True, "flip_v": False,
                        "frame_w": 120, "frame_h": 80}}})
    assert scene["viewport"]["orientation"] == {
        "degrees": 90.0, "flip_h": True, "flip_v": False, "frame_w": 120.0, "frame_h": 80.0}


def test_an_upright_scene_reads_back_exactly_as_before():
    viewport = {"x": 10, "y": 20, "w": 80, "h": 120}
    assert schema.normalize_scene({"viewport": viewport})["viewport"] == {
        "x": 10.0, "y": 20.0, "w": 80.0, "h": 120.0}
    upright = {**viewport, "orientation": {"degrees": 360, "flip_h": False, "flip_v": False}}
    assert "orientation" not in schema.normalize_scene({"viewport": upright})["viewport"]


@pytest.mark.parametrize("raw, degrees", [(450, 90.0), (-90, 270.0), ("x", 0.0), (None, 0.0)])
def test_degrees_are_normalized(raw, degrees):
    orientation = schema.normalize_orientation(
        {"degrees": raw, "flip_h": True, "frame_w": 5, "frame_h": 6}, {"w": 1, "h": 1})
    assert orientation["degrees"] == degrees


def test_a_missing_frame_falls_back_to_the_box():
    orientation = schema.normalize_orientation({"degrees": 0, "flip_v": True},
                                               {"w": 40.0, "h": 30.0})
    assert (orientation["frame_w"], orientation["frame_h"]) == (40.0, 30.0)


def test_the_panel_s_size_is_the_frame_not_the_box():
    viewport = _oriented(100, 100, 120, 40, 30)
    assert schema.frame_size(viewport) == (120.0, 40.0)
    assert viewport["w"] > 120
    assert schema.frame_size({"x": 0, "y": 0, "w": 7.0, "h": 9.0}) == (7.0, 9.0)


# -- rendering ---------------------------------------------------------------

@pytest.mark.parametrize("degrees, flip_h, flip_v, rearrange", [
    (90, False, False, lambda a: np.rot90(a, k=-1)),
    (90, True, False, lambda a: np.fliplr(np.rot90(a, k=-1))),
    (270, False, True, lambda a: np.flipud(np.rot90(a, k=1))),
    (180, False, False, lambda a: np.rot90(a, k=2)),
    (0, True, False, np.fliplr),
    (0, False, True, np.flipud),
    (0, True, True, lambda a: np.rot90(a, k=2)),
])
def test_a_right_angle_is_the_upright_render_rearranged(stack, degrees, flip_h, flip_v, rearrange):
    source = _source(stack)
    box = {"x": 50.0, "y": 40.0, "w": 120.0, "h": 80.0}
    upright, _ = render.render_panel(source, _scene(dict(box)), 120, 80)

    quarter = degrees % 180 == 90
    frame_w, frame_h = (80.0, 120.0) if quarter else (120.0, 80.0)
    viewport = schema.normalize_viewport({**box, "orientation": {
        "degrees": degrees, "flip_h": flip_h, "flip_v": flip_v,
        "frame_w": frame_w, "frame_h": frame_h}})
    turned, info = render.render_panel(source, _scene(viewport), int(frame_w), int(frame_h))

    assert turned.size == (int(frame_w), int(frame_h))
    assert np.array_equal(_pixels(turned), rearrange(_pixels(upright)))
    assert info["channels_rendered"] == 1


@pytest.mark.parametrize("degrees, flip_h, flip_v", [
    (30, False, False), (30, False, True), (137, True, False), (300, True, True)])
def test_any_angle_keeps_the_field_where_the_viewer_put_it(degrees, flip_h, flip_v):
    stack = np.zeros((1, 400, 400), dtype=np.uint16)
    dot = (236, 176)                                     # x, y in image pixels
    stack[0, dot[1] - 2:dot[1] + 3, dot[0] - 2:dot[0] + 3] = 1000
    source = _source(stack)
    viewport = _oriented(200, 200, 160, 100, degrees, flip_h, flip_v)

    image, _ = render.render_panel(source, _scene(viewport), 160, 100)
    pixels = _pixels(image).astype(float)
    # The dot's centre of mass, in continuous pixel coordinates: a turned 5x5
    # square has no single brightest pixel worth trusting.
    ys, xs = np.indices(pixels.shape)
    total = pixels.sum()
    x = (pixels * (xs + 0.5)).sum() / total
    y = (pixels * (ys + 0.5)).sum() / total

    # The dot's own centre is the middle of pixel 236, i.e. 236.5; the
    # frame's middle is the continuous point (200, 200).
    offset = render.orient_offset(viewport["orientation"], dot[0] + 0.5 - 200, dot[1] + 0.5 - 200)
    want = (80 + offset[0], 50 + offset[1])
    assert abs(x - want[0]) <= 1.0 and abs(y - want[1]) <= 1.0, (x, y, want)


def test_a_turned_frame_off_the_edge_of_the_slide_is_background_there():
    """Padded, not clipped: a raster that shrank to the part of the box on the
    slide would move the frame's middle and show the wrong field."""
    stack = np.full((1, 200, 200), 1000, dtype=np.uint16)
    source = _source(stack)
    viewport = _oriented(10, 100, 60, 60, 90)            # half off the left edge
    image, _ = render.render_panel(source, _scene(viewport), 60, 60)
    pixels = _pixels(image)
    # Turned a quarter clockwise, image-left is screen-up: the frame's middle
    # is 10 px inside the slide, so the top 20 rows of 60 are off it.
    assert pixels[:18, :].max() == 0
    assert pixels[22:, :].min() > 200


def test_an_upright_panel_renders_exactly_as_before(stack):
    source = _source(stack)
    scene = _scene({"x": 50.0, "y": 40.0, "w": 120.0, "h": 80.0})
    image, _ = render.render_panel(source, scene, 120, 80)
    plane = stack[0, 40:120, 50:170].astype(np.float32)
    want = (np.clip(plane / 1000.0, 0, 1) * render.CHANNEL_ALPHA * 255.0).astype(np.uint8)
    assert np.array_equal(_pixels(image), want)


def test_the_effective_dpi_is_measured_across_the_frame():
    source = _source(np.zeros((1, 400, 400), dtype=np.uint16))
    viewport = _oriented(200, 200, 100, 50, 45)
    report = render.panel_report(source, _scene(viewport), 25.4, 300)
    assert report["effective_dpi"] == 100.0


# -- the furniture and the record -------------------------------------------

def test_the_scale_bar_spans_the_frame():
    viewport = _oriented(200, 200, 100, 100, 45)
    document = {"sources": {"s": {"pixel_size": None}}}
    panel = {"source_id": "s", "scene": {"viewport": viewport},
             "scalebar": {"visible": True, "unit": "px", "target_px": 120}}
    # 120 px does not fit across a 100 px frame, though it fits the box.
    assert viewport["w"] > 120
    assert compose.scale_bar(document, panel) is None


def test_the_provenance_says_how_the_field_was_shown():
    viewport = _oriented(200, 200, 100, 50, 90, flip_h=True)
    document = {"sources": {"s": {"display_name": "slide",
                                  "pixel_size": {"value": 0.5, "unit": "um"}}}}
    panel = {"panel_id": "p1", "source_id": "s",
             "scene": {"viewport": viewport, "channels": [], "plugins": {}}}
    text = "\n".join(provenance._panel_lines(document, panel, {"label": "A"}))
    assert "turned 90°, flipped horizontally" in text
    assert "frame 100.0 × 50.0 px" in text
    # The field is the frame's, not the box's: 100 x 50 px at 0.5 um.
    assert compose.format_microns(50.0) in text and compose.format_microns(25.0) in text


# -- the browser agrees ------------------------------------------------------

CASES = [
    {"degrees": 0, "flip_h": True, "flip_v": False},
    {"degrees": 90, "flip_h": False, "flip_v": False},
    {"degrees": 90, "flip_h": True, "flip_v": False},
    {"degrees": 30, "flip_h": False, "flip_v": True},
    {"degrees": 217, "flip_h": True, "flip_v": True},
]


def test_the_browser_turns_the_way_the_exporter_does():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE), json.dumps(CASES)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    report = json.loads(proc.stderr)
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    for case, got in zip(CASES, report["mapped"]):
        for (dx, dy), (x, y) in zip([(10, 0), (0, 10), (3, -7)], got):
            want = render.orient_offset(case, dx, dy)
            assert abs(x - want[0]) < 1e-9 and abs(y - want[1]) < 1e-9, (case, dx, dy, (x, y), want)
