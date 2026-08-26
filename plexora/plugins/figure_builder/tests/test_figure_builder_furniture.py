"""What is drawn ON a panel: the scale bar, the colour bar, and free captions.

Three pieces of furniture, each placed at one of nine anchors, each drawn twice
-- once by the canvas in CSS pixels and once by the exporter in millimetres.
That duplication is the whole risk here, and it is the kind that ships green: a
scale bar bottom-right on screen and bottom-left in the PDF is not something
either half's own tests can see, and nobody looks at the PDF until a reviewer
does.

So the rules that exist twice are held to one fixture. The probe emits the
canvas's answers and `test_the_canvas_and_the_exporter_anchor_furniture_alike`
recomputes every one of them here.

The rest is what a figure tool must not get wrong on its own:

* a scale bar's caption says how long the bar is, and the unit it is written in
  is the user's choice -- "500 µm" beside "1000 µm" is comparable at a glance
  where "500 µm" beside "1 mm" is not;

* a colour bar's ticks are the channel's own display window in RAW units. Any
  other scale -- bytes, percentages -- is a quantity the figure does not encode,
  and a reader would take it at face value;

* the ramp ends at the brightest pixel the renderer can actually produce, which
  is the colour times `CHANNEL_ALPHA`, not the colour;

* and every appearance field defaults to what the bar looked like before any of
  them existed, so that reopening an existing figure does not restyle it.

The canvas's half lives in `tests/js/figure_layout_probe.mjs`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import compose, render, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_layout_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    try:
        return json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def panel(**overrides):
    """A placed, calibrated, two-channel panel."""
    raw = {
        "panel_id": "pnl_1", "source_id": "src_1",
        "placement": {"page_id": "pg_1", "x_mm": 10, "y_mm": 20,
                      "w_mm": 60, "h_mm": 40},
        "scene": {"viewport": {"x": 0, "y": 0, "w": 1000, "h": 800},
                  "channels": [
                      {"key": "c1", "fullname_at_capture": "CD8",
                       "color": {"r": 255, "g": 0, "b": 0}, "window": [100, 4000]},
                      {"key": "c2", "fullname_at_capture": "DAPI",
                       "color": {"r": 0, "g": 0, "b": 255}, "window": [0, 20000]}]},
    }
    raw.update(overrides)
    return schema.normalize_panel(raw)


def document(pixel_size=0.5):
    doc = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1")
    doc["sources"]["src_1"] = schema.normalize_source({
        "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
        "pixel_size": {"value": pixel_size} if pixel_size else None})
    return doc


def furniture(doc, one):
    style = doc["settings"]["style"]
    return compose._panel_furniture(doc, one, 0, "A", style)


def of_kind(instructions, kind):
    return [item for item in instructions if item["kind"] == kind]


def texts(instructions):
    return [item["runs"][0]["text"] for item in of_kind(instructions, "text")]


# -- the rule that exists twice ---------------------------------------------


def test_the_canvas_and_the_exporter_anchor_furniture_alike(probe):
    """Nine anchors, computed by `FigureSchema.anchorBox` for the screen and by
    `compose.anchor_box` for the page.

    The fixture is the probe's own, so the two cannot drift apart by one of them
    being updated: changing the JavaScript changes what this test compares
    against, and the Python has to follow or fail.
    """
    fixture = probe["furniture"]
    place = fixture["place"]
    box = fixture["box"]

    for anchor, expected in fixture["anchors"].items():
        x, y = compose.anchor_box(place, anchor, box["w"], box["h"],
                                  fixture["margin_mm"])
        assert (round(x, 9), round(y, 9)) == (round(expected["x"], 9),
                                              round(expected["y"], 9)), anchor


def test_the_client_defaults_match_the_servers():
    """A panel is built in the BROWSER -- by a capture, a split, a dropped PNG
    -- and drawn from that draft before any round trip. So the defaults exist
    twice, and a field the client forgets is a panel that renders once against
    `undefined.visible` and takes the canvas down.

    Read out of the JavaScript rather than restated here, so adding a field to
    one side and not the other fails instead of drifting.
    """
    source = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
              / "figureSchema.js").read_text(encoding="utf-8")
    body = source[source.index("    defaultFurniture(overrides) {"):]
    body = body[:body.index("\n    },")]

    served = {
        "scalebar": schema.normalize_scalebar({}),
        "colorbar": schema.normalize_colorbar({}),
    }
    for group, defaults in served.items():
        assert f"{group}: {{" in body
        for key, value in defaults.items():
            if value is None:
                assert f"{key}: null" in body, f"{group}.{key}"
            elif isinstance(value, bool):
                assert f"{key}: {str(value).lower()}" in body, f"{group}.{key}"
            elif isinstance(value, str):
                assert f'{key}: "{value}"' in body, f"{group}.{key}"
            else:
                assert f"{key}: {value}" in body, f"{group}.{key}"

    assert "labels: []" in body
    assert "legend: { channels: false }" in body


def test_every_anchor_the_schema_allows_is_one_the_layout_knows():
    """A name accepted by `normalize_panel` that `anchor_parts` does not
    recognise would fall back to the bottom right -- a stored position that is
    silently a different position, which is the one failure a validator is
    supposed to make impossible."""
    for anchor in schema.PANEL_ANCHORS:
        row, column = compose.anchor_parts(anchor)
        assert anchor == ("center" if (row, column) == ("middle", "center")
                          else f"{row}_{column}")


def test_the_colour_bar_stop_count_agrees_with_the_schema():
    """Repeated rather than imported -- `compose` is deliberately free of the
    schema's validation machinery -- so it is pinned instead."""
    assert compose.COLORBAR_STOPS == schema.COLORBAR_STOPS


