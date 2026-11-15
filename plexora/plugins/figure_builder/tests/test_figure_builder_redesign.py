"""The three-column editor (2026-11): what it added to the document, and the
one rule it made the canvas and the exporters share.

* Every object carries `name`, `hidden` and `locked`, additively: an old
  document reads as named by its content, shown and unlocked, and a key the
  normaliser did not list would round-trip in the browser and vanish on reload.
* A visual group may carry a `layout` record -- preset, gaps, fit -- which is
  what keeps a layout editable. `update_group` may rename it, reorder its
  members and change or clear its layout, and nothing else.
* Hidden objects are not drawn by the exporters and keep their letter; panels
  are stacked by `z` in the file, as on screen.
* A panel whose shape is not its field's shows the CENTRE of the field at the
  field's proportions -- never stretched -- and its scale bar is measured across
  what it shows. `tests/js/figure_redesign_probe.mjs` holds the browser's half;
  every parity case it prints is re-run here through Python.

    node tests/js/figure_redesign_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import compose, export, operations, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_redesign_probe.mjs"
STATIC = Path(__file__).resolve().parents[1] / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "figure_builder"

SOURCE = {
    "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
    "image": {"width": 4000, "height": 3000},
    "pixel_size": {"value": 1.0, "unit": "µm", "source": "metadata"},
}


@pytest.fixture
def document():
    base = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1",
                               created_at="2026-01-01T00:00:00Z")
    return operations.apply_operations(base, [{"op": "add_source", "source": SOURCE}])


def add_panel(panel_id, x=10, y=10, w=40, h=40, z=0, viewport=None):
    return {"op": "add_panel", "panel": {
        "panel_id": panel_id, "source_id": "src_1",
        "scene": {"viewport": viewport or {"x": 0, "y": 0, "w": 1000, "h": 1000}},
        "placement": {"page_id": "pg_1", "x_mm": x, "y_mm": y, "w_mm": w, "h_mm": h, "z": z},
    }}


# -- the probe --------------------------------------------------------------


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert proc.stdout, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def test_the_browser_arithmetic_holds(probe):
    code, report = probe
    assert code == 0 and not report["problems"], "\n".join(report["problems"])


def test_the_browser_and_the_exporter_show_the_same_field(probe):
    """Every case the probe ran through FigureSchema.shownViewport, through
    compose.visible_viewport. A disagreement here is a scale bar the canvas
    measures across one field and the PDF across another."""
    _, report = probe
    assert report["parity"]
    for case in report["parity"]:
        place = case["place"]
        python = compose.visible_viewport(case["viewport"], place["w_mm"], place["h_mm"])
        shown = case["shown"]
        if compose._turned(python):
            assert python["orientation"] == pytest.approx(shown["orientation"]), case
        else:
            box = lambda v: {k: v[k] for k in ("x", "y", "w", "h")}   # noqa: E731
            assert box(python) == pytest.approx(box(shown)), case


# -- layer fields -------------------------------------------------------------


def test_an_old_object_reads_as_named_by_its_content_shown_and_unlocked(document):
    after = operations.apply_operations(document, [add_panel("pnl_a")])
    panel = after["panels"]["pnl_a"]
    assert (panel["name"], panel["hidden"], panel["locked"]) == ("", False, False)


def test_name_hidden_and_locked_survive_a_save_and_a_reload(document):
    after = operations.apply_operations(document, [
        add_panel("pnl_a"),
        {"op": "update_panel", "panel_id": "pnl_a",
         "changes": {"name": "Tumour core", "hidden": True, "locked": True}},
        {"op": "add_annotation", "annotation": {
            "annotation_id": "ann_1", "type": "text", "page_id": "pg_1",
            "geometry": {"x_mm": 1, "y_mm": 1, "w_mm": 20, "h_mm": 5}}},
        {"op": "update_annotation", "annotation_id": "ann_1",
         "changes": {"name": "Title", "locked": True}},
    ])
    reloaded = schema.normalize_document(json.loads(json.dumps(after)))
    assert reloaded["panels"]["pnl_a"]["name"] == "Tumour core"
    assert reloaded["panels"]["pnl_a"]["hidden"] is True
    assert reloaded["panels"]["pnl_a"]["locked"] is True
    assert reloaded["annotations"]["ann_1"]["name"] == "Title"
    assert reloaded["annotations"]["ann_1"]["locked"] is True
    assert reloaded["annotations"]["ann_1"]["hidden"] is False


# -- layout groups ---------------------------------------------------------------


def test_a_layout_record_is_coerced_and_never_refused():
    assert schema.normalize_group_layout(None) is None
    assert schema.normalize_group_layout({"preset": "from-a-newer-build"}) is None
    layout = schema.normalize_group_layout({"preset": "2x2", "gap_x_mm": -4,
                                            "gap_y_mm": 9, "fit": "stretch"})
    assert layout == {"preset": "2x2", "gap_x_mm": 0.0, "gap_y_mm": 9.0, "fit": "fit"}


def test_every_browser_preset_is_one_the_server_keeps():
    """A preset the server does not list is dropped on save, which would turn a
    layout into a plain group the moment the page is reloaded."""
    source = (STATIC / "figureLayouts.js").read_text(encoding="utf-8")
    for preset in schema.LAYOUT_PRESETS:
        assert f'id: "{preset}"' in source, preset
    import re
    browser = set(re.findall(r'\{ id: "([a-z0-9]+)", label:', source))
    assert browser == set(schema.LAYOUT_PRESETS)


def test_a_layout_group_can_be_renamed_reordered_and_detached(document):
    four = [add_panel(f"pnl_{c}", x=10 + i * 45) for i, c in enumerate("abcd")]
    grouped = operations.apply_operations(document, four + [{"op": "group_items", "group": {
        "group_id": "grp_1", "member_ids": ["pnl_a", "pnl_b", "pnl_c", "pnl_d"],
        "layout": {"preset": "2x2", "gap_x_mm": 3, "gap_y_mm": 3, "fit": "fit"}}}])
    assert grouped["groups"]["grp_1"]["layout"]["preset"] == "2x2"

    renamed = operations.apply_operations(grouped, [{"op": "update_group", "group_id": "grp_1",
        "changes": {"name": "Regions", "member_ids": ["pnl_d", "pnl_c", "pnl_b", "pnl_a"]}}])
    assert renamed["groups"]["grp_1"]["name"] == "Regions"
    assert renamed["groups"]["grp_1"]["member_ids"] == ["pnl_d", "pnl_c", "pnl_b", "pnl_a"]

    detached = operations.apply_operations(renamed, [{"op": "update_group", "group_id": "grp_1",
                                                      "changes": {"layout": None}}])
    assert detached["groups"]["grp_1"]["layout"] is None
    assert detached["groups"]["grp_1"]["member_ids"] == ["pnl_d", "pnl_c", "pnl_b", "pnl_a"]


def test_update_group_cannot_change_who_is_in_a_group(document):
    two = operations.apply_operations(document, [add_panel("pnl_a"), add_panel("pnl_b"),
                                                 add_panel("pnl_c"),
        {"op": "group_items", "group": {"group_id": "grp_1", "member_ids": ["pnl_a", "pnl_b"]}}])
    with pytest.raises(ValueError, match="reorder"):
        operations.apply_operations(two, [{"op": "update_group", "group_id": "grp_1",
                                           "changes": {"member_ids": ["pnl_a", "pnl_c"]}}])


# -- what the exporter draws -------------------------------------------------------


def test_a_hidden_panel_is_not_drawn_and_keeps_its_letter(document):
    doc = operations.apply_operations(document, [
        add_panel("pnl_a", x=10), add_panel("pnl_b", x=60), add_panel("pnl_c", x=110),
        {"op": "update_panel", "panel_id": "pnl_b", "changes": {"hidden": True}},
    ])
    page = doc["pages"][0]
    panels = list(doc["panels"].values())
    drawn = compose.page_instructions(doc, page, panels, [])
    ids = [item["panel_id"] for item in drawn if item["kind"] == "panel"]
    assert ids == ["pnl_a", "pnl_c"]
    letters = [item["runs"][0]["text"] for item in drawn if item.get("kind") == "text"
               and item["runs"][0]["text"] in ("A", "B", "C")]
    assert letters == ["A", "C"]


def test_a_hidden_annotation_is_not_drawn(document):
    doc = operations.apply_operations(document, [{"op": "add_annotation", "annotation": {
        "annotation_id": "ann_1", "type": "rect", "page_id": "pg_1", "hidden": True,
        "geometry": {"x_mm": 1, "y_mm": 1, "w_mm": 20, "h_mm": 5}}}])
    assert compose.page_instructions(doc, doc["pages"][0], [],
                                     list(doc["annotations"].values())) == []


def test_panels_are_stacked_by_z_in_the_file_as_on_screen(document):
    """Reading order says B is drawn after A; z says A is on top. The file
    follows z, which is what the canvas draws."""
    doc = operations.apply_operations(document, [
        add_panel("pnl_a", x=10, y=10, z=5), add_panel("pnl_b", x=20, y=20, z=1)])
    drawn = [item["panel_id"] for item in compose.page_instructions(
        doc, doc["pages"][0], list(doc["panels"].values()), []) if item["kind"] == "panel"]
    assert drawn == ["pnl_b", "pnl_a"]


def test_the_canvas_and_the_file_letter_a_centred_row_the_same_way(probe):
    """A fit layout centres a wider picture in its cell, so its top is a little
    lower than its neighbours'. Both sides keep it in its row (A B C, not
    A C B), held to the probe's own fixture."""
    _, report = probe
    case = report["rows"]
    panels = [{"panel_id": entry["panel_id"],
               "placement": {key: entry[key] for key in ("x_mm", "y_mm", "w_mm", "h_mm", "z")}}
              for entry in reversed(case["panels"])]
    assert [panel["panel_id"] for panel in compose.reading_order(panels)] == case["order"]


