"""Video Studio's timeline: a keyframe diamond can be dragged, Alt-copied and
clicked to seek.

The probe (tests/js/video_timeline_drag_probe.mjs) routes pointer events the
way a browser does -- to the element holding pointer capture while it is still
in the document, otherwise to whatever is under the pointer -- so a diamond
that is replaced in the middle of its own gesture loses the rest of it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_timeline_drag_probe.mjs"

CHECKS = (
    "the diamond for k1 sits at 100 px",
    "dragging a diamond 100 px sends one update_keyframe",
    "…that moves it to 2 s",
    "Alt-dragging a diamond sends one add_keyframe",
    "…at the new time with the original's value and pace",
    "clicking a diamond seeks the preview to its time",
    "…and edits nothing",
)


def test_a_keyframe_diamond_drags_copies_and_seeks():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert report["checked"] == len(CHECKS)
    assert not report["failures"], json.dumps(report["failures"], indent=2, ensure_ascii=False)
    assert proc.returncode == 0
