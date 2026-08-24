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
submitted -- so it is compared here against `export._arrow_head` itself rather
than against a restatement of what it is supposed to do.
"""

import json
import math
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


class _Recorder:
    """Just enough of a reportlab canvas to catch where the barbs are drawn."""

    def __init__(self):
        self.lines = []

    def line(self, x1, y1, x2, y2):
        self.lines.append((x2, y2))


@pytest.mark.parametrize("case,start,end,size", [
    ("points", (10.0, 20.0), (60.0, 20.0), 3.0),
    ("diagonal", (0.0, 0.0), (30.0, 40.0), 5.0),
])
def test_the_canvas_draws_the_same_arrowhead_the_pdf_does(report, case, start, end, size):
    """Both halves, on the same numbers.

    `_arrow_head` takes a line WIDTH and derives the size; the canvas splits the
    two so the size can be converted to screen pixels separately. Feeding the
    width that produces this size is what makes the comparison exercise the real
    function rather than a fragment of it.
    """
    from plexora.plugins.figure_builder.server import export

    _, data = report
    recorder = _Recorder()
    export._arrow_head(recorder, start[0], start[1], end[0], end[1], size / 4.0)

    for (px, py), (jx, jy) in zip(recorder.lines, data["arrow"][case]):
        assert px == pytest.approx(jx, abs=1e-9), case
        assert py == pytest.approx(jy, abs=1e-9), case
    assert len(recorder.lines) == 2


def test_the_arrowhead_size_rule_is_the_same_on_both_sides(report):
    """`max(3, line_width * 4)`, in POINTS. The canvas then converts to screen
    pixels; the exporter is already in points. A canvas that sized the head in
    millimetres would look right at one zoom level and wrong at every other."""
    _, data = report
    px_per_mm = 96 / 25.4
    pt_per_mm = 2.8346

    for line_width, expected_pt in ((0.75, max(3.0, 0.75 * 4)), (2.0, max(3.0, 2.0 * 4))):
        recorder = _Recorder()
        from plexora.plugins.figure_builder.server import export
        export._arrow_head(recorder, 0.0, 0.0, 10.0, 0.0, line_width)
        # The barb reaches back by `size` along the shaft at 160 degrees, so its
        # distance from the tip IS the size.
        tip_x, tip_y = 10.0, 0.0
        barb = recorder.lines[0]
        drawn = math.hypot(barb[0] - tip_x, barb[1] - tip_y)
        assert drawn == pytest.approx(expected_pt, abs=1e-9)

    assert data["arrow"]["sizeAtDefaultWidth"] == pytest.approx(
        3.0 * px_per_mm / pt_per_mm, abs=1e-6)
    assert data["arrow"]["sizeAtFatWidth"] == pytest.approx(
        8.0 * px_per_mm / pt_per_mm, abs=1e-6)


def test_every_annotation_type_the_schema_allows_can_be_drawn():
    """The rail and the shapes menu between them have to reach all five, or a
    type exists in the document format and in the exporter with no way to make
    one."""
    static = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
    template = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "templates"
                / "figure_builder" / "workspace_body.html").read_text(encoding="utf-8")
    workspace = (static / "figureWorkspace.js").read_text(encoding="utf-8")

    from plexora.plugins.figure_builder.server import schema

    assert 'data-tool="text"' in template
    for kind in schema.ANNOTATION_TYPES:
        if kind == "text":
            continue
        assert f'act: "{kind}"' in workspace, f"the shapes menu cannot make a {kind}"


def test_lines_and_arrows_are_drawn_rather_than_boxed():
    """They used to render as empty bordered divs -- indistinguishable from a
    rectangle until the figure was exported, which is the worst possible moment
    to discover it."""
    canvas = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
              / "figureCanvas.js").read_text(encoding="utf-8")
    assert "strokeMarkup" in canvas
    assert "<svg" in canvas