# -- one stack for panels and annotations ------------------------------------------


def _box(annotation_id, z):
    return {"op": "add_annotation", "annotation": {
        "annotation_id": annotation_id, "type": "rect", "page_id": "pg_1", "z": z,
        "geometry": {"x_mm": 15, "y_mm": 15, "w_mm": 20, "h_mm": 10}}}


def _drawn(doc):
    """The panel and the rectangle, in the order the file draws them (a panel's
    letter and border are its own furniture, not part of the question)."""
    return [item.get("panel_id") or "annotation"
            for item in compose.page_instructions(doc, doc["pages"][0], list(doc["panels"].values()),
                                                  list(doc["annotations"].values()))
            if item["kind"] in ("panel", "rect")]


def test_a_new_figure_stacks_panels_and_annotations_together(document):
    """An image moved above a shape covers it, on the page and in the file."""
    assert document["settings"]["stack"] == "shared"
    doc = operations.apply_operations(document, [add_panel("pnl_a", z=2), _box("ann_1", 1)])
    assert _drawn(doc) == ["annotation", "pnl_a"]


def test_an_old_figure_keeps_every_annotation_above_every_panel(document):
    """A figure saved before `stack` existed has no key and reads as split, so
    it looks exactly as it did until its order is next changed."""
    old = json.loads(json.dumps(document))
    del old["settings"]["stack"]
    doc = schema.normalize_document(old)
    assert doc["settings"]["stack"] == "split"
    doc = operations.apply_operations(doc, [add_panel("pnl_a", z=2), _box("ann_1", 1)])
    assert _drawn(doc) == ["pnl_a", "annotation"]