def test_a_colour_bar_ends_where_the_renderer_does():
    """The bright end of a ramp has to be the brightest pixel the panel can
    contain. `render` multiplies every channel by CHANNEL_ALPHA -- a bar drawn
    from the raw colour would key a picture nobody can produce."""
    assert compose.CHANNEL_ALPHA == render.CHANNEL_ALPHA
    ramp = compose._ramp({"r": 255, "g": 0, "b": 0}, 2)
    assert ramp == ["#000000", "#e60000"]      # 255 * 0.9 = 229.5 -> 230 = 0xe6


# -- the scale bar -----------------------------------------------------------


def test_a_default_scale_bar_is_exactly_where_it_has_always_been():
    """Every appearance field is new, and every one of them defaults to what the
    bar looked like before they existed. A figure made last year has to reopen
    unchanged -- a scale bar that moved 0.4 mm on reopening is a diff nobody can
    attribute and a re-export nobody can trust."""
    doc = document()
    out = furniture(doc, panel(scalebar={"visible": True}))
    rule = of_kind(out, "rect")[0]

    assert rule["fill"] == "#ffffff"
    assert rule["h"] == compose.SCALEBAR_HEIGHT_MM
    # Bottom right, one INSET_MM in from both far edges.
    assert rule["y"] == pytest.approx(20 + 40 - compose.INSET_MM
                                      - compose.SCALEBAR_HEIGHT_MM)
    assert rule["x"] + rule["w"] == pytest.approx(10 + 60 - compose.INSET_MM)
    assert texts(out) == ["A", "100 µm"]


def test_a_scale_bar_takes_its_corner_its_colour_and_its_weight():
    doc = document()
    out = furniture(doc, panel(scalebar={
        "visible": True, "position": "top_left", "color": "#000000",
        "thickness_mm": 2.0, "margin_mm": 4.0}))
    rule = of_kind(out, "rect")[0]

    assert rule["fill"] == "#000000"
    assert rule["h"] == 2.0
    assert rule["x"] == pytest.approx(10 + 4.0)
    # The caption stays ABOVE the rule in every corner, so a bar moved from one
    # to another keeps the same shape rather than reflowing.
    caption = [item for item in of_kind(out, "text")
               if item["runs"][0]["text"] == "100 µm"][0]
    assert caption["y"] < rule["y"]
    assert caption["align"] == "left"


def test_a_scale_bars_caption_can_be_turned_off_without_moving_the_bar():
    """The bar is anchored, and the caption is part of the block -- so hiding
    the caption has to leave the bar against the same edge rather than sliding
    it up by the height of the text that is no longer there."""
    doc = document()
    with_caption = furniture(doc, panel(scalebar={"visible": True}))
    without = furniture(doc, panel(scalebar={"visible": True, "label": False}))

    assert texts(without) == ["A"]
    assert of_kind(without, "rect")[0]["y"] == pytest.approx(
        of_kind(with_caption, "rect")[0]["y"])


