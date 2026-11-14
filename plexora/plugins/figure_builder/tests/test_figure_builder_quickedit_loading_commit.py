"""Quick Edit must not save a session that is still opening.

`open()` makes the session at once, then waits for the shown channels' ranges
(`pixel_info`) before the channel widget exists, and for a 3-D panel for the
renderer before anything is drawn. Done, Open in Main Viewer, or clicking
another panel (which saves the one being left) in that window used to write
the panel with no channels and a black preview -- and a 3-D panel's preview is
the picture its export is made from. An open that was left mid-way must also
not mount a second channel widget into the session that replaced it, nor throw
on finding none.

`tests/js/figure_quickedit_loading_commit_probe.mjs` drives the real class
against a stub DOM with `pixel_info` held back; this runs it.

Run the probe alone:
    node tests/js/figure_quickedit_loading_commit_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_quickedit_loading_commit_probe.mjs"

CHECKS = (
    "Done on a session still opening writes no panel (no channels, black preview)",
    "and uploads no preview cut from a view that has drawn nothing",
    "Open in Main Viewer on a session still opening saves nothing over the panel",
    "clicking another panel while the first is opening does not save the first",
    "Done on a 3-D panel before its renderer is up stores no picture over its master",
    "a superseded open leaves the new session's channel widget in place",
    "an open cancelled mid-way finishes quietly, without a widget or a session",
)


def test_quick_edit_does_not_save_a_session_still_opening():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    probe_text = PROBE.read_text(encoding="utf-8")
    for name in CHECKS:
        assert name in probe_text, f"probe no longer checks: {name}"

    proc = subprocess.run([node, str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert not report["problems"], json.dumps(report["problems"], indent=2)[:4000]
    assert proc.returncode == 0