def test_the_stacking_setting_survives_a_save_and_cannot_be_garbage(document):
    doc = operations.apply_operations(document, [
        {"op": "set_meta", "changes": {"settings": {"stack": "shared"}}}])
    assert schema.normalize_document(doc)["settings"]["stack"] == "shared"
    doc = operations.apply_operations(document, [
        {"op": "set_meta", "changes": {"settings": {"stack": "sideways"}}}])
    assert doc["settings"]["stack"] == "split"


# -- a panel of another shape --------------------------------------------------------


def test_a_panel_of_its_fields_shape_shows_the_whole_field():
    viewport = {"x": 0, "y": 0, "w": 1000, "h": 800}
    assert compose.visible_viewport(viewport, 50, 40) is viewport


def test_a_narrower_panel_shows_the_centre_and_is_not_stretched():
    shown = compose.visible_viewport({"x": 0, "y": 0, "w": 1000, "h": 800}, 40, 40)
    assert shown == {"x": 100, "y": 0, "w": 800, "h": 800}


def test_the_scale_bar_measures_what_the_panel_shows():
    """A square field 1000 µm across, in a panel half as wide as it is tall:
    the panel shows 500 µm across, so an automatic bar is 100 µm and a fifth
    of the panel -- not 250 µm and a quarter, which is what measuring across
    the whole field drew while the left and right quarters were off the page."""
    document = schema.new_document("fig_aaaaaaaaaaaa")
    document["sources"]["src_1"] = {"source_id": "src_1",
                                    "pixel_size": {"value": 1.0, "unit": "µm"}}
    panel = {"source_id": "src_1", "scalebar": {"visible": True, "target_um": None},
             "scene": {"viewport": {"x": 0, "y": 0, "w": 1000, "h": 1000}},
             "placement": {"w_mm": 30, "h_mm": 60}}
    bar = compose.scale_bar(document, panel)
    assert bar["length_um"] == 100
    assert bar["fraction"] == pytest.approx(0.2)