@pytest.mark.parametrize("unit,expected", [
    ("auto", "100 µm"),
    ("nm", "100000 nm"),
    ("um", "100 µm"),
    ("mm", "0.1 mm"),
])
def test_the_caption_is_written_in_the_unit_the_panel_asks_for(unit, expected):
    """The bar's LENGTH never changes -- microns are stored throughout. Naming a
    unit is what makes a row of panels comparable at a glance."""
    doc = document()
    one = panel(scalebar={"visible": True, "unit": unit})
    bar = compose.scale_bar(doc, one)
    assert bar["length_um"] == 100
    assert bar["label"] == expected


def test_an_uncalibrated_source_still_gets_no_bar_at_all():
    """The oldest rule in this file, and the one every new field had to not
    break: a bar drawn from an assumed pixel size is wrong and looks exactly
    like one that is right."""
    doc = document(pixel_size=None)
    out = furniture(doc, panel(scalebar={
        "visible": True, "position": "center", "color": "#ff0000"}))
    assert of_kind(out, "rect") == []


# -- the colour bar ----------------------------------------------------------


def test_a_colour_bar_is_off_until_it_is_asked_for():
    """It is a claim that the intensities are quantitative, and most panels are
    not making it."""
    assert panel()["colorbar"]["visible"] is False
    assert of_kind(furniture(document(), panel()), "swatch") == []


def test_one_ramp_per_visible_channel_labelled_with_its_own_window():
    """Each channel has its own window, so each gets its own bar. One shared
    axis would have to print one channel's numbers under all of them, which is
    a colour bar that is wrong for every channel but one."""
    doc = document()
    out = furniture(doc, panel(colorbar={"visible": True, "ticks": 2}))

    assert len(of_kind(out, "swatch")) == 2
    assert texts(out) == ["A", "100", "4000", "0", "20000"]


def test_a_hidden_channel_gets_no_ramp():
    one = panel()
    one["scene"]["channels"][1]["visible"] = False
    one["colorbar"] = schema.normalize_colorbar({"visible": True})
    assert len(compose.colorbar_rows(one)) == 1


def test_ticks_can_be_turned_off_and_the_bar_stays():
    doc = document()
    out = furniture(doc, panel(colorbar={"visible": True, "ticks": 0}))
    assert len(of_kind(out, "swatch")) == 2
    assert of_kind(out, "line") == []
    assert texts(out) == ["A"]


def test_a_vertical_colour_bar_runs_low_at_the_bottom():
    """Which is how a reader expects a vertical scale to run, and the opposite
    of how the ramp is stored -- so the exporter reverses the stops. Getting
    this backwards produces a bar that is upside down and perfectly plausible.
    """
    doc = document()
    out = furniture(doc, panel(colorbar={
        "visible": True, "orientation": "vertical", "ticks": 2}))
    bar = of_kind(out, "swatch")[0]

    assert bar["vertical"] is True
    assert bar["ramp"][0] == "#e60000"    # the bright end, drawn first = at the top
    assert bar["h"] > bar["w"]
    # And the low label is below the high one.
    labels = [item for item in of_kind(out, "text") if item["runs"][0]["text"] in ("100", "4000")]
    low = [item for item in labels if item["runs"][0]["text"] == "100"][0]
    high = [item for item in labels if item["runs"][0]["text"] == "4000"][0]
    assert low["y"] > high["y"]


def test_a_colour_bar_stays_inside_the_panel_it_belongs_to():
    """Anchored as one block, ticks and labels included. A bar whose numbers
    hang off the edge of the image is furniture drawn on the panel next door."""
    doc = document()
    place = {"x_mm": 10, "y_mm": 20, "w_mm": 60, "h_mm": 40}
    for orientation in ("horizontal", "vertical"):
        for anchor in schema.PANEL_ANCHORS:
            out = furniture(doc, panel(colorbar={
                "visible": True, "orientation": orientation,
                "position": anchor, "ticks": 3}))
            for item in of_kind(out, "swatch") + of_kind(out, "line"):
                assert place["x_mm"] <= item["x"] <= place["x_mm"] + place["w_mm"]
                assert place["y_mm"] <= item["y"] <= place["y_mm"] + place["h_mm"]


