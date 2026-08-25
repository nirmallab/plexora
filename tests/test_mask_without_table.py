"""A segmentation mask draws without a feature table.

Image, then a mask attached from the edit page, and nothing else -- an ordinary
project shape that `attach_segmentation` explicitly supports (it inserts the
"Area" label channel whether or not the project has data). Drawing the mask
needs nothing per-cell from the server: `renderLabelTile` reads cell ids out of
the label pyramid itself. Cell ids are what PLUGINS want, and every plugin that
wants them is already gated on a table existing.

`NumericData.fetchCells` did not agree. It destructured `this.schema`, which is
null for a project with no data block, so the first click on Outlines threw
before the pyramid was ever requested -- the mask never drew, and
`ViewerControls.selectMode` caught the TypeError as "a mask that will not load"
and fell back.

Run in node against the real numericData.js and datasetContext.js, because
nothing else can: the Python suite renders the template and stops.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "mask_without_table_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_a_mask_without_a_table_loads_no_cells_rather_than_throwing(probe):
    assert "a project with no table loads no cells instead of throwing" in probe, probe
    assert "...and does not ask the server for a table that is not there" in probe, probe


def test_a_table_whose_roles_are_unanswered_is_the_same_case(probe):
    """The other half, and the one that did NOT throw. A schema full of nulls
    sails past the destructure and asks /get_all_cells for columns named
    "null" -- a round trip that could only ever come back wrong."""
    assert "a table with no roles answered is treated the same way" in probe, probe
    assert "...rather than asking the server for columns named null" in probe, probe
    assert "and so is a table missing only one of the three" in probe, probe


def test_a_project_that_has_coordinates_is_untouched(probe):
    """The guard must not become a way to silently skip a fetch that should
    happen: an empty result and a missing result look identical downstream."""
    assert "a project that HAS coordinates still fetches and deinterleaves them" in probe, probe
    assert "...asking for the columns the project recorded, in role order" in probe, probe
    assert "loading twice round-trips once" in probe, probe


def test_the_renderer_already_tolerates_having_no_cells():
    """Why the client-side guard is the whole fix rather than half of it.

    Everything downstream of `loadCells` was already written for zero cells --
    `bindSegmentationBuffers` returns early on empty and `forceRepaint` skips
    `loadBuffers` while `idCount` is 0. If either of those stops guarding, an
    empty table becomes a blank viewer instead of a drawn mask, and the failure
    would be silent."""
    viewer = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "imageViewer.js").read_text(encoding="utf-8")
    bind = viewer.split("bindSegmentationBuffers(ids, centers) {", 1)[1]
    assert "if (!ids?.length || !centers?.length) return;" in bind.split("}", 1)[0]
    repaint = viewer.split("async forceRepaint() {", 1)[1].split("\n    }", 1)[0]
    assert "if (this.idCount) {" in repaint
