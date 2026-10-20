"""A gating session's layer in the Single Cell Classifier panel, in the browser's
half: the codes' colours, the dot, the block, the raw switch, the hover card.

The checks are in `tests/js/gating_classifier_probe.mjs`, which runs the real
`GateClassifierControl` against a hand-built DOM and API; the server's half is
in test_classifier_routes.py. This drives the probe and names every check, so
one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_classifier_probe.mjs"
STYLES = REPO_ROOT / "plexora" / "plugins" / "gating" / "static" / "gating.css"
PANEL = REPO_ROOT / "plexora" / "plugins" / "gating" / "templates" / "gating" / "panel.html"

#: Every line the probe prints.
CHECKS = [
    "codes 4 and 5 are drawn sky blue and magenta through the cell layer",
    "a labelled cell keeps its example colour over a disagreement",
    "the dot is filled for a decided marker and hollow for a session's layer",
    "a session's layer names the session, the gate and the counts, and offers Adopt",
    "an adopted layer keeps its head and counts and loses Adopt",
    "calls that no longer match the table cannot be adopted",
    "a panel run with no layer and no gate shows no block",
    "the switch asks for the classifier's own calls only where the run kept them",
    "the hover names a disagreement and the gate's call",
    "a stale layer says why, and no examples hint sits under it",
    "no server change moves the card into this mode",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_classifier_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)


def test_the_layers_block_hides_with_its_attribute_and_wears_the_canvas_colours():
    """Every part of the block that hides itself sets `display`, so each
    restates `[hidden]`; the swatches are the colours the canvas draws codes
    4 and 5 in; the hollow dot matches the words the panel starts it with."""
    styles = STYLES.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")
    assert (".clf-session[hidden],\n.clf-session-counts[hidden],\n.clf-raw-line[hidden],\n"
            ".clf-session-actions[hidden] {\n    display: none;\n}") in styles
    assert 'data-code="4"' in panel and 'data-code="5"' in panel
    four = styles[styles.index('.clf-session-count[data-code="4"] .clf-swatch {'):]
    five = styles[styles.index('.clf-session-count[data-code="5"] .clf-swatch {'):]
    assert "rgb(86, 180, 233)" in four[:four.index("}")]
    assert "rgb(255, 70, 230)" in five[:five.index("}")]
    assert '.marker-combobox-option-indicator[title^="Classified by gating session"]' in styles
    for element_id in ("clf_session", "clf_adopt_button", "clf_discard_button", "clf_raw_toggle"):
        assert f'id="{element_id}"' in panel, element_id
    assert ">Show the classifier's own opinion<" in panel
