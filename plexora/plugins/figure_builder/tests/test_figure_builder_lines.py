"""Lines and arrows: one arithmetic, two languages, and one vocabulary.

A line is a start point, a signed offset, and a handful of flat style keys
naming what to draw at each end and how the shaft should look. Both languages
have to agree about what those names mean: the browser draws from its own
reading and the PDF and the raster from Python's, and nothing else in the suite
ever compares the two. A drift shows the user one arrow and prints another, and
they find out in the export.

`tests/js/figure_stroke_probe.mjs` owns the case table -- deliberately awkward,
because the alternative is writing every number twice and having the copies
drift. It runs the browser's geometry over the inputs, emits inputs and answers
together, and everything below re-runs the identical inputs through
`server/strokegeom.py`.

The other half of this file is the vocabulary. Five style keys arrived with the
lines work, and the one that matters is `end_head`: it is the only default in
the whole schema that depends on the annotation's kind, because an `arrow` drawn
before heads were configurable stored no head at all and every one of those has
to keep its barbs. Get that wrong and the failure is silent, universal, and only
visible on the next reload.

Run the probe alone:
    node tests/js/figure_stroke_probe.mjs
"""

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import schema, strokegeom

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_stroke_probe.mjs"
PANEL_PROBE = REPO_ROOT / "tests" / "js" / "figure_line_panel_probe.mjs"

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


def _compare(cases, run):
    """Every case in a probe table, re-run here."""
    mismatches = []
    for case in cases:
        got = run(case)
        problems = _same(got, case["output"])
        if problems:
            mismatches.append({"case": case["name"], "differences": problems,
                               "python": got, "browser": case["output"]})
    return mismatches


# -- the probe itself -------------------------------------------------------


def test_the_browsers_own_stroke_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], json.dumps(data["problems"], indent=2)[:4000]
    assert returncode == 0


def test_the_probe_actually_measured_something(report):
    """A probe whose case table is empty passes every assertion in it."""
    _, data = report
    assert len(data["dashCases"]) >= 18
    assert len(data["headSizeCases"]) >= 20
    assert len(data["headCases"]) >= 15
    assert len(data["legacyCases"]) >= 4
    assert len(data["placeCases"]) >= 5
    assert len(data["trimCases"]) >= 5
    assert len(data["taperCases"]) >= 15
    assert len(data["fadeCases"]) >= 30
    assert len(data["planCases"]) >= 6


# -- the same arithmetic, in Python -----------------------------------------


