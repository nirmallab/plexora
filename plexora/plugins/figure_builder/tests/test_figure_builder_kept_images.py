"""The figure canvas keeps its pictures across a render
(tests/browser/figure_images.mjs, in a real Chrome).

The sheet and the tray are drawn from markup on every document change,
selection and zoom step. With `innerHTML` that was a new `<img>` per panel each
time: a request for a preview already on screen, and a white panel until it
came back. On the website, where the page's own Python answers one request at a
time, dragging one panel blanked the whole page for seconds.
`FigureCanvas.replaceKeepingImages` rebuilds the markup and carries the
pictures over; a changed picture replaces the old one only once it has decoded.

Skips without node and playwright-core (PLEXORA_PLAYWRIGHT_MODULES), like the
other browser drivers.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
DRIVER = ROOT / "tests" / "browser" / "figure_images.mjs"
CANVAS = ROOT / "plexora" / "plugins" / "figure_builder" / "static" / "figureCanvas.js"
WORKSPACE = ROOT / "plexora" / "plugins" / "figure_builder" / "static" / "figureWorkspace.js"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"


@pytest.fixture(scope="module")
def record(tmp_path_factory):
    if not shutil.which("node") or not PLAYWRIGHT.is_dir():
        pytest.skip("needs node and playwright-core")
    out = tmp_path_factory.mktemp("figure-images") / "record.json"
    env = {**os.environ, "PLEXORA_PYTHON": os.environ.get("PLEXORA_PYTHON") or sys.executable}
    done = subprocess.run(["node", str(DRIVER), "--out", str(out)], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", timeout=180, env=env)
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail(f"figure_images.mjs wrote no record: {done.stderr[-3000:]}")
    assert not result["failed"], result["failed"]
    return result


def test_a_render_keeps_the_pictures_it_already_has(record):
    kept = record["cases"]["kept"]
    assert kept["sameA"] and kept["sameB"]
    # The rest of the markup still applies to the kept element.
    assert kept["flipApplied"]
    # One picture per URL, however many renders.
    assert record["asked"]["a.png?v=1"] == 1
    assert record["asked"]["b.png?v=1"] == 1


def test_a_failed_picture_is_asked_for_again(record):
    assert record["cases"]["kept"]["brokenRetried"]
    assert record["asked"]["missing.png"] > 1


def test_a_changed_picture_replaces_the_old_one_only_once_it_is_ready(record):
    swap = record["cases"]["swap"]
    assert swap["oldShownFirst"] and swap["swapped"]
    assert swap["blankFrames"] == 0


def test_renders_while_a_picture_is_on_its_way_ask_for_it_once(record):
    assert record["cases"]["burst"]["swapped"]
    assert record["asked"]["b.png?v=3"] == 1


def test_a_picture_written_onto_the_element_meanwhile_wins(record):
    # Quick Edit's live preview writes straight onto the panel's <img>.
    assert record["cases"]["live"]["kept"]


def test_a_lazy_tray_picture_still_swaps(record):
    assert record["cases"]["lazy"]["swapped"] and record["cases"]["lazy"]["complete"]


def test_the_sheet_and_the_tray_render_through_it():
    canvas = CANVAS.read_text(encoding="utf-8")
    render = canvas[canvas.index("    render() {"):canvas.index("    describeObjects() {")]
    assert "FigureCanvas.replaceKeepingImages(this.surfaceEl" in render
    assert "this.surfaceEl.innerHTML =" not in render
    workspace = WORKSPACE.read_text(encoding="utf-8")
    tray = workspace[workspace.index("    renderTray() {"):workspace.index("    renderStatus(")]
    assert "FigureCanvas.replaceKeepingImages(strip" in tray
