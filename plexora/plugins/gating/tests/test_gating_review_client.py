"""Thresholding reviewing an automatic gate, client side.

The checks run `tests/js/gating_review_probe.mjs`: the real
gatingReviewChannels.js, gatingReviewNotes.js and the controller's review
hooks, against a sidebar that behaves like core's -- the channels a gate was
decided with on beside the marker and put back after, never saved; the
agent's notes over the viewer, dismissed and brought back; the switch that
keeps the channels off.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_review_probe.mjs"
STATIC = Path(__file__).resolve().parent.parent / "static"

CHECKS = [
    "supporting channels come on in slots from 2 up, each in its own colour",
    "the marker's own channel and slots 0 and 1 are never touched",
    "a channel already on screen is the user's and is left as it is",
    "moving to the next marker releases what the last one needed and keeps what both do, "
    "without re-enabling it",
    "a released slot is put back exactly as it was",
    "a save while supporting channels are on leaves them out",
    "a claimed slot the user recoloured is left alone",
    "a claimed slot the user switched off, or gave another channel, is theirs again",
    "with no free slot the channel is skipped and said so",
    "a supporting channel the image does not have is skipped",
    "a gate with no review changes nothing",
    "the marker's own save still goes through, and leaves the supporting channels out",
    "the same marker followed twice is a no-op",
    "the notes show the reasoning and a chip in each channel's colour",
    "dismissed, the notes go and an AI Notes link appears under the gate",
    "the link brings the notes back",
    "a new marker gets its own notes; a dismissed marker stays dismissed",
    "provenance that lands after a marker switch applies to the marker on screen",
    "the viewer given back by an agent: stale claims are forgotten and the marker followed again",
    "hiding the panel releases the channels; showing it brings them back",
    "an older row without a review falls back to its reason and no channels",
    "an agent moving the panel switches nothing on",
    "the evidence row is hidden with no gates or only hand gates",
    "the evidence row shows for an AI gate, and is muted but not hidden on a hand gate beside AI "
    "gates",
    "the evidence row counts the channels and wears each one's colour; folded, the chips go",
    "the eye shut releases the channels and keeps the notes; opened, it brings them back",
    "the eye is remembered across controllers (localStorage stub)",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT,
                          timeout=120)


def test_the_review_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert probe.stdout.count("PASS ") == len(CHECKS)


@pytest.mark.parametrize("name", ["gatingReviewChannels.js", "gatingReviewNotes.js"])
def test_the_helpers_parse(name):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    checked = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True,
                             text=True, timeout=60)
    assert checked.returncode == 0, checked.stderr


def test_the_helpers_load_before_the_controller_that_builds_them():
    from plexora.plugins.gating import PLUGIN

    scripts = list(PLUGIN.scripts)
    for name in ("gatingReviewChannels.js", "gatingReviewNotes.js"):
        assert scripts.index(name) < scripts.index("gatingSidebarController.js")
