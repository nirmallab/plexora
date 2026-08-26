"""Shapes, and the four ways one silently stops being the shape it was.

A shape annotation is a list of nodes in a normalised box. Both languages have
to agree about what those nodes mean: the browser draws from its own reading,
the document stores Python's, and `compose` hands the exporters a third form
derived from Python again. Nothing in the running application ever compares the
two, so a divergence is invisible until someone opens a PDF.

`tests/js/figure_shape_probe.mjs` owns the case table -- deliberately awkward
input, every preset, placements, splits, renormalisations -- and emits its own
answers beside the inputs. This pushes the identical inputs through
`schema.normalize_shape` and `server/shapegeom.py` and compares. The table lives
in one place rather than being written out twice and drifting, which is the
failure it exists to catch.

The rest pins the things that are only visible later:

  * a malformed shape must never delete itself. `normalize_document` reads a
    raised ValueError as "drop this annotation", so a normaliser that refuses
    bad input removes the user's shape on the next reload with nothing to show
    for it;

  * a preset's ink must fill its box exactly. `w_mm`/`h_mm` are what all three
    renderers rotate ABOUT, so ink that sits off-centre in its box does not
    merely look off-centre -- a rotated shape lands somewhere else entirely;

  * `rect` and `ellipse` must stay readable forever. They are not creatable any
    more, and every figure drawn before the shape tool is full of them.

    node tests/js/figure_shape_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import compose, schema, shapegeom

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_shape_probe.mjs"

#: Two doubles that took the same route through the same IEEE arithmetic in two
#: languages are equal, but the route is not always identical -- a literal
#: written `3.0 *` in one and `3 *` in the other, a sum reassociated by a JIT.
#: This is loose enough to survive that and tight enough that any real
#: disagreement about geometry is thousands of times bigger.
TOLERANCE = 1e-9


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _same(got, want, path=""):
    """Structural equality with a tolerance on the numbers. Returns a list of
    differences, deepest first, so a failure names the coordinate."""
    if isinstance(want, bool) or isinstance(got, bool):
        return [] if got is want else [f"{path}: {got!r} != {want!r}"]
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        if abs(got - want) <= TOLERANCE + TOLERANCE * abs(want):
            return []
        return [f"{path}: {got!r} != {want!r}"]
    if isinstance(want, (list, tuple)) and isinstance(got, (list, tuple)):
        if len(got) != len(want):
            return [f"{path}: {len(got)} entries, expected {len(want)}"]
        out = []
        for index, (a, b) in enumerate(zip(got, want)):
            out.extend(_same(a, b, f"{path}[{index}]"))
        return out
    if isinstance(want, dict) and isinstance(got, dict):
        if set(got) != set(want):
            return [f"{path}: keys {sorted(got)} != {sorted(want)}"]
        out = []
        for key in want:
            out.extend(_same(got[key], want[key], f"{path}.{key}"))
        return out
    return [] if got == want else [f"{path}: {got!r} != {want!r}"]


def test_the_browsers_own_shape_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], json.dumps(data["problems"], indent=2)[:4000]
    assert returncode == 0


def test_the_probe_actually_measured_something(report):
    """A probe whose case table is empty passes every assertion in it."""
    _, data = report
    assert len(data["normalizeCases"]) >= 15
    assert len(data["presets"]) >= 15
    assert len(data["segments"]) >= 5
    assert len(data["renormalizeCases"]) >= 3


def test_the_client_and_the_server_normalise_shapes_the_same_way(report):
    """The important one.

    Every case the probe ran, run again here. A mismatch means the canvas draws
    one path and the document stores another, for input a user can produce --
    a shape dragged outside its own box, a handle half-written by a failed
    round trip, a preset name that arrived with whitespace.
    """
    _, data = report
    mismatches = []
    for case in data["normalizeCases"]:
        got = schema.normalize_shape(case["input"])
        problems = _same(got, case["output"])
        if problems:
            mismatches.append({"case": case["name"], "differences": problems,
                               "python": got, "browser": case["output"]})
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_the_constants(report):
    _, data = report
    constants = data["constants"]
    assert constants["KAPPA"] == pytest.approx(shapegeom.KAPPA, abs=1e-15)
    assert constants["MAX_NODES"] == schema.MAX_SHAPE_NODES
    assert constants["COORD_SLACK"] == schema.SHAPE_COORD_SLACK
    assert constants["MAX_FLATTEN_DEPTH"] == shapegeom.MAX_FLATTEN_DEPTH
    assert constants["PRESET_IDS"] == list(schema.SHAPE_PRESETS)


def test_the_two_languages_put_the_same_path_in_the_same_millimetres(report):
    """`compose` emits absolute mm and the browser draws in the box's own 0-1
    space, so this is the one place the two forms are provably the same path."""
    _, data = report
    mismatches = []
    for case in data["segments"]:
        got = [list(segment) for segment in
               shapegeom.segments_mm(case["shape"], case["geometry"])]
        problems = _same(got, case["segments"])
        if problems:
            mismatches.append({"case": case["name"], "differences": problems[:6]})
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_languages_flatten_a_curve_to_the_same_polyline(report):
    """The raster exporter draws the polyline Python produces; the canvas hit-
    tests against the one JavaScript produces. A different subdivision means a
    shape you can see but cannot click, or the reverse."""
    _, data = report
    mismatches = []
    for case in data["segments"]:
        segments = shapegeom.segments_mm(case["shape"], case["geometry"])
        got = [list(point) for point in shapegeom.flatten(segments, 0.05)]
        problems = _same(got, case["flattened"])
        if problems:
            mismatches.append({"case": case["name"],
                               "python_points": len(got),
                               "browser_points": len(case["flattened"]),
                               "differences": problems[:6]})
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_every_preset_fills_its_own_box(report):
    """Measured in Python, on the tables JavaScript built.

    A preset whose ink stops short of its box looks fine sitting upright and
    lands somewhere else the moment it is rotated, because the box centre is
    the pivot all three renderers turn about.
    """
    _, data = report
    wrong = []
    for preset in data["presets"]:
        bounds = shapegeom.ink_bounds(preset["nodes"], preset["closed"])
        if _same(list(bounds), [0.0, 0.0, 1.0, 1.0]):
            wrong.append({"preset": preset["id"], "bounds": bounds})
    assert not wrong, json.dumps(wrong, indent=2)


def test_a_renormalised_shape_fills_its_new_box(report):
    """The other half of the same rule, on the boxes the point editor computes.

    Whatever the user drags where, the commit that follows must leave the ink
    exactly filling the box -- otherwise the error compounds edit by edit and
    the resize handles drift away from the shape they belong to.

    A shape can legitimately be flat: a horizontal open path has no height. It
    gets a floored box so it stays grabbable, and on that axis the rule is that
    the ink sits in the MIDDLE of the box rather than filling it -- which is
    what keeps the pivot on the line and the two handles either side of it.
    """
    _, data = report
    wrong = []
    for case in data["renormalizeCases"]:
        output = case["output"]
        x, y, w, h = shapegeom.ink_bounds(output["nodes"], case["closed"])
        for axis, low, span in (("x", x, w), ("y", y, h)):
            want = [0.0, 1.0] if span > 0.5 else [0.5, 0.0]
            if _same([low, span], want):
                wrong.append({"case": case["name"], "axis": axis,
                              "got": [low, span], "want": want})
    assert not wrong, json.dumps(wrong, indent=2)


# -- what the server does on its own ----------------------------------------


def _document(shape):
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "figure_id": "fig_abc123",
        "pages": [{"page_id": "pg_1", "name": "Page 1"}],
        "annotations": {"ann_1": {
            "annotation_id": "ann_1", "type": "shape", "page_id": "pg_1",
            "geometry": {"x_mm": 10, "y_mm": 10, "w_mm": 30, "h_mm": 20, "rotation": 0},
            "shape": shape,
        }},
    }


@pytest.mark.parametrize("shape", [
    None, "a rectangle", 42, {}, {"nodes": []}, {"nodes": [{"x": 0, "y": 0}]},
    {"closed": True, "nodes": [{"x": float("nan"), "y": float("inf")}]},
    {"preset": "rect", "closed": True, "nodes": [None, "x"]},
])
def test_a_malformed_shape_never_deletes_itself(shape):
    """The one that matters more than it looks.

    `normalize_document` drops an annotation whose normaliser raises. If
    `normalize_shape` refused bad input the user's shape would vanish on the
    next open, with no error and nothing to undo. It falls back to the unit
    rectangle instead, which is on the page and can be grabbed and fixed.
    """
    document = schema.normalize_document(_document(shape))
    assert "ann_1" in document["annotations"], "the shape was silently deleted"
    stored = document["annotations"]["ann_1"]["shape"]
    assert len(stored["nodes"]) >= 3
    assert stored["closed"] is True


def test_a_shape_keeps_its_nodes_through_a_round_trip():
    document = schema.normalize_document(_document(
        {"preset": "pentagon", "closed": True,
         "nodes": [{"x": 0.5, "y": 0.0, "type": "smooth",
                    "in": {"x": 0.4, "y": 0.0}, "out": {"x": 0.6, "y": 0.0}},
                   {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]}))
    stored = document["annotations"]["ann_1"]["shape"]
    assert stored["preset"] == "pentagon"
    assert stored["nodes"][0]["type"] == "smooth"
    assert stored["nodes"][0]["out"] == {"x": 0.6, "y": 0.0}
    assert stored["nodes"][1]["in"] is None
    assert schema.normalize_shape(stored) == stored, "the stored form is not canonical"


def test_only_a_shape_carries_a_path():
    """`_update_annotation` deep-merges style and copies the rest, so a key that
    appears on the wrong kind is a key that stays there forever."""
    for kind in ("text", "line", "arrow", "rect", "ellipse"):
        normalized = schema.normalize_annotation({
            "annotation_id": "ann_1", "type": kind, "page_id": "pg_1",
            "shape": {"preset": "rect", "closed": True,
                      "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
        })
        assert "shape" not in normalized, kind


def test_the_types_drawn_before_the_shape_tool_still_open():
    """`rect` and `ellipse` are not creatable any more. Removing them from
    ANNOTATION_TYPES would delete every one ever drawn, on the next read."""
    assert "rect" in schema.ANNOTATION_TYPES
    assert "ellipse" in schema.ANNOTATION_TYPES
    for kind in ("rect", "ellipse"):
        normalized = schema.normalize_annotation(
            {"annotation_id": "ann_1", "type": kind, "page_id": "pg_1"})
        assert normalized["type"] == kind


def test_opacity_is_normalised_for_every_kind():
    for raw, expected in ((None, 1.0), (0.5, 0.5), (-3, 0.0), (9, 1.0),
                          ("half", 1.0), (True, 1.0)):
        normalized = schema.normalize_annotation({
            "annotation_id": "ann_1", "type": "shape", "page_id": "pg_1",
            "style": {"opacity": raw},
        })
        assert normalized["style"]["opacity"] == expected, raw


def test_a_shape_composes_to_one_path_instruction():
    document = schema.normalize_document(_document(
        {"preset": "rect", "closed": True,
         "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]}))
    annotation = document["annotations"]["ann_1"]
    instructions = compose.page_instructions(
        document, document["pages"][0], [], [annotation])
    paths = [item for item in instructions if item["kind"] == "path"]
    assert len(paths) == 1
    assert paths[0]["segments"][0] == ("move", 10.0, 10.0)
    assert paths[0]["closed"] is True
    assert "rotation" not in paths[0]


def test_a_rotated_shape_turns_about_its_own_centre():
    """The pivot every backend uses comes from x/y/w/h, which is why the box has
    to be the ink's tight bounds and not merely a box the ink fits inside."""
    document = schema.normalize_document(_document(
        {"preset": "rect", "closed": True,
         "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]}))
    annotation = document["annotations"]["ann_1"]
    annotation["geometry"]["rotation"] = 30.0
    instructions = compose.page_instructions(
        document, document["pages"][0], [], [annotation])
    path = [item for item in instructions if item["kind"] == "path"][0]
    assert path["rotation"] == 30.0
    assert path["pivot"] == {"x": 25.0, "y": 20.0}


def test_an_open_path_is_never_filled():
    """A fill on an open path is the browser's guess about where the missing
    edge runs, and it is a different guess in every renderer."""
    document = schema.normalize_document(_document(
        {"preset": "custom", "closed": False,
         "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}))
    annotation = document["annotations"]["ann_1"]
    annotation["style"]["fill"] = "#ff0000"
    instructions = compose.page_instructions(
        document, document["pages"][0], [], [annotation])
    path = [item for item in instructions if item["kind"] == "path"][0]
    assert path["fill"] is None
    assert path["closed"] is False


def test_a_zero_width_stroke_is_no_stroke():
    """There is no `stroke: none` key. A width of nothing already says it, and a
    second way to say the same thing is a second thing to keep in step."""
    document = schema.normalize_document(_document(
        {"preset": "rect", "closed": True,
         "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}))
    annotation = document["annotations"]["ann_1"]
    annotation["style"]["line_width_pt"] = 0.0
    annotation["style"]["fill"] = "#00ff00"
    instructions = compose.page_instructions(
        document, document["pages"][0], [], [annotation])
    path = [item for item in instructions if item["kind"] == "path"][0]
    assert path["stroke"] is None
    assert path["fill"] == "#00ff00"


# -- one control, one surface -----------------------------------------------

STATIC = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
TEMPLATES = (REPO_ROOT / "plexora" / "plugins" / "figure_builder"
             / "templates" / "figure_builder")


def test_shape_styling_lives_in_exactly_one_place():
    """The failure this prevents has already happened once, to text.

    The floating bar's Stroke and Color popovers used to apply to every
    annotation that was not text. With a shape panel beside them there would be
    two controls for one number, in two places, disagreeing about which is
    authoritative -- and for a shape it is worse than for a caption, because a
    single "Color" button cannot say whether it means the fill or the outline.

    Lines went the same way afterwards, so what is left is the legacy
    `rect`/`ellipse` -- not creatable since the shape tool, no panel of their
    own, and this popover is the only way in to either.
    """
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")
    for action in ('id: "stroke"', 'id: "colour"'):
        start = actions.index(action)
        # To the start of the next entry, so the window is the whole action and
        # nothing of its neighbour, however long its comments grow.
        end = actions.index("{ id: ", start)
        body = actions[start:end]
        assert "FigureActions.LEGACY_BOXES.includes(" in body, action
    # An allow-list, not a chain of exclusions: a new annotation kind with a
    # panel of its own should have to be ADDED to reach these, rather than
    # reaching them by default and being noticed later.
    assert 'LEGACY_BOXES() { return ["rect", "ellipse"]; }' in actions


def test_the_panel_and_the_mode_are_wired_end_to_end():
    """Each of these is one line, and each is silently inert if it is missing:
    a panel with no strip to appear in, a strip with no element, a mode with no
    way to enter it."""
    workspace = (STATIC / "figureWorkspace.js").read_text(encoding="utf-8")
    body = (TEMPLATES / "workspace_body.html").read_text(encoding="utf-8")
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")

    assert 'shape: "fb_shape_panel"' in workspace, "the strip cannot hold the panel"
    assert 'id="fb_shape_panel"' in body, "the panel has no element"
    assert "new FigureShapePanel(" in workspace, "the panel is never built"
    assert 'id: "editpoints"' in actions, "there is no way into Edit Points"
    assert "editPoints(annotationId)" in workspace, "the action leads nowhere"
    # A contextual panel must never become the pinned one, or shutting it hands
    # the strip to itself.
    assert 'name !== "text" && name !== "shape" && name !== "line"' in workspace


def test_the_numeric_fields_are_text_inputs():
    """`type="number"` has no selection API, so `selectionStart` is null and the
    panel's caret restore puts the caret back at 0 after every keystroke --
    typing "20" leaves 02 in the field. Documented in figureTextPanel.js, and
    the same trap here."""
    panel = (STATIC / "figureShapePanel.js").read_text(encoding="utf-8")
    assert 'type="number"' not in panel
    assert 'inputmode="decimal"' in panel


def test_every_shape_script_is_registered():
    """A file that is not in PLUGIN.scripts never reaches the browser, and the
    symptom is a canvas that loads and then throws on the first press."""
    from plexora.plugins.figure_builder import PLUGIN

    for name in ("figureShapeGeometry.js", "figureShapeDefs.js", "figureShapePanel.js",
                 "figureShapeDrawing.js", "figurePointEditor.js"):
        assert name in PLUGIN.scripts, name
        assert (STATIC / name).exists(), name


def test_every_preset_survives_the_whole_server_pipeline(report):
    """Each of the seventeen, from the browser's table through the normaliser to
    an instruction the exporters can draw.

    Cheap, and it catches the one class of mistake the parity tests above cannot:
    a preset whose table is fine in isolation and is rejected, truncated or
    flattened by something downstream -- which would show up as a shape the
    picker offers and the PDF does not contain.
    """
    _, data = report
    page = schema.new_page("pg_1")
    broken = []
    for preset in data["presets"]:
        annotation = schema.normalize_annotation({
            "annotation_id": "ann_1", "type": "shape", "page_id": "pg_1",
            "geometry": {"x_mm": 10, "y_mm": 10, "w_mm": 40, "h_mm": 30, "rotation": 17},
            "style": {"fill": "#cccccc", "color": "#222222", "line_width_pt": 1},
            "shape": {"preset": preset["id"], "closed": preset["closed"],
                      "nodes": preset["nodes"]},
        })
        if annotation["shape"]["preset"] != preset["id"]:
            broken.append({"preset": preset["id"], "why": "the normaliser refused it"})
            continue
        if len(annotation["shape"]["nodes"]) != len(preset["nodes"]):
            broken.append({"preset": preset["id"], "why": "nodes were lost"})
            continue
        paths = [item for item in compose.page_instructions(
            {"settings": schema.default_settings()}, page, [], [annotation])
            if item["kind"] == "path"]
        if len(paths) != 1:
            broken.append({"preset": preset["id"], "why": "did not compose to one path"})
            continue
        segments = paths[0]["segments"]
        if segments[0][0] != "move" or (preset["closed"] and segments[-1][0] != "close"):
            broken.append({"preset": preset["id"], "why": f"malformed path {segments[:2]}"})
            continue
        points = shapegeom.flatten(segments, 0.1)
        if not 3 <= len(points) <= 2000:
            broken.append({"preset": preset["id"], "why": f"{len(points)} points"})
    assert not broken, json.dumps(broken, indent=2)
