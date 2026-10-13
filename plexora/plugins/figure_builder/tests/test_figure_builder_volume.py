"""A panel captured in the 3-D view exports from the picture the viewer drew.

Figure Builder's server renderer draws planes, not volumes: a panel captured
while the 3-D view was on screen carries the view's state in its scene
(`plugins.volume`, contributed by core's 3-D view) and its preview raster is
what the browser's renderer drew, at print size. Export takes that raster,
resamples it to the page, and says so -- in preflight and in the warnings --
rather than re-rendering a plane the panel never showed.
"""

import io

import numpy as np
import pytest

from plexora.plugins.figure_builder.server import export, repository

from plexora.plugins.figure_builder.tests.test_figure_builder_export import figure  # noqa: F401


def _volume_panel(figure_id, version=1, preview_px=(480, 360), extra=None):
    from PIL import Image

    repository.apply(figure_id, version, [
        {"op": "add_panel", "panel": {
            "panel_id": "pnl_3d", "source_id": "src_1",
            "scene": {
                "source_id": "src_1",
                "viewport": {"x": 0, "y": 0, "w": 256, "h": 192},
                "channels": [],
                "plugins": {"volume": {"version": "1", "state": {
                    "mode": "volume", "camera": {"q": [0, 0, 0, 1], "dist": 2.2, "pan": [0, 0]},
                    "surfaces": [{"id": "surf-abc", "visible": True, "style": "glass"}],
                    **(extra or {})}}},
            },
            "placement": {"page_id": "pg_1", "x_mm": 80, "y_mm": 20,
                          "w_mm": 40, "h_mm": 30, "z": 1},
        }},
    ])
    picture = np.zeros((preview_px[1], preview_px[0], 3), np.uint8)
    picture[:, :, 1] = 180                       # green: what the 3-D view drew
    buffer = io.BytesIO()
    Image.fromarray(picture).save(buffer, format="PNG")
    assert repository.put_preview(figure_id, "pnl_3d", 1, buffer.getvalue(),
                                  width=preview_px[0], height=preview_px[1], fmt="png")


def test_a_3d_panel_keeps_the_views_state(figure):
    _volume_panel(figure)
    panel = repository.load(figure)["panels"]["pnl_3d"]
    state = export.volume_state(panel)
    assert state["mode"] == "volume" and state["camera"]["dist"] == 2.2
    assert state["surfaces"][0]["style"] == "glass"
    assert export.volume_state(repository.load(figure)["panels"]["pnl_1"]) is None


def test_a_3d_panel_exports_its_client_raster_and_says_so(figure):
    _volume_panel(figure)
    document = repository.load(figure)
    panel = document["panels"]["pnl_3d"]
    image, report = export._render_unflipped(document, panel, {}, 300)
    pixels = np.asarray(image)
    assert report["client_rendered"] is True
    # 480 pixels over 40 mm is ~305 dpi.
    assert report["effective_dpi"] == pytest.approx(480 / (40 / 25.4), abs=1)
    assert pixels.shape[2] == 3 and int(pixels[pixels.shape[0] // 2, pixels.shape[1] // 2, 1]) == 180
    preflight = export.preflight(document, {"dpi": 300})
    kinds = {warning["kind"] for warning in preflight["warnings"]}
    assert "client_rendered" in kinds
    row = next(p for p in preflight["per_panel"] if p["panel_id"] == "pnl_3d")
    assert row["client_rendered"] is True


def test_a_small_3d_capture_is_flagged_as_low_resolution(figure):
    _volume_panel(figure, preview_px=(120, 90))
    report = export.preflight(repository.load(figure), {"dpi": 300})
    low = next(w for w in report["warnings"] if w["kind"] == "low_resolution")
    assert "pnl_3d" in low["panels"]


def test_a_3d_panel_reaches_the_exported_page(figure, tmp_path):
    _volume_panel(figure)
    result = export.export(repository.load(figure), tmp_path / "out", {"format": "png", "dpi": 150})
    assert any(w["kind"] == "client_rendered" for w in result.get("warnings", []))
    from PIL import Image

    page = next(p for p in result["files"] if p.endswith(".png"))
    with Image.open(page) as opened:
        pixels = np.asarray(opened.convert("RGB"))
    # The panel sits at x 80-120 mm, y 20-50 mm on A4 at 150 dpi.
    x = int((100 / 25.4) * 150)
    y = int((35 / 25.4) * 150)
    assert int(pixels[y, x, 1]) == pytest.approx(180, abs=6) and int(pixels[y, x, 0]) < 20


def test_a_cut_3d_panel_says_how_it_was_cut(figure):
    """The cut the 3-D view drew (the viewer's inspect, in its own words) is in
    the panel's report: a cropped or cut-away picture never passes for the
    whole stack."""
    cut = "crop X 100–600 µm, Z planes 4–9 · 12.0 µm"
    _volume_panel(figure, extra={"cut": cut, "inspect": {"mode": "crop", "axes": {"x": True, "z": True}},
                                 "z": 12})
    document = repository.load(figure)
    panel = document["panels"]["pnl_3d"]
    assert export.volume_state(panel)["inspect"]["mode"] == "crop"
    assert export.volume_cut(panel) == cut
    row = next(p for p in export.preflight(document, {"dpi": 300})["per_panel"] if p["panel_id"] == "pnl_3d")
    assert row["cut"] == cut
    _, report = export._render_unflipped(document, panel, {}, 300)
    assert report["cut"] == cut


def test_a_whole_stack_says_nothing_about_a_cut(figure):
    _volume_panel(figure)
    panel = repository.load(figure)["panels"]["pnl_3d"]
    assert export.volume_cut(panel) == ""
    assert export.volume_cut(repository.load(figure)["panels"]["pnl_1"]) == ""
