"""Video Studio: edits and first saves run one at a time; closing stops a recording.

Two quick edits on a never-saved video created two saved videos, and two quick
edits on a saved one sent one revision twice and lost the second to a 409
nobody saw. A recording left running when the tool closed leaked its listener
and interval. The probe (tests/js/video_edit_serial_probe.mjs) runs the real
controller with the bar, preview and server faked.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_edit_serial_probe.mjs"


def test_edits_are_serialized_and_close_stops_recording():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert report["checked"] == 4
    assert not report["failures"], json.dumps(report["failures"], indent=2, ensure_ascii=False)
    assert proc.returncode == 0
