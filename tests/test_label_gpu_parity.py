"""The GPU cell layer draws the same pixels as the CPU path, in a real browser.

`tests/js/label_gpu_parity.mjs` loads the shipped labelTile.js, labelGpu.js and
both shaders into Chromium and draws synthetic label tiles through each path:
four cell sizes (so the small-cell fill weight is 0, partial, and 1), ids past 2^16 and
2^23, a gate as a mask and as an id Set, dense and sparse colour tables with a
hidden category, filled mode, a stored-outlines datasource, a layer at partial
opacity and two layers stacked. Alpha must match exactly and the premultiplied
colour within one step (a stacked layer's alpha within one step: the 2D
canvas's own blend of a WebGL source rounds differently). It also checks the
GL context is handed back to the channel program intact.

Headless Chromium draws WebGL with SwiftShader, which is also what a machine
with no usable GPU gets -- so the default run is the no-GPU case. Set
PLEXORA_BROWSER_HEADED=1 to run it on the machine's GPU as well.

Skipped where Playwright cannot be found (plexora/client/node_modules or the npx
cache).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "js" / "label_gpu_parity.mjs"

MODES = ["headless"] + (["headed"] if os.environ.get("PLEXORA_BROWSER_HEADED") == "1" else [])


@pytest.fixture(scope="module", params=MODES)
def parity(request):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    args = [node, str(SCRIPT)] + (["--headed"] if request.param == "headed" else [])
    proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT, timeout=600)
    if proc.stdout.startswith("SKIP"):
        pytest.skip(proc.stdout.strip())
    return proc


def test_every_case_matches_the_cpu_path(parity):
    assert parity.returncode == 0, parity.stdout + parity.stderr
    assert "FAIL" not in parity.stdout, parity.stdout


def test_the_matrix_actually_ran(parity):
    lines = [line for line in parity.stdout.splitlines() if line.startswith("PASS ")]
    # 4 geometries x 2 segmentation modes x 8 cases, plus the hand-back check.
    assert len(lines) == 4 * 2 * 8 + 1, parity.stdout
    for needle in ("| filled | gate mask", "| outlines | sparse colours",
                   "3 px cells 300x200 edge tile | filled | two layers stacked",
                   "the context is handed back to the channel program"):
        assert needle in parity.stdout, needle


def test_the_small_cell_fill_was_exercised(parity):
    """The derived-outline alpha rules only matter where cells are small; a
    matrix whose tiles all had fill 0 would pass without testing them."""
    import re

    fills = {float(f) for f in re.findall(r"fill (\d\.\d{3})", parity.stdout)}
    assert 0.0 in fills and 1.0 in fills, fills
    assert any(0.05 < f < 0.95 for f in fills), f"no partial fill among {fills}"
