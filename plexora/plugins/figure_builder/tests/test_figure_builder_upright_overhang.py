"""An upright panel whose viewport runs off the image exports as the preview shows it.

Quick Edit lets a panel zoom out past the slide and pan across its edge, and
saves that viewport as it is. The preview (figurePanelCompositor.renderPreview)
clamps the region to the image and draws the part that exists at its own offset
inside the frame, with background around it. The export must do the same: the
part of the image that exists in the same place, at the same scale, and the
background where the image is not -- not that part stretched to fill the panel.
"""

import numpy as np

from plexora.plugins.figure_builder.server import render


def _source(stack, brightfield=False):
    """A SourceImage over an in-memory stack, reading through the real `read`
    and `read_rgb` -- so the clipping at the slide's edge is production code."""
    source = render.SourceImage.__new__(render.SourceImage)
    source.datasource = "memory"
    source.channels = [{"src": f"/tiles/ch_{index}/"} for index in range(stack.shape[0])]
    source.height, source.width = stack.shape[-2], stack.shape[-1]
    source.levels = 1
    source._remote = None
    source._file = None
    source.is_brightfield = brightfield
    source.level = lambda level: stack
    return source


def _scene(viewport, channels=1):
    return {
        "viewport": viewport,
        "channels": [{"key": f"ch_{index}", "window": [0, 1000],
                      "color": {"r": 255, "g": 255, "b": 255}, "visible": True}
                     for index in range(channels)],
    }


def test_an_upright_panel_past_the_right_edge_keeps_the_image_in_place():
    """A 100-pixel field starting halfway across a 100-pixel image: the image's
    right half belongs in the panel's left half, and the panel's right half is
    background. A bright stripe on the image's last ten columns lands on panel
    columns 40-49, not stretched across 80-99."""
    stack = np.zeros((1, 100, 100), dtype=np.uint16)
    stack[0, :, 90:100] = 1000
    image, _ = render.render_panel(
        _source(stack), _scene({"x": 50, "y": 0, "w": 100, "h": 100}), 100, 100)
    row = np.asarray(image)[50, :, 0]
    lit = np.flatnonzero(row > 128)
    assert (int(lit.min()), int(lit.max())) == (40, 49)
    assert row[52:].max() == 0


def test_an_upright_panel_hanging_off_the_top_left_corner_is_not_stretched():
    """Overhanging on one axis only must not squash the other: a viewport that
    begins 50 pixels above and to the left of the image puts the image's corner
    at the middle of the panel, with black above and to the left of it."""
    stack = np.full((1, 100, 100), 1000, dtype=np.uint16)
    image, _ = render.render_panel(
        _source(stack), _scene({"x": -50, "y": -50, "w": 100, "h": 100}), 100, 100)
    pixels = np.asarray(image)[..., 0]
    assert pixels[:45, :].max() == 0
    assert pixels[:, :45].max() == 0
    assert pixels[55:, 55:].min() >= 200


def test_an_upright_brightfield_panel_past_the_edge_pads_with_white():
    """The brightfield counterpart: what exists stays where it is and the rest
    is the white the viewer draws behind a brightfield slide."""
    stack = np.zeros((3, 100, 100), dtype=np.uint8)
    stack[:, :, 90:100] = 0
    stack[:, :, :90] = 128
    image, _ = render.render_panel(
        _source(stack, brightfield=True),
        {"viewport": {"x": 50, "y": 0, "w": 100, "h": 100}, "channels": []}, 100, 100)
    row = np.asarray(image)[50, :, 0]
    assert row[:38].min() >= 100 and row[:38].max() <= 156   # image grey, columns 50-87
    assert row[41:48].max() <= 30                            # dark stripe, columns 90-99
    assert row[52:].min() == 255                             # off the slide: white