def test_the_two_agree_on_dash_patterns(report):
    """The array is derived from the enum and never taken from a document.

    reportlab raises on a negative entry or a pattern that sums to zero, and the
    exception comes out of the middle of the PDF writer naming no annotation at
    all -- so the whole export fails and nothing says why.
    """
    _, data = report
    mismatches = _compare(
        data["dashCases"],
        lambda c: strokegeom.dash_pattern(c["style"], c["width"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_head_size(report):
    _, data = report
    mismatches = _compare(
        data["headSizeCases"],
        lambda c: strokegeom.head_size(c["asked"], c["width"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_head_geometry(report):
    _, data = report
    mismatches = _compare(
        data["headCases"],
        lambda c: strokegeom.head_geometry(c["style"], c["size"], c["width"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_where_a_head_lands(report):
    _, data = report
    mismatches = _compare(
        data["placeCases"],
        lambda c: strokegeom.place_head(
            c["tip"], c["other"], strokegeom.head_geometry(c["style"], c["size"], 1)))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_where_the_shaft_stops(report):
    _, data = report
    mismatches = _compare(
        data["trimCases"],
        lambda c: strokegeom.trimmed_shaft(c["p1"], c["p2"], c["trim1"], c["trim2"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_taper_outlines(report):
    _, data = report
    mismatches = _compare(
        data["taperCases"],
        lambda c: strokegeom.taper_outline(
            c["p1"], c["p2"], c["width"], c["edge"], c["trim1"], c["trim2"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_the_fade_ramp(report):
    _, data = report
    mismatches = _compare(
        data["fadeCases"], lambda c: strokegeom.fade_alpha(c["t"], c["edge"]))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_the_render_plan(report):
    """The plan is what both EXPORTERS draw.

    The browser never runs it -- it has `stroke-dasharray` and a gradient -- so
    this is the one piece of geometry where a disagreement is invisible on
    screen by construction and only ever shows up in a file.
    """
    _, data = report
    mismatches = _compare(
        data["planCases"],
        lambda c: strokegeom.shaft_render_plan(
            c["p1"], c["p2"], c["dash"], c["fade"], strokegeom.FADE_STEPS))
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_head_lands_where_the_old_arrow_code_put_it(report):
    """The compatibility pin.

    `FigureCanvas.arrowHeadPoints` and `export._arrow_head` spread two barbs 160
    degrees from the forward direction. Both are gone; every arrow drawn with
    them is not. The probe computes the old formula's answer beside the new
    one's, and this re-runs the new one here -- so a change to the head frame
    that moves every existing arrow's barbs fails in both languages at once.
    """
    _, data = report
    mismatches = []
    for case in data["legacyCases"]:
        geom = strokegeom.head_geometry("open", case["size"], 2)
        got = strokegeom.place_head(
            (case["x2"], case["y2"]), (case["x1"], case["y1"]), geom)
        tips = [line[1] for line in got["lines"]]
        problems = _same(tips, case["legacy"])
        if problems:
            mismatches.append({"case": case["name"], "differences": problems})
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_two_agree_on_the_constants(report):
    _, data = report
    constants = data["constants"]
    assert constants["LINE_STYLES"] == list(schema.LINE_STYLES)
    assert constants["HEAD_STYLES"] == list(schema.HEAD_STYLES)
    assert constants["LINE_EDGES"] == list(schema.LINE_EDGES)
    assert constants["MAX_HEAD_SIZE_PT"] == schema.MAX_HEAD_SIZE_PT
    assert {k: list(v) for k, v in constants["DASH_FACTORS"].items()} \
        == {k: list(v) for k, v in strokegeom.DASH_FACTORS.items()}
    assert constants["MIN_DASH_WIDTH_PT"] == strokegeom.MIN_DASH_WIDTH_PT
    assert constants["FADE_STEPS"] == strokegeom.FADE_STEPS
    assert constants["MAX_DASHES"] == strokegeom.MAX_DASHES
    assert constants["OPEN_HEAD_DEGREES"] == strokegeom.OPEN_HEAD_DEGREES
    assert constants["HEAD_HALF_WIDTH"] == strokegeom.HEAD_HALF_WIDTH
    assert constants["HEAD_TRIM"] == strokegeom.HEAD_TRIM
    assert constants["TAPER_THIN"] == strokegeom.TAPER_THIN


def test_nothing_here_can_reach_reportlab():
    """PNG and TIFF export must work in an install that never had reportlab.

    A module every renderer calls is the easiest place to lose that by
    accident, and the loss is invisible until someone without it exports a PNG
    -- so this reads the imports rather than trusting the docstring that says
    there are none.
    """
    tree = ast.parse(Path(strokegeom.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "math"}


# -- the vocabulary ---------------------------------------------------------


def _annotation(kind, style=None, **extra):
    raw = {"annotation_id": "ann_1", "page_id": "pg_1", "type": kind}
    if style is not None:
        raw["style"] = style
    raw.update(extra)
    return schema.normalize_annotation(raw)


def test_every_kind_carries_the_line_keys():
    """`_update_annotation` deep-merges style, so a key that exists for one kind
    ends up on any annotation that ever copies a style. A conditional key here
    would only be a key that is sometimes missing -- the `opacity` argument."""
    keys = {"line_style", "start_head", "end_head", "head_size_pt", "edge"}
    for kind in schema.ANNOTATION_TYPES:
        assert keys <= set(_annotation(kind)["style"]), kind


def test_a_stored_arrow_keeps_its_head():
    """THE one kind-dependent default in this schema.

    Every arrow drawn before heads were configurable stored no head at all. If
    the default were a uniform "none", every arrow in every existing figure
    would lose its barbs -- silently, on the next read, with nothing on screen
    that says so.
    """
    assert _annotation("arrow")["style"]["end_head"] == "open"
    assert _annotation("arrow")["style"]["start_head"] == "none"


def test_a_stored_line_does_not_grow_one():
    assert _annotation("line")["style"]["end_head"] == "none"
    for kind in ("text", "rect", "ellipse", "shape"):
        assert _annotation(kind)["style"]["end_head"] == "none", kind


def test_an_arrow_may_say_it_has_no_head():
    """The default only fills a gap. Once the user has answered, the answer
    stands -- otherwise "remove the head from this arrow" is un-saveable."""
    assert _annotation("arrow", {"end_head": "none"})["style"]["end_head"] == "none"
    assert _annotation("line", {"end_head": "filled"})["style"]["end_head"] == "filled"


def test_the_defaults_are_what_a_plain_line_looks_like():
    style = _annotation("line")["style"]
    assert style["line_style"] == "solid"
    assert style["edge"] == "standard"
    # Zero is not "no head": it is "size the head from the pen", which is what
    # `strokegeom.head_size` calls auto and what every legacy arrow means.
    assert style["head_size_pt"] == 0.0


@pytest.mark.parametrize("garbage", [
    None, "", 42, [], {}, "SOLID", " dashed ", "taper", "fade", "open-arrow",
    "x" * 500, True, float("nan"),
])
def test_an_unknown_name_draws_plainly_instead_of_deleting_the_annotation(garbage):
    """`normalize_document` reads a ValueError as "drop this annotation".

    So refusing a value a newer build wrote would delete the user's arrow rather
    than draw it with one setting missing. Everything coerces.
    """
    style = _annotation("line", {"line_style": garbage, "start_head": garbage,
                                 "end_head": garbage, "edge": garbage,
                                 "head_size_pt": garbage})["style"]
    assert style["line_style"] in schema.LINE_STYLES
    assert style["start_head"] in schema.HEAD_STYLES
    assert style["end_head"] in schema.HEAD_STYLES
    assert style["edge"] in schema.LINE_EDGES
    assert 0.0 <= style["head_size_pt"] <= schema.MAX_HEAD_SIZE_PT


def test_whitespace_around_a_name_is_not_a_different_name():
    """`clean_text` strips, so a value that made a round trip through an input
    field is the value the user chose."""
    assert _annotation("line", {"edge": "  fade_end  "})["style"]["edge"] == "fade_end"


@pytest.mark.parametrize("asked,stored", [
    (0, 0.0), (-5, 0.0), (6, 6.0), (72, 72.0), (500, 72.0), (0.5, 0.5),
])
def test_head_size_is_clamped_rather_than_refused(asked, stored):
    assert _annotation("line", {"head_size_pt": asked})["style"]["head_size_pt"] == stored


def test_the_keys_survive_a_round_trip():
    """Normalising twice is normalising once. A default that is not idempotent
    rewrites the user's choice every time the document is read."""
    once = _annotation("arrow", {"line_style": "dotted", "start_head": "bar",
                                 "end_head": "diamond", "head_size_pt": 14,
                                 "edge": "fade_both"})
    twice = schema.normalize_annotation(once)
    assert twice["style"] == once["style"]


# -- the edit surface -------------------------------------------------------

STATIC = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
TEMPLATES = (REPO_ROOT / "plexora" / "plugins" / "figure_builder"
             / "templates" / "figure_builder")


def test_every_line_script_is_registered():
    """A file that is not in PLUGIN.scripts never reaches the browser, and the
    symptom is a canvas that loads and then throws on the first press."""
    from plexora.plugins.figure_builder import PLUGIN

    for name in ("figureStrokeGeometry.js", "figureLineDefs.js", "figureLinePanel.js"):
        assert name in PLUGIN.scripts, name
        assert (STATIC / name).exists(), name


def test_the_panel_and_the_card_are_wired_end_to_end():
    """Each of these is one line, and each is silently inert if it is missing:
    a panel with no strip to appear in, a strip with no element, a card with no
    button to open it."""
    workspace = (STATIC / "figureWorkspace.js").read_text(encoding="utf-8")
    body = (TEMPLATES / "workspace_body.html").read_text(encoding="utf-8")

    assert 'line: "fb_line_panel"' in workspace, "the strip cannot hold the panel"
    assert 'id="fb_line_panel"' in body, "the panel has no element"
    assert "new FigureLinePanel(" in workspace, "the panel is never built"
    assert "openLinesCard(event.currentTarget)" in workspace, "the rail opens nothing"
    # A contextual panel must never become the pinned one, or shutting it hands
    # the strip to itself.
    assert 'name !== "text" && name !== "shape" && name !== "line"' in workspace


def test_the_numeric_fields_are_text_inputs():
    """`type="number"` has no selection API, so `selectionStart` is null and the
    panel's caret restore puts the caret back at 0 after every keystroke --
    typing "20" leaves 02 in the field. The same trap the other two panels
    document."""
    panel = (STATIC / "figureLinePanel.js").read_text(encoding="utf-8")
    assert 'type="number"' not in panel
    assert 'inputmode="decimal"' in panel


def test_the_panel_offers_every_value_the_schema_accepts():
    """A vocabulary the user cannot reach is a vocabulary that rots. The head
    and dash rows are built by iterating the shared enums rather than from lists
    of their own, and the edge select is checked against the schema here because
    it IS a list of its own -- seven values that need labels."""
    panel = (STATIC / "figureLinePanel.js").read_text(encoding="utf-8")
    assert "FigureStrokeGeometry.HEAD_STYLES.map(" in panel
    assert "FigureStrokeGeometry.LINE_STYLES.map(" in panel
    for edge in schema.LINE_EDGES:
        assert f'["{edge}"' in panel, edge


def test_the_icons_are_inline_svg_rather_than_font_awesome_spans():
    """FontAwesome walks the document once at boot and replaces `<span class=
    "fas">` in place. A span injected into a card opened afterwards is never
    replaced and draws nothing at all -- so every icon that is generated has to
    be SVG. (The panel's own chrome -- the close X, the stepper's plus and
    minus -- is in the initial markup path and may stay a span.)"""
    defs = (STATIC / "figureLineDefs.js").read_text(encoding="utf-8")
    assert "<svg" in defs
    assert "fa-" not in defs


def test_the_line_panel_behaves():
    """The sidebar, driven against a stub DOM.

    Separate from the geometry probe because it is a different kind of claim:
    nothing here is arithmetic that has to match Python, it is what the panel
    does with a document -- which selections it claims, what it commits, and
    when. See tests/js/figure_line_panel_probe.mjs for what each case would
    cost if it were wrong.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run([node, str(PANEL_PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert not report["problems"], json.dumps(report["problems"], indent=2)[:4000]
    assert proc.returncode == 0