# -- free captions -----------------------------------------------------------


def test_a_caption_is_placed_at_its_own_anchor_and_takes_its_own_style():
    doc = document()
    out = furniture(doc, panel(labels=[
        {"label_id": "lbl_1", "text": "Tumor", "position": "bottom_center",
         "color": "#ffd60a", "size_pt": 12, "bold": True}]))
    caption = [item for item in of_kind(out, "text")
               if item["runs"][0]["text"] == "Tumor"][0]

    assert caption["align"] == "center"
    assert caption["runs"][0]["color"] == "#ffd60a"
    assert caption["runs"][0]["size_pt"] == 12
    assert caption["runs"][0]["bold"] is True


def test_a_caption_with_no_size_follows_the_figure():
    """Stored as None rather than as a copy of the number, so that changing the
    figure's label size still moves every caption that never asked for one."""
    doc = document()
    doc["settings"]["style"]["label_size_pt"] = 17.0
    out = furniture(doc, panel(labels=[
        {"label_id": "lbl_1", "text": "Tumor", "size_pt": None}]))
    caption = [item for item in of_kind(out, "text")
               if item["runs"][0]["text"] == "Tumor"][0]
    assert caption["runs"][0]["size_pt"] == 17.0


def test_an_empty_caption_draws_nothing_and_is_not_an_error():
    out = furniture(document(), panel(labels=[{"label_id": "lbl_1", "text": ""}]))
    assert texts(out) == ["A"]


def test_captions_keep_their_order_and_their_ids():
    """The order decides which of two captions in the same corner is on top, and
    the ids are what make editing the third of five an update to that one rather
    than a rewrite of the list."""
    one = panel(labels=[{"label_id": "lbl_b", "text": "second"},
                        {"label_id": "lbl_a", "text": "first"}])
    assert [entry["label_id"] for entry in one["labels"]] == ["lbl_b", "lbl_a"]


def test_a_caption_with_no_id_is_dropped():
    """Ids come from the client, like every other id in this format. One without
    is a row nothing can address -- not editable, not deletable, and impossible
    to tell apart from its neighbour."""
    one = panel(labels=[{"text": "orphan"}, {"label_id": "lbl_a", "text": "kept"}])
    assert [entry["text"] for entry in one["labels"]] == ["kept"]


def test_there_is_a_ceiling_on_how_much_furniture_one_panel_can_carry():
    one = panel(labels=[{"label_id": f"lbl_{n}", "text": str(n)} for n in range(200)])
    assert len(one["labels"]) == schema.MAX_PANEL_LABELS
    assert schema.normalize_colorbar({"ticks": 500})["ticks"] == schema.MAX_COLORBAR_TICKS


# -- the legend, and what it no longer contains ------------------------------


def test_a_legend_names_channels_and_nothing_else():
    """Overlay rows are gone. The export re-renders channels from the source and
    reproduces no cell colouring at all, so a phenotype row keyed a picture the
    exported figure does not contain -- and whether a figure had one depended on
    which plugins were installed the day it was captured.
    """
    one = panel(legend={"channels": True, "plugins": True},
                scene={"viewport": {"x": 0, "y": 0, "w": 1000, "h": 800},
                       "channels": [{"key": "c1", "fullname_at_capture": "CD8",
                                     "color": {"r": 255, "g": 0, "b": 0}}],
                       "plugins": {"roi": {
                           "version": "1", "state": {},
                           "legend": [{"kind": "categorical", "label": "Tumor",
                                       "color": "#e04c4c"}]}}})

    assert "plugins" not in one["legend"]
    assert one["scene"]["plugins"]["roi"] == {"version": "1", "state": {}}
    assert compose.legend_rows(one) == [{"label": "CD8", "color": "#ff0000"}]


def test_a_legend_swatch_is_a_flat_colour():
    """The ramp form went out with the overlay rows it was for. A swatch still
    carrying one would be drawn as a gradient by both backends -- which is what
    the colour bar is now, in the place a colour bar belongs."""
    doc = document()
    out = furniture(doc, panel(legend={"channels": True}))
    for swatch in of_kind(out, "swatch"):
        assert swatch["ramp"] is None
