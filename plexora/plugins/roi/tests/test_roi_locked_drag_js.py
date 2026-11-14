"""A locked region cannot be dragged, and one refused save does not stop saving.

Runs tests/js/roi_locked_drag_probe.mjs, which drives the real roiTools.js and
roiState.js in node:vm:

    node tests/js/roi_locked_drag_probe.mjs

The lock exists to stop accidental drags, and the server enforces it
(operations.py's _require_unlocked answers a move of a locked region with a
400). The Select tool must not move a locked region's body on screen nor
commit a move the server will refuse; and when the server does refuse a batch,
the edits the user makes afterwards must still be saved rather than being sent
again and again alongside the refused operation.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "roi_locked_drag_probe.mjs"

DRAG_CHECKS = (
    "dragging the body of a locked region does not move it on screen",
    "...and sends the server nothing it will refuse (a locked region)",
    "dragging the body of a region in a locked category does not move it on screen",
    "...and sends the server nothing it will refuse (a region in a locked category)",
    "an unlocked region beside a locked one still moves",
)

SAVE_CHECKS = (
    "the refused move is reported",
    "an edit made after a refused one still reaches the server",
    "...and the refused operation is not sent again with it",
)


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    try:
        return json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _failed(report, names):
    return [f for f in report["failures"] if f["check"] in names]


def test_the_probe_ran_every_check(report):
    assert report["checked"] >= len(DRAG_CHECKS) + len(SAVE_CHECKS)


def test_the_select_tool_does_not_drag_a_locked_region(report):
    """Dragging the body of a locked region (or one in a locked category) with
    Select leaves it where it is and commits no roi.update_geometry."""
    failed = _failed(report, DRAG_CHECKS)
    assert not failed, json.dumps(failed, indent=2)


def test_an_edit_after_a_refused_save_is_still_saved(report):
    """A batch the server refuses with a 400 does not ride along with every
    later batch, so a region drawn afterwards still reaches the server."""
    failed = _failed(report, SAVE_CHECKS)
    assert not failed, json.dumps(failed, indent=2)
