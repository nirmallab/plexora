"""Untrusted names reach the browser as text, not as markup.

Channel names (OME-XML, uploaded CSVs), figure annotation colours and ids are
data from outside, and each of them is written into the page by code that
builds markup. tests/js/xss_sinks_probe.mjs runs those sinks from the shipped
source with a hostile value and checks where it lands; this file runs it, and
syntax-checks the files the fix touched.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "xss_sinks_probe.mjs"

CHECKS = (
    "a channel name reaches the legend as text, and no markup is written",
    "a colour cannot close the attribute it is written into, on any annotation",
    "a hostile id stays inside its selector when the canvas looks an object up",
    "the point editor's redraw finds its element by an escaped id and writes escaped markup",
    "the selector escape is the browser's CSS.escape wherever the page has one",
)

SOURCES = (
    "plexora/client/src/js/views/imageViewer.js",
    "plexora/plugins/figure_builder/static/figureSchema.js",
    "plexora/plugins/figure_builder/static/figureCanvas.js",
    "plexora/plugins/figure_builder/static/figurePointEditor.js",
    "plexora/plugins/figure_builder/static/figureContextBar.js",
    "plexora/plugins/figure_builder/static/figureTextEditor.js",
    "plexora/plugins/figure_builder/static/figureWorkspace.js",
    "plexora/plugins/figure_builder/static/figureQuickEdit.js",
)


@pytest.fixture(scope="module")
def node():
    found = shutil.which("node")
    if found is None:
        pytest.skip("node is not installed")
    return found


@pytest.mark.parametrize("source", SOURCES)
def test_syntax(node, source):
    done = subprocess.run([node, "--check", str(REPO_ROOT / source)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_hostile_names_land_as_text(node):
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines()
             if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
