"""Drawing on the page, and the one number that has to agree with the exporter.

The annotation back end -- the schema, the operations, the PDF renderer -- was
finished long before anything could create an annotation, so this is the first
suite that exercises the half in the browser. Most of it runs in node:

    node tests/js/figure_annotation_probe.mjs

The part that cannot is the arrowhead. Its geometry exists twice, in two
languages, and the two are the same picture drawn for two different audiences:
the canvas is what the author looks at while deciding the figure is finished,
and the PDF is what a reviewer sees. An arrowhead that is bigger, or splayed
differently, in one of them is a discrepancy nobody notices until the figure is
submitted -- so it is compared here against `server/strokegeom.py` itself rather
than against a restatement of what it is supposed to do. Neither renderer has
head geometry of its own any more; what is checked below is that the canvas
still ASKS, and converts the answer to screen pixels correctly.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_annotation_probe.mjs"


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


def test_the_drawing_geometry_holds(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


@pytest.mark.parametrize("case,start,end,size", [
    ("points", (10.0, 20.0), (60.0, 20.0), 3.0),
    ("diagonal", (0.0, 0.0), (30.0, 40.0), 5.0),
])
def test_the_canvas_draws_the_same_arrowhead_the_pdf_does(report, case, start, end, size):
    """Both halves, on the same numbers.

    Neither renderer owns arrowhead geometry any more: `strokegeom.head_geometry`
    is a table and `place_head` puts it on the page, and the canvas and the two
    exporters all call them. What this pins is that the canvas still calls them
    -- a shortcut back into the canvas would look identical on screen and be
    wrong in the PDF, which is where it would be found.
    """
    from plexora.plugins.figure_builder.server import strokegeom

    _, data = report
    placed = strokegeom.place_head(
        end, start, strokegeom.head_geometry("open", size, 0))
    tips = [line[1] for line in placed["lines"]]

    for (px, py), (jx, jy) in zip(tips, data["arrow"][case]):
        assert px == pytest.approx(jx, abs=1e-9), case
        assert py == pytest.approx(jy, abs=1e-9), case
    assert len(tips) == 2


def test_the_arrowhead_size_rule_is_the_same_on_both_sides(report):
    """`max(3, line_width * 4)`, in POINTS. The canvas then converts to screen
    pixels; the exporter is already in points. A canvas that sized the head in
    millimetres would look right at one zoom level and wrong at every other.

    That conversion is the only part of head sizing that exists in one language
    only, which is why it is checked here and the rule itself is checked in
    test_figure_builder_lines.py."""
    from plexora.plugins.figure_builder.server import strokegeom

    _, data = report
    px_per_mm = 96 / 25.4
    pt_per_mm = 2.8346

    for line_width, expected_pt in ((0.75, max(3.0, 0.75 * 4)), (2.0, max(3.0, 2.0 * 4))):
        assert strokegeom.head_size(0, line_width) == pytest.approx(expected_pt, abs=1e-9)

    assert data["arrow"]["sizeAtDefaultWidth"] == pytest.approx(
        3.0 * px_per_mm / pt_per_mm, abs=1e-6)
    assert data["arrow"]["sizeAtFatWidth"] == pytest.approx(
        8.0 * px_per_mm / pt_per_mm, abs=1e-6)


def test_every_annotation_type_the_schema_allows_can_be_drawn():
    """Every type the format allows is reachable, or one exists in the document
    and in the exporter with no way to make one.

    An explicit table rather than a loop over ANNOTATION_TYPES, because the
    types are no longer alike: `rect`, `ellipse` and now `arrow` are in the
    tuple so that figures drawn before their replacements still open, and are
    deliberately NOT creatable -- the picker arms `shape`, whose nodes describe
    the first two and fourteen others besides, and the lines card arms
    `line:<variant>`, of which "arrow" is a line carrying a head. A loop would
    demand a menu entry for a type nobody should be able to make a new one of.
    """
    static = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
    template = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "templates"
                / "figure_builder" / "workspace_body.html").read_text(encoding="utf-8")
    workspace = (static / "figureWorkspace.js").read_text(encoding="utf-8")
    defs = (static / "figureShapeDefs.js").read_text(encoding="utf-8")
    line_defs = (static / "figureLineDefs.js").read_text(encoding="utf-8")

    from plexora.plugins.figure_builder.server import schema

    assert 'data-tool="text"' in template
    # Both cards arm `<kind>:<variant>`; the variants themselves are pinned in
    # test_figure_builder_shapes.py and test_figure_builder_lines.py.
    assert 'data-act="line:${id}"' in workspace, "the lines card is not wired"
    assert 'add("line"' in line_defs, "the picker offers no plain line"
    assert 'add("arrow"' in line_defs, "the picker offers no arrow"
    assert 'data-act="shape:${id}"' in workspace, "the shapes card is not wired"
    assert 'add("rect"' in defs, "the picker offers no rectangle"

    reachable = {"text", "line", "shape"}
    superseded = {"rect", "ellipse", "arrow"}
    assert set(schema.ANNOTATION_TYPES) == reachable | superseded, (
        "a new annotation type needs a way to make one, or a reason it has none")
    # Not creatable, but they must stay readable forever: dropping a type from
    # ANNOTATION_TYPES deletes every annotation of it on the next read.
    for kind in superseded:
        assert schema.normalize_annotation(
            {"annotation_id": "ann_1", "type": kind, "page_id": "pg_1"})["type"] == kind


def test_lines_and_arrows_are_drawn_rather_than_boxed():
    """They used to render as empty bordered divs -- indistinguishable from a
    rectangle until the figure was exported, which is the worst possible moment
    to discover it."""
    canvas = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
              / "figureCanvas.js").read_text(encoding="utf-8")
    assert "strokeMarkup" in canvas
    assert "<svg" in canvas
