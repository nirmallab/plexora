"""The viewer's automatic contrast always lands (tests/js/contrast_landing_probe.mjs).

A contrast fit that could not be had used to be kept as if it were the fit,
so the channel's window never landed for the rest of the session; one failed
request ended its levelling; and a viewer left before its fit came back
saved the full range (or the stats' hint) as the levelled window, which every
later open restored. The browser driver tests/browser/contrast.mjs
(tests/test_browser_contrast.py) shows each in the real apps; the probe
holds the shipped methods to it without a browser.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "contrast_landing_probe.mjs"


def test_the_automatic_contrast_always_lands():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    for line in (
        "a fit whose request failed twice is asked again and lands",
        "a fit that could not be had is not kept: the channel's next levelling lands",
        "callers asking at once share one request, and an undrawable fit is still the fit",
        "statistics that could not be had are asked for again",
        "a restored auto row takes its fit when it was saved before the fit, and is left when it is the fit",
        "whenContrastSettled resolves once both passes have landed, and not before",
        "a failed or error answer is no fit and no statistics",
    ):
        assert line in proc.stdout, proc.stdout
