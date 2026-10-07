"""Review images an agent can judge: an artifact category's tiles drawn in
its nuclear and lead channels (not black for want of a channel), and a
black or dim score-review tile redrawn stretched, marked `*`."""

from types import SimpleNamespace

import numpy as np
import pytest


def test_an_artifact_field_is_drawn_in_its_nuclear_and_lead_channel(monkeypatch):
    from plexora.agent.evidence import calibration
    from plexora.plugins.qc.server import artifacts, score_review, sheets

    summary = {"nuclear": ["DNA_1", "DNA_2"], "pan_channels": ["DNA_1", "CD3", "CD8"]}
    objects = [{"source_channel": "CD8", "area_um2": 100.0},
               {"source_channel": "CD3", "area_um2": 50.0},
               {"source_channel": "CD8", "area_um2": 20.0}]
    assert artifacts.display_channels(summary, objects) == ["DNA_1", "CD8"]
    # Nothing found, or found in the nuclear stain only: the first pan marker.
    assert artifacts.display_channels(summary, []) == ["DNA_1", "CD3"]
    assert artifacts.display_channels(
        summary, [{"source_channel": "DNA_1", "area_um2": 9.0}]) == ["DNA_1", "CD3"]
    # Saturation: the channel most saturated.
    assert artifacts.display_channels(
        summary, [{"source_channel": "CD20", "area_um2": 5.0}]) == ["DNA_1", "CD20"]

    monkeypatch.setattr(calibration, "current", lambda *a, **k: None)
    subject = SimpleNamespace(check="artifacts", channel=None, reference="fold",
                              display_channels=["DNA_1", "CD8"])
    drawn = score_review._channels(object(), "p", subject)
    assert [c.name for c in drawn] == ["DNA_1", "CD8"]
    assert [c.color for c in drawn] == [sheets.NUCLEAR_COLOR, sheets.MARKER_COLOR]
    # A field with a channel of its own keeps it.
    subject = SimpleNamespace(check="blur", channel="DNA_2", reference=None,
                              display_channels=None)
    assert [c.name for c in score_review._channels(object(), "p", subject)] == ["DNA_2"]


def _fake_draw(levels):
    """A `_draw` whose panel is `levels[(x, y)]` grey at the calibrated
    window and bright once stretched (`window == [0, 10]`)."""
    from PIL import Image

    calls = []

    def draw(session, project, scan, bounds, channels, size, *, pixel, shapes=None,
             scale_bar=True, segmentation="none"):
        stretched = any(c.window == [0.0, 10.0] for c in channels)
        key = (round(bounds["x"] + bounds["width"] / 2), round(bounds["y"]
                                                               + bounds["height"] / 2))
        value = 200 if stretched else levels[key]
        calls.append((key, stretched))
        manifest = {"bounds_fullres": dict(bounds), "level": 0,
                    "channels": [{"name": c.name, "window": c.window if isinstance(
                        c.window, list) else [0.0, 1.0]} for c in channels]}
        return Image.new("RGB", (size, size), (value, value, value)), manifest

    return draw, calls


def test_a_black_or_dim_tile_is_redrawn_stretched(monkeypatch):
    from plexora.agent.render_spec import ChannelSpec
    from plexora.plugins.qc.server import sheets

    levels = {(100, 100): 0, (300, 300): 50, (500, 500): 180}
    draw, calls = _fake_draw(levels)
    monkeypatch.setattr(sheets, "_draw", draw)
    monkeypatch.setattr(sheets, "stretch_window", lambda scan, name, box: [0.0, 10.0])
    field = SimpleNamespace(grid={"step": 10.0, "image_size": [1000, 1000]},
                            score_name="blur score", check="blur", channel="DNA_1",
                            reference=None, fingerprint="fp", values=np.zeros((1, 1)),
                            valid=np.zeros((1, 1), dtype=bool))
    rows = [("clear_good", [{"x": 100.0, "y": 100.0, "score": 0.1},
                            {"x": 300.0, "y": 300.0, "score": 0.2},
                            {"x": 500.0, "y": 500.0, "score": 0.3}])]
    sheet = sheets.score_sheet(object(), "p", object(), field, rows, threshold=0.5,
                               tile_px_side=64.0, channels=[ChannelSpec(name="DNA_1",
                                                                        window=[0.0, 99.0])],
                               fmt="webp", pixel=None, overview=False, store=False)
    manifest = sheet["manifest"]
    tiles = manifest["rows"][0]["tiles"]
    assert [t.get("stretched", False) for t in tiles] == [True, True, False]
    assert manifest["stretched"] is True and manifest["footnote"] == sheets.STRETCHED_FOOTNOTE
    assert tiles[0]["windows"] == {"DNA_1": [0.0, 10.0]}
    # The bright tile was drawn once; the black and the dim one twice.
    assert [s for k, s in calls if k == (500, 500)] == [False]
    assert sum(1 for k, _ in calls if k == (300, 300)) == 2


def test_the_dim_level_ignores_the_scale_bar():
    from plexora.plugins.qc.server import sheets

    picture = np.zeros((152, 152, 3), dtype=np.uint8)
    picture[-20:, :40] = 255
    assert sheets._level_p99(picture) == pytest.approx(255)
    assert sheets._level_p99(picture, scale_bar=True) == 0.0