def test_an_imported_picture_is_cut_to_the_panel_not_stretched():
    from PIL import Image
    picture = Image.new("RGB", (400, 300), "white")
    panel = {"placement": {"w_mm": 30, "h_mm": 30}}
    cut = export._crop_to_panel(picture, panel, 400, 300)
    assert cut.size == (300, 300)
    same = export._crop_to_panel(picture, {"placement": {"w_mm": 40, "h_mm": 30}}, 400, 300)
    assert same is picture


# -- the page --------------------------------------------------------------------------


def test_the_three_columns_replace_the_rail_and_the_floating_tray():
    body = (TEMPLATES / "workspace_body.html").read_text(encoding="utf-8")
    for element in ('id="fb_left"', 'id="fb_layers"', 'id="fb_tray_panel"', 'id="fb_insert_view"',
                    'id="fb_right"', 'id="fb_inspector"', 'id="fb_menu_add"',
                    'id="fb_menu_layouts"', 'id="fb_menu_arrange"', 'id="fb_menu_annotate"',
                    'id="fb_preview"', 'id="fb_toggle_left"', 'id="fb_toggle_right"'):
        assert element in body, element
    assert 'id="fb_tool_rail"' not in body
    # The four object panels are docked in the inspector now, not floating cards.
    for panel in ("fb_text_panel", "fb_shape_panel", "fb_line_panel", "fb_image_panel"):
        start = body.index(f'id="{panel}"')
        tag = body[body.rindex("<aside", 0, start):start]
        assert "fb-card" not in tag and "fb-side-panel" not in tag, panel


def test_the_new_scripts_load_before_the_workspace_that_uses_them():
    from plexora.plugins.figure_builder import PLUGIN
    scripts = list(PLUGIN.scripts)
    for name in ("figureLayouts.js", "figureLayers.js", "figureInspector.js"):
        assert scripts.index(name) < scripts.index("figureWorkspace.js"), name


# -- the design's additions: a border and a letter of the panel's own --------------


def test_a_border_is_four_strips_inside_the_panel(document):
    doc = operations.apply_operations(document, [
        add_panel("pnl_a", x=10, y=20, w=40, h=30),
        {"op": "update_panel", "panel_id": "pnl_a",
         "changes": {"border": {"visible": True, "width_pt": 72 / 25.4, "color": "#123456"}}}])
    drawn = compose.page_instructions(doc, doc["pages"][0], list(doc["panels"].values()), [])
    strips = [item for item in drawn if item["kind"] == "rect" and item.get("fill") == "#123456"]
    assert len(strips) == 4
    for strip in strips:   # 72/25.4 pt is exactly 1 mm, and none leaves the panel
        assert min(strip["w"], strip["h"]) == pytest.approx(1.0)
        assert strip["x"] >= 10 - 1e-9 and strip["x"] + strip["w"] <= 50 + 1e-9
        assert strip["y"] >= 20 - 1e-9 and strip["y"] + strip["h"] <= 50 + 1e-9


def test_an_old_panel_has_no_border_and_its_usual_letter(document):
    doc = operations.apply_operations(document, [add_panel("pnl_a")])
    panel = doc["panels"]["pnl_a"]
    assert panel["border"]["visible"] is False
    assert {k: panel["label"][k] for k in ("size_pt", "color", "position", "bold", "background")} == \
        {"size_pt": None, "color": "#ffffff", "position": "top_left", "bold": True, "background": ""}


def test_a_letter_takes_its_own_corner_size_colour_and_ground(document):
    doc = operations.apply_operations(document, [
        add_panel("pnl_a", x=10, y=20, w=40, h=30),
        {"op": "update_panel", "panel_id": "pnl_a", "changes": {"label": {
            "text": "", "auto": True, "visible": True, "size_pt": 14, "color": "#ff0000",
            "position": "bottom_right", "bold": False, "background": "#000000"}}}])
    drawn = compose.page_instructions(doc, doc["pages"][0], list(doc["panels"].values()), [])
    ground = next(item for item in drawn if item["kind"] == "rect" and item.get("fill") == "#000000")
    letter = next(item for item in drawn if item.get("kind") == "text"
                  and item["runs"][0]["text"] == "A")
    assert ground["x"] + ground["w"] == pytest.approx(50 - compose.INSET_MM)
    assert ground["y"] + ground["h"] == pytest.approx(50 - compose.INSET_MM)
    assert drawn.index(ground) < drawn.index(letter)
    assert letter["runs"][0]["size_pt"] == 14
    assert letter["runs"][0]["color"] == "#ff0000"
