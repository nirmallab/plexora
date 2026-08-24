"""Page arithmetic: the part that decides what a figure looks like.

None of it runs anywhere else in this suite -- pytest renders HTML and stops,
and `node --check` sees syntax only -- so each of these mistakes would ship
green and produce a figure that is quietly, expensively wrong:

* a corner resize that does not keep the aspect ratio squashes the tissue in a
  panel, which is a scientific error wearing a layout error's clothes;
* "distribute" as equal CENTRES rather than equal GAPS looks right only when
  every panel is the same size, which for a figure of mixed crops is almost
  never;
* a snap threshold in millimetres is unusably sticky zoomed in and inert zoomed
  out;
* labels in capture order give a 3x2 grid numbered by when each field happened
  to be found.

    node tests/js/figure_layout_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_layout_probe.mjs"


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


def test_the_page_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_the_probe_actually_committed_something(report):
    """A probe whose fixture silently does nothing passes every assertion in
    it."""
    _, data = report
    assert data["commits"] >= 1


def test_the_canvas_and_the_exporter_number_panels_the_same_way(report):
    """The one rule that exists twice, in two languages, held to one fixture.

    `FigureSchema.panelsOnPage` decides what the canvas draws under each panel;
    `compose._reading_order` decides what the PDF prints. If they ever disagree
    the author sees A B C D on screen and a reviewer reads A C B D in the file
    -- and nothing in either half is wrong on its own, so neither half's tests
    would catch it.

    The geometry comes from the probe's own fixture rather than being restated
    here, so the two sides cannot drift apart by one of them being updated.
    """
    from plexora.plugins.figure_builder.server import compose, schema

    _, data = report
    fixture = data["ordering"]

    document = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1")
    document["sources"]["src_1"] = schema.normalize_source(
        {"source_id": "src_1", "kind": "plexora_project", "datasource": "demo"})
    page = document["pages"][0]

    panels = []
    for entry in fixture["panels"]:
        panel = schema.normalize_panel({
            "panel_id": entry["panel_id"], "source_id": "src_1",
            "scene": {"viewport": {"x": 0, "y": 0, "w": 1000, "h": 800}},
            "placement": {
                "page_id": page["page_id"],
                "x_mm": entry["x_mm"], "y_mm": entry["y_mm"],
                "w_mm": entry["w_mm"], "h_mm": entry["h_mm"], "z": entry["z"],
            },
            "label": {"text": "", "auto": True, "visible": True},
        })
        document["panels"][panel["panel_id"]] = panel
        panels.append(panel)

    # Shuffled on the way in, so the answer comes from the sort rather than from
    # the order they happened to be listed in.
    instructions = compose.page_instructions(document, page, list(reversed(panels)), [])
    # A text instruction carries a LIST of styled runs, not one string: a line
    # can mix an italic gene name into a roman sentence, so what is drawn is the
    # runs joined rather than a single `text` field.
    drawn = ["".join(run["text"] for run in item["runs"])
             for item in instructions if item["kind"] == "text"]

    assert drawn == fixture["labels"]
