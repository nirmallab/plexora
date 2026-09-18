"""The browser half of drawing a registered layer, run under node.

The assertion that matters here cannot be made from Python and cannot be made
by a pixel hash either: "a hidden layer costs nothing" is a statement about
whether an item is in OpenSeadragon's world, and a layer at opacity 0 and a
layer removed look identical in a screenshot while costing entirely different
amounts. So the checks live in `tests/js/layer_visibility_probe.mjs`, run
against the shipped module, and this drives it and names every check -- so a
check that is quietly deleted fails here rather than passing silently.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "layer_visibility_probe.mjs"

#: Every line the probe prints. Two of them carry the whole feature: hiding
#: removes the world item, and a hide that lands mid-load still hides.
CHECKS = [
    "a registered layer lands as one world item",
    "tagged with its layer id, so the stack can order it",
    "placed where its transform says",
    "drawn with the LAYER's own tile grid, not the reference's",
    "and no extra zoom levels of its own",
    "hiding REMOVES the item rather than fading it",
    "showing adds it back",
    "at the same placement",
    "removing leaves an empty world",
    "a hide that lands mid-load still hides",
    "a transform OpenSeadragon cannot express draws nothing",
    "the reference layer is not drawn twice",
    "a points layer is not drawn by core",
    "a layer still being built is not drawn",
    "and the one drawable layer is drawn",
    "re-syncing leaves a layer already on screen alone",
    "a layer the config no longer lists is dropped",
]


@pytest.fixture(scope="module")
def probe():
    node = "node"
    try:
        result = subprocess.run([node, str(PROBE)], capture_output=True,
                                text=True, cwd=REPO_ROOT, timeout=120)
    except FileNotFoundError:
        pytest.skip("node is not on PATH")
    return result


def test_the_visibility_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("ok - ") == len(CHECKS)
