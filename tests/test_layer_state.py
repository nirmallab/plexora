"""The base image layer's eye and opacity, run under node.

`test_layer_visibility.py` covers a registered layer, which is one world item.
The base layer is N channel items that come and go as the user picks channels,
and the assertions that matter are about OpenSeadragon's world -- how many
items are in it, and which slots survived a hide. None of that can be seen from
Python and none of it can be seen in a screenshot either: a hidden layer and a
layer at opacity 0 look identical and cost entirely different amounts.

So the checks live in `tests/js/layer_state_probe.mjs`, run against the shipped
files, and this drives it and names every one -- so a check quietly deleted
fails here rather than passing silently.
"""

import subprocess

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_state_probe.mjs"

#: Every line the probe prints. The two that carry the feature: hiding removes
#: the items, and it leaves the channel slots standing so there is something to
#: rebuild from.
CHECKS = [
    "the base layer is its channels on the world, two blits each",
    "...as a cover blit and a paint blit each",
    "...with every cover below every paint",
    "switching the base layer off removes every channel item",
    "and leaves the channel slots standing",
    "a channel chosen while the base is off draws nothing yet",
    "but takes its slot, so it arrives with the rest",
    "switching it back on rebuilds every slot's pair",
    "each one tagged as the reference layer",
    "the base layer's opacity reaches every channel item",
    "and a channel added afterwards arrives already wearing it",
    "switching the base layer off leaves the cell mask alone",
    "and the mask is pinned, because it has no card to drag",
    "a brightfield project's base layer is its one slide item",
    "the Adjustments slider fades the slide through the stack",
    "its eye takes the slide off the world too",
    "and puts it back, still faded",
    "a blank project has its one transparent frame",
    "and its eye is inert, because there would be nothing left",
    "two channels are four items, a cover and a paint each",
    "...addressed at the quality in force when they were built",
    "the HD toggle rebuilds every channel's pair",
    "...without the picture ever leaving the world",
    "...and the slots are untouched, because nothing was removed",
    "...each replacement added invisible, so the two qualities never "
    "composite together",
    "...and preloading, or an invisible item would never load at all",
    "...then faded up together at the base layer's own opacity",
    "and back to the fast path, still with no gap",
    "flipping HD while the base layer is hidden draws nothing",
    "...and the eye brings it back at the quality now asked for",
]


@pytest.fixture(scope="module")
def probe():
    try:
        result = subprocess.run(["node", str(PROBE)], capture_output=True,
                                text=True, cwd=REPO_ROOT, timeout=120)
    except FileNotFoundError:
        pytest.skip("node is not on PATH")
    return result


def test_the_layer_state_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("ok - ") == len(CHECKS)
