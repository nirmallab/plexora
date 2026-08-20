"""What the open page redraws after the user changes which matrix is read.

Answering the requirements modal ("gate on layers['log1p']", or "apply log1p")
makes the server re-read the table and drop its caches, but the page was loaded
against the previous matrix. `refreshDataset()` in main.js is what brings it
back in step, and the part of that worth pinning is object identity rather than
fetching: ChannelList and ViewerSidebar each take a reference to the description
object at boot and read their ranges and histograms out of it for the rest of
the session. Fetching correct numbers and binding them to a *new* object leaves
both of them on the old one -- the Thresholding panel reads the sidebar's copy,
so that shipped as a log-valued slider readout drawn over an X-valued histogram
axis, with the handle pinned to the far left of a domain still in raw counts.

The probe extracts the real function out of main.js rather than reimplementing
it, and drives it with stand-ins for the objects a live page holds.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "refresh_dataset_probe.mjs"


def test_answering_the_modal_redraws_from_the_matrix_that_is_now_read():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe raises if it cannot find the function to extract, but assert on
    # its output too so a future refactor that quietly loosens a check is not
    # mistaken for a passing test.
    for line in (
        "the objects ChannelList and ViewerSidebar hold report the new matrix",
        "every holder is still the one shared description object",
        "lazily fetched image statistics survive the refresh",
        "the read spec is adopted and the channel list is left alone",
    ):
        assert line in proc.stdout, proc.stdout
