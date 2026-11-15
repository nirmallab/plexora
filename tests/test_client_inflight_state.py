"""Client state that outlives the moment it was made for.

Each probe runs the shipped client code in node against fakes:

- tests/js/viewer_prefs_flush_probe.mjs: a viewer preference whose save fails
  goes with the next save instead of being forgotten.
- tests/js/settings_teardown_probe.mjs: leaving Settings stops the migration
  poll (even one whose request is out) and the licence device-code poll.
- tests/js/viewer_pick_reference_probe.mjs: a right or middle release throws
  nothing, the right-click pick reads the reference item's own source, and
  reopening the base image's eye keeps the view however long it was closed.
- tests/js/slot_order_inflight_probe.mjs: reordering channel rows does not
  carry a levelling or an ease in flight onto another row.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(probe_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / probe_name)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout, proc.stdout
    return proc.stdout


def test_a_failed_viewer_prefs_save_is_kept_for_the_next():
    out = _run("viewer_prefs_flush_probe.mjs")
    assert out.count("ok - ") >= 7


def test_leaving_settings_stops_its_pollers():
    out = _run("settings_teardown_probe.mjs")
    assert out.count("ok - ") >= 7


def test_right_click_pick_and_the_base_image_eye():
    out = _run("viewer_pick_reference_probe.mjs")
    assert out.count("ok - ") >= 7


def test_reordering_rows_leaves_in_flight_work_behind():
    out = _run("slot_order_inflight_probe.mjs")
    assert out.count("ok - ") >= 7
