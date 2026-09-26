"""The per-tile colorize pass, run under node.

What a registered layer's tile means cannot be asserted from Python and cannot
be asserted by a pixel hash either. A layer is drawn by blitting each channel
twice -- once to take the image underneath away where the channel covers it,
once to add the channel's colour -- and both halves of that rest on properties
of the tile canvas that fail silently: an opaque black backing would make the
first blit erase the whole footprint, and the second item of the pair is handed
a Tile that never passed through the decoder.

So the checks live in `tests/js/tile_colorize_probe.mjs`, run against the
shipped modules, and this drives it and names every check -- so a check that is
quietly deleted fails here rather than passing silently.
"""

import subprocess

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tile_colorize_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "a tile that has not asked for coverage is backed with opaque black",
    "...and its alpha stays the constant it has always been",
    "the reference image's tile carries coverage in its alpha",
    "...and is cleared rather than filled black",
    "...while its colour still comes from the channel table",
    "a layer's channel tile is cleared, never filled black",
    "...and its alpha is told to carry coverage",
    "...and the colour still comes from the record the set holds",
    "a layer item with no channel record draws nothing",
    "the alpha mode is part of a tile's drawn signature",
    "a decoded plane is left on the tile's cache record",
    "...and the second item of a pair draws from it",
    "...rather than blanking the canvas its twin just filled",
    "a layer tile with no pixels yet clears instead of going black",
    "...and says so, so the next frame tries again",
    "on the GPU each layer of a label tile is drawn by labelGpu, bottom first",
    "...each at its own opacity",
    "...and a frame with nothing changed draws nothing",
    "a layer's renderVersion moving redraws the tile",
    "so does restacking the layers",
    "muted cells on the GPU clear the tile once and cache that",
    "a label tile with no ids yet clears and is retried next frame",
    "a layer the GPU could not draw is not cached as drawn",
]


@pytest.fixture(scope="module")
def probe():
    try:
        result = subprocess.run(["node", str(PROBE)], capture_output=True,
                                text=True, cwd=REPO_ROOT, timeout=120)
    except FileNotFoundError:
        pytest.skip("node is not on PATH")
    return result


def test_the_colorize_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("ok - ") == len(CHECKS)
