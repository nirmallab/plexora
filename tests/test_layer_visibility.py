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
    "a layer is added at the opacity it was given",
    "and with the blend it was given",
    "opacity is a blend on the live item, not a re-add",
    "a blend change is set in place where OSD allows it",
    "a blend change re-adds where the viewer has no setter for it",
    "a transform OpenSeadragon cannot express draws nothing",
    "the reference layer is not drawn twice",
    "a points layer is not drawn by core",
    "a layer still being built is not drawn",
    "and the one drawable layer is drawn",
    "re-syncing leaves a layer already on screen alone",
    "a layer the config no longer lists is dropped",
    "a stored opacity reaches the world item",
    "a layer restored with its eye off is dropped on arrival",
    "...and was never added in the first place",
    "two layers and a reference channel key three distinct tiles",
    "a layer landing in an emptied world puts the view back",
    "a layer's saved channels are each a world item",
    "...and each is paired with a blit that clears the way for it",
    "drawn through the GL colorize pass, not coloured server-side",
    "each carrying its own channel record",
    "...the SAME record for both halves of a channel",
    "...and its own texture-cache identity",
    "...which the pair deliberately shares",
    "at one placement for the whole layer",
    "a colour or window change adds and removes NOTHING",
    "...it mutates the record the tile source already holds",
    "a layer has no ground until it is given one",
    "a background adds exactly one item, whatever the channel count",
    "...drawn under every cover blit",
    "...opaquely, so what is beneath the layer is hidden",
    "...from a tile source of its own",
    "setting the same background again changes nothing",
    "the layer's opacity reaches its ground",
    "an eye switched off takes the ground with it",
    "...and switching it back on brings it back",
    "and no background means no item again",
    "a channel switched off takes both of its items with it",
    "the eye drops every channel item",
    "a channel switched on inside a hidden layer is not added at all",
    "...and the eye brings every one of them back",
    "opacity reaches every channel item",
    "every channel of a layer still adds as a reference channel does",
    "...and the layer as a whole clears the way for itself",
    "...nothing asks the shader for an intensity-driven alpha per item",
    "...and there is no blend left to set on a channel set",
    "every clearing blit is marked to sit below every colour blit",
    "the reference image's kind does not change how a layer composites",
    "...and layerStack no longer answers a question nobody asks",
    "an rgb layer is still one server-coloured item, drawn over",
    "the HD toggle refetches every layer channel",
    "...and the pair comes back the right way up",
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
