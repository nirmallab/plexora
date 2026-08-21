"""What a label tile holds, and when it lets go of it.

A label tile carries the decoded id array (~4 MB at 1024 square) and one cached
RGBA canvas per layer drawn from it (~4 MB each). Those canvases are what lets
several plugins stack without refetching anything, and they are the largest
thing on the client heap.

Nothing else in the suite can see any of this. The Python tests render templates
and serve tiles; the pixel probes assert what one canvas contains, not how many
exist or how long they live. The failure mode is a tab that grows for as long as
somebody keeps panning, which no assertion anywhere was in a position to notice
-- and which was in fact happening: `tile-unloaded` freed `_array` and left the
rendered canvas behind. `test_the_probe_can_actually_fail` reinstates exactly
that line, so the probe is proven to catch it rather than trusted to.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "label_tile_lifecycle_probe.mjs"
VIEWER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "imageViewer.js"

#: The two lines that free the per-layer canvases, and the handler as it stood
#: before them -- freeing the decoded array and leaking every canvas.
FREES_CANVASES = """            e.tile._layerContexts?.clear();
            delete e.tile._layerContexts;
"""


def _run(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def probe():
    returncode, output = _run()
    assert returncode == 0, output
    return output


def test_a_decoded_tile_carries_one_canvas_per_drawn_layer(probe):
    assert "a decoded tile gets one canvas per layer in the stack" in probe, probe
    assert "and each was rendered exactly once" in probe, probe


def test_re_rendering_one_layer_leaves_the_rest_of_the_stack_alone(probe):
    """The reason gating over a phenotype map is cheaper than one combined layer
    would be: editing a gate rebuilds the gate layer's canvases and does not
    re-derive a single colour."""
    assert "re-rendering one layer rebuilds only that layer's canvas" in probe, probe
    assert "and the other layer's canvas is still the one it had" in probe, probe


def test_a_layer_switched_off_stops_costing_anything_that_grows():
    """The whole argument for having no cap on how many plugins may be loaded.
    A hidden layer's canvases go immediately -- not when the tiles are evicted --
    and it renders nothing into tiles loaded afterwards, so panning with it off
    is flat."""
    _, probe = _run()
    assert "hiding a layer takes its canvas off every loaded tile" in probe, probe
    assert "without waiting for the tiles to be evicted" in probe, probe
    assert "showing it again builds its canvases back" in probe, probe
    assert "and only that layer was rendered" in probe, probe


def test_eviction_frees_the_canvases_as_well_as_the_array(probe):
    assert "eviction frees every cached canvas on the tile" in probe, probe


def test_the_probe_can_actually_fail(tmp_path):
    """Run it against the eviction handler as it shipped -- freeing the decoded
    array and leaking the rendered canvas -- and it must report the leak."""
    mutated = tmp_path / "imageViewer.js"
    source = VIEWER.read_text(encoding="utf-8")
    assert FREES_CANVASES in source, "the lines this test mutates have moved or been renamed"
    # Bytes, not write_text: the probe finds the methods it needs by slicing on
    # multi-line markers, and Windows' newline translation would rewrite every
    # one of them into something those markers no longer match -- a probe that
    # then "fails" for the wrong reason and proves nothing.
    mutated.write_bytes(source.replace(FREES_CANVASES, "").encode("utf-8"))

    returncode, output = _run(mutated)

    assert returncode == 1, output
    assert "FAIL eviction frees every cached canvas on the tile" in output, output
